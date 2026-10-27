#!/usr/bin/env python3
"""Daycare submission desk: the thread header card, untagged triage, and the
watcher's one-click cards (suggested reply, quote, draft).

Everything a person approves is built by code from data: recipients come from
the interaction or the Hub intake, never from a model; test channels redirect
every send to Pranav. Nothing sends without a click by a full Soma member.

CLI (used by Loma workers):
  daycare_desk.py draft-card --draft-id D --channel C --thread-ts T [--title "Customer quote"]
      post a Send / Open-draft card for an existing team@ Gmail draft
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, INSTALL_DIR)
import daycare_watch as watch  # noqa: E402

WORK_ROOT = os.environ.get("SOMA_WORK_ROOT", os.path.expanduser("~/work"))
SKILL_DIR = os.environ.get("DAYCARE_SUBMIT_DIR", os.path.join(WORK_ROOT, "customers/.agents/skills/daycare-submit"))
HOME = os.path.expanduser("~")
os.environ["PATH"] = f"{HOME}/.bun/bin:{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")
ANGIE_USER_IDS = set(os.environ.get("ANGIE_SLACK_USER_IDS", "U0CBAL1JRB6,U09QG7Y6X1C").split(","))
OWNER_ID = os.environ.get("AUTHORIZED_USER_ID", "U0BE1F85H0Q")
TEAM = "team@somainsure.com"
QUO_LINE = "PNtwGcxkbn"  # Daycare - Pranav — +1 (903) 289-5495
CARD_DELAY_SECONDS = 8


def _skill(name: str):
    """daycare-submit helpers (slack_ui send paths, customer_ask copy rules)."""
    if SKILL_DIR not in sys.path:
        sys.path.insert(0, SKILL_DIR)
    return importlib.import_module(name)


def gmail_thread_link(thread_id: str) -> str:
    return f"https://mail.google.com/mail/?authuser={TEAM}#all/{thread_id}"


def _when(epoch: float) -> str:
    return f"<!date^{int(epoch)}^{{date_short_pretty}} at {{time}}|{datetime.fromtimestamp(epoch):%b %d %H:%M}>"


def _confirm(title: str, text: str, ok: str) -> dict:
    return {"title": {"type": "plain_text", "text": title[:100]}, "text": {"type": "plain_text", "text": text[:300]},
            "confirm": {"type": "plain_text", "text": ok}, "deny": {"type": "plain_text", "text": "Cancel"}}


def _btn(action_id: str, text: str, value: dict | None = None, style: str = "", url: str = "", confirm=None) -> dict:
    b = {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}}
    if value is not None:
        b["value"] = json.dumps(value)
    if style:
        b["style"] = style
    if url:
        b["url"] = url
    if confirm:
        b["confirm"] = confirm
    return b


def _phone_label(e164: str) -> str:
    d = re.sub(r"\D", "", e164 or "")[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (e164 or "")


def _fence(text: str) -> str:
    return "```" + (text or "").replace("```", "'''")[:2800] + "```"


# ── 1. thread header: the desk card ───────────────────────────────────────────

def is_parent(evt: dict) -> bool:
    """A new Angie submission parent (or Pranav's stand-in in a test channel)."""
    channel = evt.get("channel", "")
    authors = ANGIE_USER_IDS | ({OWNER_ID} if watch.is_test_channel(channel) else set())
    return (evt.get("type") == "message" and watch.in_desk_window(channel, evt.get("ts"))
            and evt.get("user") in authors and not evt.get("subtype")
            and evt.get("thread_ts") in (None, "", evt.get("ts"))
            and "made the submission for" in (evt.get("text") or ""))


def desk_card_text(th: dict) -> str:
    return f"{(th.get('info') or {}).get('customer', 'Submission')} — submission desk"


def desk_card_blocks(th: dict) -> list[dict]:
    info = th.get("info") or {}
    owner = f"<@{info['owner_id']}>" if info.get("owner_id") else (info.get("owner_email") or "").split("@")[0]
    title = f"*{info.get('customer', 'Submission')}*" + (f" · {info['state']}" if info.get("state") else "")
    meta = " · ".join(x for x in [owner, " + ".join(info.get("lines") or []),
                                  f"eff {info['eff']}" if info.get("eff") else ""] if x)
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": title + (f"\n{meta}" if meta else "")}}]
    if th.get("last_status"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": watch.header_line(th["last_status"])}})
    value = {"c": info.get("customer", "")[:120]}
    blocks.append({"type": "actions", "block_id": "dcs_run", "elements": [
        _btn("dcs_run_intake", "Prepare intake", value),
        _btn("dcs_run_full", "Submit", value, style="primary"),
        _btn("dcs_run_notes", "Run with notes…", value),
    ]})
    if th.get("last_run"):
        r = th["last_run"]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f"Last run: {r.get('label')} by <@{r.get('user')}> {_when(r.get('at', time.time()))}"}]})
    return blocks


def _owner_id(ctx, email: str) -> str:
    if not email:
        return ""
    try:
        return ((ctx.web.users_lookupByEmail(email=email).get("user") or {}).get("id")) or ""
    except Exception:
        return ""


def ensure_desk_card(ctx, channel: str, thread_ts: str, parsed: dict, backfill: bool = False, watcher=None) -> str:
    """Post (or adopt) the desk card in a submission thread and register it with the watcher."""
    if not watch.in_desk_window(channel, thread_ts):
        return ""
    watcher = watcher or watch.get(ctx)
    hub = watch.hub_lookup(parsed["customer"]) if parsed.get("customer") else {}
    info = {"customer": parsed["customer"], "owner_email": parsed.get("owner_email", ""),
            "owner_id": _owner_id(ctx, parsed.get("owner_email", "")), **hub}
    if watch.is_test_channel(channel):
        info["test"] = True
    key = watch.thread_key(channel, thread_ts)
    th = watch.read_state(watcher.state_path if watcher else None)["threads"].get(key) or {}
    card_ts = th.get("card_ts", "")
    if not card_ts:
        import slack_reads
        try:
            replies = slack_reads.replies(ctx.web, channel, thread_ts, limit=100)
        except Exception as e:  # noqa: BLE001  — SuspectRead or API error: fail closed, never a 2nd card
            ctx.log.info(f"DAYCARE DESK dedupe read suspect for {channel}/{thread_ts} ({e}); not posting, will retry")
            return ""
        legacy = next((m for m in replies if m.get("user") == ctx.bot_user_id
                       and any(b.get("block_id") == "dcs_run" for b in m.get("blocks") or [])), None)
        card_ts = legacy.get("ts", "") if legacy else ""
    view = {"info": info, **{k: th[k] for k in ("last_status", "last_run") if th.get(k)}}
    try:
        if card_ts:
            ctx.web.chat_update(channel=channel, ts=card_ts, text=desk_card_text(view), blocks=desk_card_blocks(view))
        else:
            card_ts = ctx.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=desk_card_text(view),
                                               blocks=desk_card_blocks(view), unfurl_links=False)["ts"]
    except Exception as e:  # noqa: BLE001
        ctx.log.info(f"DAYCARE DESK card failed for {channel}/{thread_ts}: {e}")
        return ""
    if watcher:
        watcher.register(channel, thread_ts, info, card_ts=card_ts, backfill=backfill)
        if not backfill:
            threading.Thread(target=watcher.tick_thread, args=(key,), kwargs={"force": True}, daemon=True).start()
    ctx.log.info(f"DAYCARE DESK card {'adopted' if th.get('card_ts') else 'posted'} {channel}/{thread_ts} "
                 f"({info.get('customer')}, lead={info.get('lead_id') or '?'})")
    return card_ts


_parents_seen: set[str] = set()
_parents_lock = threading.Lock()


def on_parent(ctx, evt: dict) -> bool:
    """Message hook: a new submission parent in a desk channel → desk card in ~8 s."""
    if not is_parent(evt):
        return False
    parsed = watch.parse_parent(evt.get("text", ""))
    if not parsed:
        return False
    ts = evt.get("ts", "")
    with _parents_lock:
        if ts in _parents_seen:
            return True
        _parents_seen.add(ts)
    threading.Timer(CARD_DELAY_SECONDS, _ensure_with_retry, args=(ctx, evt["channel"], ts, parsed)).start()
    return True


RETRY_DELAYS = (60, 180, 600)


def _ensure_with_retry(ctx, channel: str, ts: str, parsed: dict, attempt: int = 0) -> None:
    """A suspect dedupe read posts nothing; try again later (the backfill loop also catches it)."""
    if ensure_desk_card(ctx, channel, ts, parsed) or attempt >= len(RETRY_DELAYS):
        return
    t = threading.Timer(RETRY_DELAYS[attempt], _ensure_with_retry, args=(ctx, channel, ts, parsed, attempt + 1))
    t.daemon = True
    t.start()


def record_run(ctx, channel: str, thread_ts: str, label: str, user: str) -> bool:
    """Stamp 'Last run' on the desk card. False when this thread has no desk card."""
    key = watch.thread_key(channel, thread_ts)
    with watch.state() as s:
        th = s["threads"].get(key)
        if not th or not th.get("card_ts"):
            return False
        th["last_run"] = {"label": label, "user": user, "at": time.time()}
        view = dict(th)
    try:
        ctx.web.chat_update(channel=channel, ts=view["card_ts"], text=desk_card_text(view), blocks=desk_card_blocks(view))
    except Exception as e:  # noqa: BLE001
        ctx.log.info(f"DAYCARE DESK run stamp failed: {e}")
    return True


# ── 2. triage: untagged messages in desk channels ─────────────────────────────

ACK_RE = re.compile(r"^(ok(ay)?|k|kk|thanks?( you)?|thx|ty|got it|done|sent|noted|cool|great|perfect|nice|yes|no|"
                    r"yep|yup|nope|sure|\+1|lol|haha|will do|on it|sounds good|np)[.!]*$", re.I)
COMMAND_RE = re.compile(r"^(status\??|\?|stop\??)$", re.I)


def prefilter(evt: dict, bot_user_id: str) -> str:
    """Deterministic first pass. Returns a skip reason, or '' when the message goes to the model."""
    if not watch.is_desk_channel(evt.get("channel", "")):
        return "not a desk channel"
    if not watch.in_desk_window(evt.get("channel", ""), evt.get("thread_ts") or evt.get("ts")):
        return "before the desk go-live cutoff"
    if evt.get("bot_id") or evt.get("subtype") or evt.get("user") in ({bot_user_id} | ANGIE_USER_IDS):
        return "bot or system"
    if evt.get("channel_type") == "im":
        return "dm"
    if is_parent(evt):
        return "submission parent"
    text = (evt.get("text") or "").strip()
    if f"<@{bot_user_id}>" in text:
        return "mentions Loma"
    if re.search(r"<@U[A-Z0-9]+>", text):
        return "addressed to a person"
    bare = re.sub(r":[a-z0-9_+-]+:|<[^>]+>", "", text).strip()
    if len(bare) < 4 or ACK_RE.match(bare) or COMMAND_RE.match(bare):
        return "acknowledgement"
    return ""


TRIAGE_PROMPT = """You triage messages in #daycare-submission, the Slack channel where Soma Insurance's team runs daycare insurance submissions to wholesalers. An assistant (Loma) can: build an intake, prepare/send wholesaler submissions, add facts or corrections to a submission, check status of a submission or quote, draft replies to wholesalers or customers, explain a quote.

Decide if this message needs Loma. relevant=true when it asks for or implies work Loma can do on a submission, asks a question about a submission/quote/customer, or adds facts/corrections about an account. relevant=false for people talking to each other, FYIs that need nothing, acknowledgements, jokes, or anything outside daycare submissions.

Thread parent: {parent}
Recent thread messages:
{recent}

Message to triage (from a teammate): {text}

Reply with JSON only: {{"relevant": true or false, "why": "<8 words"}}"""


def triage_model(text: str, parent: str = "", recent: str = "") -> tuple[bool, str]:
    """Haiku on Loma's local Claude, sharing the watcher's concurrency cap."""
    prompt = TRIAGE_PROMPT.format(parent=(parent or "(top-level message)")[:600], recent=(recent or "(none)")[:2000],
                                  text=text[:2000])
    try:
        _, out = watch.claude_local(prompt, os.environ.get("DAYCARE_TRIAGE_MODEL", "haiku"), 90)
    except subprocess.TimeoutExpired:
        return False, "triage timed out"
    return parse_triage(out)


def parse_triage(out: str) -> tuple[bool, str]:
    m = re.search(r"\{[^{}]*\"relevant\"[^{}]*\}", out or "")
    if not m:
        return False, "unparseable triage output"
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return False, "unparseable triage output"
    return bool(d.get("relevant")), str(d.get("why", ""))[:80]


_triaged: set[tuple[str, str]] = set()


def triage(ctx, evt: dict, model=triage_model) -> bool:
    """Untagged message in a desk channel: relevant → dispatch a Loma worker; else nothing."""
    reason = prefilter(evt, ctx.bot_user_id)
    channel, ts = evt.get("channel", ""), evt.get("ts", "")
    if reason:
        if reason not in ("not a desk channel", "bot or system", "before the desk go-live cutoff"):
            ctx.log.info(f"DAYCARE TRIAGE skip {channel}/{ts}: {reason}")
        return False
    with _parents_lock:
        if (channel, ts) in _triaged:
            return False
        _triaged.add((channel, ts))
    thread_ts = evt.get("thread_ts") or ts
    parent, recent = "", ""
    if thread_ts != ts:
        import slack_reads
        try:  # context only: a suspect read degrades to "no context", never blocks triage
            msgs = slack_reads.replies(ctx.web, channel, thread_ts, limit=40, backoff=(3,))
            parent = next((m.get("text") or "" for m in msgs if m.get("ts") == thread_ts), "")
            recent = "\n".join(f"- {(m.get('text') or '')[:200]}" for m in msgs[1:][-6:] if m.get("ts") != ts)
        except Exception as e:  # noqa: BLE001
            ctx.log.info(f"DAYCARE TRIAGE context read suspect {channel}/{thread_ts}: {e}")
    relevant, why = model(evt.get("text") or "", parent, recent)
    ctx.log.info(f"DAYCARE TRIAGE {'relevant' if relevant else 'ignore'} {channel}/{ts}: {why}")
    if relevant:
        text = evt.get("text") or ""
        ctx.dispatch(channel, ts, evt.get("user", "?"), text.replace("\n", " ")[:300], thread_ts, "", False, text, False)
    return relevant


def triage_async(ctx, evt: dict) -> None:
    threading.Thread(target=triage, args=(ctx, evt), daemon=True).start()


# ── 3/4. watcher cards ────────────────────────────────────────────────────────

WHOLESALER_LEAKS = re.compile(r"\b(other (markets?|wholesalers?|carriers?|brokers?)|clearance|also (sent|submitted)|"
                              r"markel|another (market|wholesaler))\b", re.I)


def copy_problems(body: str, via: str, party: str, desks_seen: list[str], own_desk: str) -> list[str]:
    if not body.strip():
        return ["empty"]
    if party == "customer":
        return _skill("customer_ask").copy_problems(body, "sms" if via == "sms" else "email")
    problems = []
    if WHOLESALER_LEAKS.search(body):
        problems.append(f"not market-neutral ({WHOLESALER_LEAKS.search(body).group(0)})")
    for d in desks_seen:
        name = d.split(" (")[0]
        if d != own_desk and len(name) > 2 and re.search(rf"\b{re.escape(name)}\b", body) and name not in own_desk:
            problems.append(f"names another desk ({d})")
    return problems


def make_card(cid: str, kind: str, channel: str, thread_ts: str, record: dict, info: dict) -> dict:
    """Card state, recipients decided here (code), never by the model."""
    test = watch.is_test_channel(channel)
    who = record.get("desk") or ("Customer" if record.get("party") == "customer" else record.get("party", "")).title()
    card = {"id": cid, "kind": kind, "channel": channel, "thread_ts": thread_ts, "status": "open", "test": test,
            "who": who, "party": record.get("party"), "summary": record.get("summary", ""), "at": record.get("at"),
            "subject": record.get("subject", ""), "gmail_thread_id": record.get("thread_id", ""),
            "customer": info.get("customer", "")}
    if kind == "quote":
        card["quote"] = record.get("quote") or {}
        card["source"] = record.get("source", "")
        card["bind"] = {"to": (TEST_EMAIL_ if test else record.get("from", "")), "body": (record.get("quote") or {}).get("bind_body", "")}
        return card
    via = record.get("reply_channel") or "none"
    body = record.get("reply_body", "")
    if record.get("party") == "customer" and via == "email_reply" and record.get("type") != "email":
        via = "sms" if info.get("contact_phone") else "email_reply"
    if via == "sms":
        to = watch.TEST_PHONE if test else ("+1" + info["contact_phone"] if info.get("contact_phone") else "")
    elif via == "email_reply":
        to = TEST_EMAIL_ if test else (record.get("from") if record.get("type") == "email" else info.get("contact_email", ""))
    else:
        to = ""
    if via == "email_reply" and not card["gmail_thread_id"]:
        card["subject"] = card["subject"] or f"{info.get('customer', '')} — following up"
    card.update(via=via, to=to, body=body)
    desks = (info.get("_desks_seen") or [])
    card["problems"] = copy_problems(body, via, record.get("party", ""), desks, record.get("desk", "")) if via != "none" else []
    if via != "none" and not to:
        card["problems"].append("no recipient on file")
    return card


TEST_EMAIL_ = watch.TEST_EMAIL


def card_text(card: dict) -> str:
    if card["kind"] == "quote":
        return f"Quote — {card.get('who')}: {card['quote'].get('carrier') or 'see quote'}"
    if card["kind"] == "draft":
        return f"{card.get('title', 'Draft')} ready in team@"
    return f"{card.get('who')}: {card.get('summary')}"


def _stamp_block(card: dict) -> dict | None:
    if card["status"] == "sent":
        text = f":white_check_mark: Sent by <@{card.get('by')}> {_when(card.get('done_at', time.time()))}"
    elif card["status"] == "dismissed":
        text = f"Dismissed by <@{card.get('by')}>"
    elif card.get("note"):
        text = card["note"]
    else:
        return None
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def card_blocks(card: dict) -> list[dict]:
    value = {"k": card["id"]}
    thread_btn = _btn("dcd_open", "Open thread", url=gmail_thread_link(card["gmail_thread_id"])) \
        if card.get("gmail_thread_id") and not card.get("test") else None
    if card["kind"] == "quote":
        q = card.get("quote") or {}
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f":moneybag: *Quote — {card.get('who')}*\n{q.get('carrier') or '_Carrier: see quote_'}"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Premium (all-in)*\n{q.get('premium') or '_see quote_'}"},
                {"type": "mrkdwn", "text": "*Status (per quote)*\n" + (q.get("admitted") or "_not stated_")},
                {"type": "mrkdwn", "text": "*Quote*\n" + (", ".join(a for a in q.get("attachments") or [] if a) or "_in email_")}]},
        ]
        if q.get("terms"):
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": "*Key terms*\n" + "\n".join(f"• {t}" for t in q["terms"])}})
        if q.get("subjectivities"):
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": "*Subjectivities*\n" + "\n".join(f"• {t}" for t in q["subjectivities"])}})
        elements = [_btn("dcd_present", "Draft customer quote", value, style="primary")]
        if (card.get("bind") or {}).get("body") and not card.get("bind_draft"):
            elements.append(_btn("dcd_bind", "Ask to bind (draft)", value))
        if card.get("bind_draft"):
            elements.append(_btn("dcd_gmail_bind", "Open bind draft", url=_skill("slack_ui").draft_link(card["bind_draft"])))
        if thread_btn:
            elements.append(thread_btn)
        blocks.append({"type": "actions", "elements": elements})
        if (stamp := _stamp_block(card)):
            blocks.append(stamp)
        return blocks

    if card["kind"] == "draft":
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f":page_facing_up: *{card.get('title', 'Draft')}*"}},
                  {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*To*\n{card.get('to')}"},
                                                 {"type": "mrkdwn", "text": f"*Subject*\n{card.get('subject') or '_none_'}"}]}]
        if card.get("body"):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _fence(card["body"])}})
        if card["status"] == "open":
            blocks.append({"type": "actions", "elements": [
                _btn("dcd_send", "Send email", value, style="primary",
                     confirm=_confirm("Send this email?", f"Sends this draft to {card.get('to')} from team@ now.", "Send")),
                _btn("dcd_gmail_draft", "Open draft", url=_skill("slack_ui").draft_link(card.get("message_id", ""))),
                _btn("dcd_dismiss", "Dismiss", value)]})
        if card.get("notes"):
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "*Internal only*\n" + "\n".join(
                f"• {n}" for n in card["notes"])[:2900]}]})
        if (stamp := _stamp_block(card)):
            blocks.append(stamp)
        return blocks

    icon = ":speech_balloon:" if card.get("party") == "customer" else ":incoming_envelope:"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"{icon} *{card.get('who')}* · {card.get('summary')}"}}]
    via = card.get("via", "none")
    if via != "none" and card.get("body"):
        label = f"Text → {_phone_label(card.get('to', ''))} from Daycare - Pranav" if via == "sms" \
            else f"Email reply → {card.get('to') or '_no address_'}"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Suggested* · {label}"}]})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _fence(card["body"])}})
    if card["status"] == "open":
        elements = []
        if via != "none" and card.get("body") and not card.get("problems"):
            what = f"Texts {_phone_label(card.get('to', ''))}" if via == "sms" else f"Emails {card.get('to')} from team@"
            elements.append(_btn("dcd_send", "Send", value, style="primary",
                                 confirm=_confirm("Send this reply?", f"{what} now.", "Send")))
        if via != "none":
            elements.append(_btn("dcd_edit", "Edit", value))
        elements.append(_btn("dcd_dismiss", "Dismiss", value))
        if thread_btn:
            elements.append(thread_btn)
        blocks.append({"type": "actions", "elements": elements})
        if card.get("problems"):
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                           "text": ":warning: Fix before sending: " + "; ".join(card["problems"])}]})
    if (stamp := _stamp_block(card)):
        blocks.append(stamp)
    return blocks


# ── click handlers ────────────────────────────────────────────────────────────

def _respond(payload: dict, text: str) -> None:
    url = payload.get("response_url", "")
    if not url:
        return
    body = json.dumps({"text": text, "response_type": "ephemeral", "replace_original": False}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10)
    except Exception:
        pass


def _card(cid: str) -> dict:
    return (watch.read_state().get("cards") or {}).get(cid) or {}


def _save_card(card: dict) -> None:
    with watch.state() as s:
        s["cards"][card["id"]] = card


def _render(ctx, card: dict) -> None:
    try:
        ctx.web.chat_update(channel=card["channel"], ts=card["ts"], text=card_text(card), blocks=card_blocks(card))
    except Exception as e:  # noqa: BLE001
        ctx.log.info(f"DAYCARE CARD render failed {card.get('id')}: {e}")


_card_locks: dict[str, threading.Lock] = {}
_claims: dict[str, float] = {}


def _claim(key: str, window: float = 60) -> bool:
    now = time.time()
    with _parents_lock:
        if now - _claims.get(key, 0) < window:
            return False
        _claims[key] = now
        return True


def handle_block_action(ctx, payload: dict) -> bool:
    actions = payload.get("actions") or []
    if not actions:
        return False
    aid = str(actions[0].get("action_id", ""))
    if not aid.startswith("dcd_"):
        return False
    if aid in ("dcd_open", "dcd_gmail_bind", "dcd_gmail_draft"):
        return True  # URL buttons
    clicker = (payload.get("user") or {}).get("id", "")
    if clicker not in ctx.writers:
        _respond(payload, "Only Soma team members can do that.")
        return True
    try:
        cid = json.loads(actions[0].get("value") or "{}").get("k", "")
    except ValueError:
        cid = ""
    card = _card(cid)
    if not card:
        _respond(payload, "That card is no longer tracked — nothing done.")
        return True
    if aid == "dcd_edit":
        _open_edit(ctx, payload, card)
    elif aid == "dcd_dismiss":
        if card["status"] == "open":
            card.update(status="dismissed", by=clicker, done_at=time.time())
            _save_card(card)
            _render(ctx, card)
    elif aid == "dcd_send":
        if card["status"] != "open" or not _claim(f"send:{cid}"):
            _respond(payload, "Those buttons are spent — this was already handled.")
            return True
        card["note"] = ":hourglass_flowing_sand: Sending…"
        _render(ctx, {**card, "status": "locked"})
        threading.Thread(target=_send, args=(ctx, payload, cid, clicker), daemon=True).start()
    elif aid == "dcd_present":
        if not _claim(f"present:{cid}", 120):
            _respond(payload, "Already drafting the customer quote.")
            return True
        _present(ctx, card, clicker, payload)
    elif aid == "dcd_bind":
        if not _claim(f"bind:{cid}", 120):
            return True
        threading.Thread(target=_bind_draft, args=(ctx, payload, cid, clicker), daemon=True).start()
    return True


def _send(ctx, payload: dict, cid: str, clicker: str) -> None:
    lock = _card_locks.setdefault(cid, threading.Lock())
    with lock:
        card = _card(cid)
        try:
            res = send_card(card)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if res.get("ok"):
            card.update(status="sent", by=clicker, done_at=time.time(), note="", sent_ref=res.get("ref", ""))
            _save_card(card)
            _render(ctx, card)
            target = _phone_label(card["to"]) if card.get("via") == "sms" else card.get("to")
            try:
                ctx.web.chat_postMessage(channel=card["channel"], thread_ts=card["thread_ts"],
                                         text=f"{'Texted' if card.get('via') == 'sms' else 'Emailed'} {target}. Approved by <@{clicker}>.")
            except Exception:
                pass
        else:
            card["note"] = ""
            _save_card(card)
            _render(ctx, card)
            with _parents_lock:
                _claims.pop(f"send:{cid}", None)
            _respond(payload, f"Not sent — {res.get('error')}")
        ctx.log.info(f"DAYCARE CARD SEND {cid} by {clicker}: " + (f"sent {res.get('ref', '')}" if res.get("ok")
                     else f"failed: {res.get('error')}"))


def _recipients_ok(card: dict, live_to: str) -> bool:
    ui = _skill("slack_ui")
    got = ui._norm(live_to)
    want = ui._norm(card.get("to", ""))
    if card.get("test") and got - {watch.TEST_EMAIL}:
        return False
    return bool(got) and got <= want


def send_card(card: dict) -> dict:
    """Send exactly what the card shows. Recipients re-verified on the live draft."""
    ui = _skill("slack_ui")
    if card.get("test") and card.get("to") not in (watch.TEST_EMAIL, watch.TEST_PHONE):
        return {"ok": False, "error": "test card with a non-test recipient — refused"}
    if card["kind"] == "draft":
        live = ui.readback(card["draft_id"])
        if not live:
            return {"ok": False, "error": "draft no longer in team@ — already sent or deleted"}
        if not _recipients_ok(card, live.get("to", "")):
            return {"ok": False, "error": f"recipient changed in Gmail ({live.get('to')}) — send from Gmail"}
        res = ui.gmail("send-draft", card["draft_id"])
        return _result(res.get("id"), res.get("error"))
    if card.get("problems"):
        return {"ok": False, "error": "; ".join(card["problems"])}
    body = (card.get("body") or "").strip()
    if card.get("via") == "sms":
        res = ui.quo_send(QUO_LINE, card["to"], body)
        return _result(res.get("id") if res.get("ok") else "", res.get("error"))
    if card.get("via") != "email_reply" or not card.get("to"):
        return {"ok": False, "error": "nothing to send"}
    if card.get("gmail_thread_id") and not card.get("test"):
        made = ui.gmail("reply", card["gmail_thread_id"], card["to"], body)
    else:
        subject = ("TEST — " if card.get("test") else "") + (card.get("subject") or "Following up")
        made = ui.gmail("draft", card["to"], subject if subject.lower().startswith(("re:", "test")) else subject, body)
    draft_id = made.get("draftId") or made.get("id")
    if not draft_id:
        return {"ok": False, "error": f"draft not created: {made.get('error', '')}"}
    live = ui.readback(draft_id)
    if not live or not _recipients_ok(card, live.get("to", "")):
        ui.gmail("delete-draft", draft_id)
        return {"ok": False, "error": "draft recipient mismatch — nothing sent"}
    res = ui.gmail("send-draft", draft_id)
    return _result(res.get("id"), res.get("error"))


def _result(ref, error) -> dict:
    """ok ⇔ a provider id came back; an error string only on failure."""
    return {"ok": True, "ref": ref} if ref else {"ok": False, "error": error or "send failed"}


def _open_edit(ctx, payload: dict, card: dict) -> None:
    if card["status"] != "open" or card.get("via", "none") == "none":
        _respond(payload, "Nothing to edit.")
        return
    limit = 480 if card.get("via") == "sms" else 2900
    label = f"Text to {_phone_label(card['to'])}" if card.get("via") == "sms" else f"Email to {card.get('to')}"
    view = {"type": "modal", "callback_id": "dcd_edit_submit",
            "title": {"type": "plain_text", "text": "Edit reply"},
            "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({"k": card["id"]}),
            "blocks": [{"type": "input", "block_id": "body", "label": {"type": "plain_text", "text": label[:150]},
                        "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                                    "initial_value": (card.get("body") or "")[:limit], "max_length": limit}}]}
    try:
        ctx.web.views_open(trigger_id=payload.get("trigger_id"), view=view)
    except Exception as e:  # noqa: BLE001
        _respond(payload, f"Couldn't open the editor: {e}")


def handle_view_submission(ctx, payload: dict) -> bool:
    view = payload.get("view") or {}
    if view.get("callback_id") != "dcd_edit_submit":
        return False
    user = (payload.get("user") or {}).get("id", "")
    if user not in ctx.writers:
        return True
    cid = json.loads(view.get("private_metadata") or "{}").get("k", "")
    body = ((((view.get("state") or {}).get("values") or {}).get("body") or {}).get("v") or {}).get("value") or ""
    card = _card(cid)
    if not card or card["status"] != "open":
        return True
    card["body"] = body.strip()
    th = watch.read_state()["threads"].get(watch.thread_key(card["channel"], card["thread_ts"])) or {}
    card["problems"] = copy_problems(card["body"], card["via"], card.get("party", ""),
                                     (th.get("last_status") or {}).get("sent") or [], card.get("who", ""))
    if not card.get("to"):
        card["problems"].append("no recipient on file")
    card["edited_by"] = user
    _save_card(card)
    _render(ctx, card)
    return True


def _present(ctx, card: dict, clicker: str, payload: dict | None = None) -> None:
    """Draft customer quote: quote-present's renderer + checks, drafted via team@ (quote_present_desk)."""
    card["note"] = f":hourglass_flowing_sand: Drafting the customer quote for <@{clicker}>…"
    _save_card(card)
    _render(ctx, card)
    threading.Thread(target=_present_run, args=(ctx, card["id"], clicker, payload or {}), daemon=True).start()


def _present_run(ctx, cid: str, clicker: str, payload: dict) -> None:
    import quote_present_desk as qpd
    card = _card(cid)
    th = watch.read_state()["threads"].get(watch.thread_key(card["channel"], card["thread_ts"])) or {}
    try:
        res = qpd.draft_customer_quote(card, th.get("info") or {})
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    card = _card(cid)
    if res.get("ok"):
        posted = post_draft_card(res["draft_id"], card["channel"], card["thread_ts"], "Customer quote",
                                 web=ctx.web, notes=res.get("flags") or [])
        card["note"] = (f"Customer quote drafted for <@{clicker}> in {res.get('seconds', '?')}s — review the card below"
                        if posted.get("ok") else f":warning: Draft made but its card failed: {posted.get('error')}")
    else:
        card["note"] = f":warning: Customer quote not drafted — {res.get('error')}"
        with _parents_lock:
            _claims.pop(f"present:{cid}", None)
    _save_card(card)
    _render(ctx, card)
    ctx.log.info(f"DAYCARE PRESENT {cid} by {clicker}: " + (f"draft {res.get('draft_id')} in {res.get('seconds')}s"
                 if res.get("ok") else f"failed: {res.get('error')}"))


def _bind_draft(ctx, payload: dict, cid: str, clicker: str) -> None:
    ui = _skill("slack_ui")
    card = _card(cid)
    bind = card.get("bind") or {}
    if not bind.get("body") or not bind.get("to"):
        _respond(payload, "No bind request text for this quote.")
        return
    if card.get("test"):
        made = ui.gmail("draft", watch.TEST_EMAIL, f"TEST — bind request — {card.get('customer')}", bind["body"])
    else:
        made = ui.gmail("reply", card["gmail_thread_id"], bind["to"], bind["body"])
    draft_id = made.get("draftId") or made.get("id")
    if not draft_id:
        _respond(payload, f"Draft not created: {made.get('error', '')}")
        return
    live = ui.readback(draft_id)
    card["bind_draft"] = live.get("message_id") or made.get("messageId") or ""
    card["note"] = f"Bind request drafted in team@ by <@{clicker}> — review and send from Gmail"
    _save_card(card)
    _render(ctx, card)
    ctx.log.info(f"DAYCARE BIND DRAFT {cid} by {clicker}: {draft_id}")


# ── draft-card CLI (for workers) ──────────────────────────────────────────────

class _CliWeb:
    """Minimal WebClient stand-in for the CLI path (posts as Loma)."""

    def chat_postMessage(self, **kw):  # noqa: N802
        return _skill("slack_ui").slack("chat.postMessage", kw)

    def chat_update(self, **kw):  # noqa: N802
        return _skill("slack_ui").slack("chat.update", kw)


def post_draft_card(draft_id: str, channel: str, thread_ts: str, title: str = "Draft", web=None,
                    notes: list[str] | None = None) -> dict:
    ui = _skill("slack_ui")
    live = ui.readback(draft_id)
    if not live:
        return {"ok": False, "error": "draft not readable in team@"}
    test = watch.is_test_channel(channel)
    if test and ui._norm(live.get("to", "")) - {watch.TEST_EMAIL}:
        return {"ok": False, "error": f"test channel: draft goes to {live.get('to')}, not pranav@ — refused"}
    cid = hashlib.sha1(f"draft|{draft_id}".encode()).hexdigest()[:16]
    card = {"id": cid, "kind": "draft", "channel": channel, "thread_ts": thread_ts, "status": "open", "test": test,
            "title": title, "draft_id": draft_id, "message_id": live.get("message_id", ""), "to": live.get("to", ""),
            "subject": live.get("subject", ""), "body": live.get("body", ""), "notes": list(notes or [])[:8]}
    res = (web or _CliWeb()).chat_postMessage(channel=channel, thread_ts=thread_ts, text=card_text(card),
                                              blocks=card_blocks(card), unfurl_links=False)
    card["ts"] = res["ts"]
    _save_card(card)
    return {"ok": True, "card_ts": card["ts"]}


def _cli() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("draft-card")
    p.add_argument("--draft-id", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--thread-ts", required=True)
    p.add_argument("--title", default="Draft")
    a = ap.parse_args()
    out = post_draft_card(a.draft_id, a.channel, a.thread_ts, a.title)
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_cli())
