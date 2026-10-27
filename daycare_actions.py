"""Daycare submission buttons + "Add inputs" modal for Loma.

Wired from socket_daemon.on_request. The heavy lifting lives in the
daycare-submit skill's slack_ui.py so the same code runs from the CLI; this
module only routes Slack interactions, enforces who may do what, and keeps
clicks from running twice.
"""

from __future__ import annotations

import json
import re
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime

# Loma's own workspace (CLAUDE_WORKSPACE) is not the work tree; the skill lives in ~/work.
WORK_ROOT = os.environ.get("SOMA_WORK_ROOT", os.path.expanduser("~/work"))
SKILL_DIR = os.environ.get("DAYCARE_SUBMIT_DIR", os.path.join(WORK_ROOT, "customers/.agents/skills/daycare-submit"))
SLACK_UI = os.path.join(SKILL_DIR, "slack_ui.py")
HOME = os.path.expanduser("~")
RUN_ENV = {
    **os.environ,
    "PATH": f"{HOME}/.bun/bin:{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
}

_busy: dict[str, float] = {}
_busy_lock = threading.Lock()
_send_locks: dict[str, threading.Lock] = {}


class Ctx:
    """What the daemon hands us: Slack client, logger, dispatcher, write users."""

    def __init__(self, web, log, dispatch, writers: set[str], bot_user_id: str):
        self.web, self.log, self.dispatch = web, log, dispatch
        self.writers, self.bot_user_id = writers, bot_user_id


def _respond(payload: dict, text: str) -> None:
    url = payload.get("response_url", "")
    if not url:
        return
    body = json.dumps({"text": text, "response_type": "ephemeral", "replace_original": False}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10)
    except Exception:
        pass


def _tell(ctx, payload: dict, user: str, channel: str, thread_ts: str, text: str) -> None:
    """Ephemeral note to the clicker — via response_url for buttons, API for modals."""
    if payload.get("response_url"):
        _respond(payload, text)
        return
    try:
        ctx.web.chat_postEphemeral(channel=channel, user=user, thread_ts=thread_ts, text=text)
    except Exception as e:
        ctx.log.info(f"DAYCARE ephemeral failed: {e}")


def _ui(*args: str, timeout: int = 900) -> dict:
    env = RUN_ENV
    if "--user" in args:
        # Every caller has already checked the clicker is a full Soma member (is_writer);
        # slack_ui honours this instead of its owner-only config list.
        env = {**RUN_ENV, "LOMA_VERIFIED_WRITER": args[args.index("--user") + 1]}
    try:
        proc = subprocess.run(["/usr/bin/python3", SLACK_UI, *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-200:]}


def _claim(key: str, window: float = 30.0) -> bool:
    """One run per (button, card) at a time — swallows double clicks."""
    now = time.time()
    with _busy_lock:
        if now - _busy.get(key, 0) < window:
            return False
        _busy[key] = now
        return True


def _release(key: str) -> None:
    with _busy_lock:
        _busy.pop(key, None)


def _where(payload: dict) -> tuple[str, str]:
    channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id", "")
    msg = payload.get("message") or {}
    thread_ts = msg.get("thread_ts") or msg.get("ts") or (payload.get("container") or {}).get("message_ts", "")
    return channel, thread_ts


def _manifest(path: str) -> dict:
    full = path if os.path.isabs(path) else os.path.join(WORK_ROOT, path)
    with open(full) as f:
        return json.load(f)


# ── block_actions ─────────────────────────────────────────────────────────────

def handle_block_action(ctx: Ctx, payload: dict) -> bool:
    """Returns True when the click belonged to a daycare card."""
    actions = payload.get("actions") or []
    if not actions:
        return False
    action = actions[0]
    aid = str(action.get("action_id", ""))
    if not aid.startswith("dcs_"):
        return False
    if aid.startswith("dcs_gmail"):
        return True  # URL button; Slack opens the link itself
    clicker = (payload.get("user") or {}).get("id", "")
    try:
        raw = action.get("value") or (action.get("selected_option") or {}).get("value") or "{}"
        value = json.loads(raw)
    except json.JSONDecodeError:
        _respond(payload, "Malformed button — nothing done.")
        return True
    manifest = value.get("m", "")
    broker = value.get("b", "")
    channel, thread_ts = _where(payload)

    if aid.startswith("dcs_run_"):
        customer = str(value.get("c", ""))[:120]
        if clicker not in ctx.writers:
            _respond(payload, "Only Soma team members can run Loma on a submission.")
        elif aid == "dcs_run_notes":
            _open_run_notes(ctx, payload, channel, thread_ts, customer)
        elif aid in RUN_ACTIONS:
            card_ts = (payload.get("message") or {}).get("ts", "")
            _start_run(ctx, payload, RUN_ACTIONS[aid], "", clicker, channel, thread_ts, card_ts, customer)
        return True
    if aid == "dcs_intake":
        threading.Thread(target=_intake, args=(ctx, payload, manifest, channel, thread_ts), daemon=True).start()
        return True
    if clicker not in ctx.writers:
        _respond(payload, "Only Soma team members can do that on a submission.")
        return True
    if aid in ("dcs_ask_sms", "dcs_ask_email", "dcs_ask_both"):
        vias = {"dcs_ask_sms": ["sms"], "dcs_ask_email": ["email"], "dcs_ask_both": ["email", "sms"]}[aid]
        if _strip_buttons(ctx, payload, channel):
            threading.Thread(target=_ask, args=(ctx, payload, manifest, vias, clicker, channel, thread_ts),
                             daemon=True).start()
    elif aid == "dcs_ask_edit":
        _open_editor(ctx, payload, manifest, "ask", channel, thread_ts)
    elif aid.startswith("dcs_edit:"):
        _open_editor(ctx, payload, manifest, broker, channel, thread_ts)
    elif aid.startswith("dcs_send"):
        if _strip_buttons(ctx, payload, channel, also=_pending_card_ts(manifest) if broker in ("", "all") else ()):
            threading.Thread(target=_send, args=(ctx, payload, manifest, broker or "all", clicker, channel, thread_ts),
                             daemon=True).start()
    elif aid == "dcs_inputs":
        _open_inputs(ctx, payload, manifest, channel, thread_ts)
    return True


def _pending_card_ts(manifest: str) -> list[str]:
    return [d["card_ts"] for d in _safe_manifest(manifest).get("drafts") or []
            if d.get("card_ts") and d.get("draft_id") and d.get("status") != "sent"]


def _strip_buttons(ctx, payload: dict, channel: str, also=()) -> bool:
    """Remove the clicked card's buttons before anything is sent.

    Two racing clicks race on this chat.update and only the first finds buttons
    to remove; the second is told the card is spent. Same ordering as Angie's
    COI approval. `also` strips other cards a Send-all covers.
    """
    message = payload.get("message") or {}
    ts = message.get("ts") or (payload.get("container") or {}).get("message_ts", "")
    blocks = message.get("blocks") or []
    if not any(b.get("type") == "actions" for b in blocks):
        _respond(payload, "Those buttons are spent — this was already sent.")
        return False
    kept = [b for b in blocks if b.get("type") != "actions"]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ":hourglass_flowing_sand: Sending…"}]})
    try:
        ctx.web.chat_update(channel=channel, ts=ts, blocks=kept, text=message.get("text") or "Sending")
    except Exception as e:
        ctx.log.info(f"DAYCARE strip failed: {e}")
        _respond(payload, "Couldn't lock the card for sending — nothing was sent. Try again.")
        return False
    thread_ts = message.get("thread_ts") or ts
    for other in also:
        if other == ts:
            continue
        try:
            # Desk cards are thread replies, which only conversations.replies returns.
            found = next((m for m in (ctx.web.conversations_replies(
                channel=channel, ts=thread_ts, oldest=other, latest=other, inclusive=True, limit=2
            ).get("messages") or []) if m.get("ts") == other), None)
            if found:
                ctx.web.chat_update(channel=channel, ts=other, text=found.get("text") or "Sending",
                                    blocks=[b for b in found.get("blocks") or [] if b.get("type") != "actions"])
        except Exception as e:
            ctx.log.info(f"DAYCARE strip (send all) {other}: {e}")
    return True


def _restore(manifest: str) -> None:
    """Re-render every card from the manifest: unsent drafts get their buttons back."""
    _ui("card", "--manifest", manifest, timeout=300)


def _send(ctx, payload, manifest, broker, clicker, channel, thread_ts):
    lock = _send_locks.setdefault(manifest, threading.Lock())
    with lock:
        try:
            res = _ui("send", "--manifest", manifest, "--broker", broker, "--user", clicker)
            sent = [r for r in res.get("results") or [] if r.get("ok")]
            bad = [r for r in res.get("results") or [] if not r.get("ok")]
            names = {d.get("broker"): d.get("name") or d.get("broker")
                     for d in (_safe_manifest(manifest).get("drafts") or [])}
            if sent:
                ctx.web.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                         text="Submission sent to " + ", ".join(names.get(r["broker"], r["broker"]) for r in sent)
                                         + f". Approved by <@{clicker}>.")
            if bad or not res.get("results"):
                _restore(manifest)
                msg = "; ".join(f"{names.get(r['broker'], r['broker'])}: {r.get('error')}" for r in bad) or res.get("error", "nothing sent")
                _tell(ctx, payload, clicker, channel, thread_ts, f"Not sent — {msg}")
            ctx.log.info(f"DAYCARE SEND {broker} {manifest} by {clicker}: {len(sent)} sent, {len(bad)} failed")
        except Exception as e:
            ctx.log.info(f"DAYCARE SEND error: {e}")
            _restore(manifest)
            _tell(ctx, payload, clicker, channel, thread_ts, f"Send failed: {e}")


def _ask(ctx, payload, manifest, vias, clicker, channel, thread_ts, text=None):
    lock = _send_locks.setdefault(f"ask:{manifest}", threading.Lock())
    with lock:
        done, errors = [], []
        for via in vias:
            args = ["ask", "--manifest", manifest, "--via", via, "--user", clicker]
            if text and via == "sms":
                args += ["--text", text]
            res = _ui(*args, timeout=180)
            if res.get("ok"):
                done.append(("Emailed" if via == "email" else "Texted") + (f" ⚠️ {res['warning']}" if res.get("warning") else ""))
            else:
                errors.append(f"{'email' if via == 'email' else 'text'}: {res.get('error', 'failed')}")
        if done:
            ctx.web.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                     text=f"{' and '.join(done)} the customer. Approved by <@{clicker}>.")
        if errors:
            _restore(manifest)
            _tell(ctx, payload, clicker, channel, thread_ts, "Not sent — " + "; ".join(errors))
        ctx.log.info(f"DAYCARE ASK {'+'.join(vias)} {manifest} by {clicker}: done={len(done)} errors={len(errors)}")


def _open_editor(ctx, payload, manifest, target, channel, thread_ts):
    m = _safe_manifest(manifest)
    if target == "ask":
        ask = m.get("customer_ask") or {}
        email, sms = ask.get("email") or {}, ask.get("sms") or {}
        body = ((email.get("view") or {}).get("body") or email.get("text") or "") if email.get("status") != "sent" else ""
        blocks = []
        if body:
            blocks.append(_text_input("body", "Email body", body, 2900))
        if sms.get("text") and sms.get("status") != "sent":
            digits = re.sub(r"\D", "", sms.get("to", ""))[-10:]
            to = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else sms.get("to", "")
            blocks.append(_text_input("sms", f"Quo text to {to}", sms["text"], 480))
        title = "Edit customer ask"
    else:
        d = next((x for x in m.get("drafts") or [] if x.get("broker") == target), {})
        body = (d.get("view") or {}).get("body") or ""
        blocks = [_text_input("body", "Email body", body, 2900)] if body else []
        title = "Edit submission"
    if not blocks:
        _respond(payload, "Nothing left to edit — it was already sent.")
        return
    view = {
        "type": "modal", "callback_id": "dcs_edit_submit",
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({"m": manifest, "target": target, "c": channel, "t": thread_ts}),
        "blocks": blocks,
    }
    try:
        ctx.web.views_open(trigger_id=payload.get("trigger_id"), view=view)
    except Exception as e:
        ctx.log.info(f"DAYCARE edit modal failed: {e}")
        _respond(payload, f"Couldn't open the editor: {e}")


def _text_input(block_id: str, label: str, value: str, limit: int) -> dict:
    return {"type": "input", "block_id": block_id, "label": {"type": "plain_text", "text": label[:150]},
            "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                        "initial_value": value[:limit], "max_length": limit}}


def _save_edit(ctx, manifest, target, body, sms, user, channel, thread_ts):
    args = ["edit", "--manifest", manifest, "--target", target, "--body", body or ""]
    if sms is not None:
        args += ["--sms", sms]
    res = _ui(*args, timeout=300)
    if not res.get("ok"):
        _tell(ctx, {}, user, channel, thread_ts, f"Edit not saved — {res.get('error', 'unknown error')}")
    ctx.log.info(f"DAYCARE EDIT {target} {manifest} by {user}: {res.get('ok')}")


def _safe_manifest(manifest: str) -> dict:
    try:
        return _manifest(manifest)
    except Exception:
        return {}


def _intake(ctx, payload, manifest, channel, thread_ts):  # noqa: D103
    key = f"intake:{manifest}"
    if not _claim(key, 20):
        return
    try:
        m = _safe_manifest(manifest)
        md = os.path.join(m.get("customer_dir", ""), "client-intake.md")
        if not os.path.exists(md):
            _respond(payload, "No client-intake.md for this customer yet.")
            return
        res = _ui("intake", "--md", md, "--channel", channel, "--thread-ts", thread_ts)
        if not res.get("ok"):
            _respond(payload, f"Intake upload failed: {res.get('error')}")
    finally:
        _release(key)


# ── "Add inputs" modal ────────────────────────────────────────────────────────

LOSS_OPTIONS = [
    ("attached", "Attached in this thread"),
    ("requested", "Requested from prior carrier"),
    ("first_policy", "First policy — none exist"),
]


def _opt(value: str, text: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value}


def _broker_options() -> list[dict]:
    try:
        with open(os.path.join(SKILL_DIR, "brokers.json")) as f:
            cfg = json.load(f)
    except Exception:
        return []
    return [_opt(k, v.get("name", k)) for k, v in cfg.items()
            if isinstance(v, dict) and not v.get("disabled")][:100]


def _input(block_id: str, label: str, element: dict, hint: str = "") -> dict:
    block = {"type": "input", "block_id": block_id, "optional": True,
             "label": {"type": "plain_text", "text": label}, "element": element}
    if hint:
        block["hint"] = {"type": "plain_text", "text": hint}
    return block


def inputs_view(manifest: str, channel: str, thread_ts: str) -> dict:
    m = _safe_manifest(manifest)
    current = [d.get("broker") for d in m.get("drafts") or [] if d.get("broker")]
    brokers = _broker_options()
    broker_el = {"type": "multi_static_select", "action_id": "v", "options": brokers,
                 "placeholder": {"type": "plain_text", "text": "Keep current desks"}}
    initial = [o for o in brokers if o["value"] in current]
    if initial:
        broker_el["initial_options"] = initial
    blocks = [
        _input("eff", "Effective date", {"type": "datepicker", "action_id": "v"}),
        _input("target", "Target premium", {"type": "plain_text_input", "action_id": "v",
                                            "placeholder": {"type": "plain_text", "text": "$3,000"}}),
        _input("prior", "Prior carrier / expiring", {"type": "plain_text_input", "action_id": "v",
               "placeholder": {"type": "plain_text", "text": "NEXT, $2,400, exp 10/27/2026"}}),
        _input("loss", "Loss runs", {"type": "static_select", "action_id": "v",
                                     "options": [_opt(v, t) for v, t in LOSS_OPTIONS]}),
        _input("brokers", "Desks", broker_el),
        _input("facts", "Corrections / new facts", {"type": "plain_text_input", "action_id": "v", "multiline": True,
               "placeholder": {"type": "plain_text", "text": "ADA 14 · ages 6 wks–5 yrs · no field trips · 1998 masonry, 2,400 sq ft"}}),
        _input("flags", "Options", {"type": "checkboxes", "action_id": "v", "options": [
            _opt("rush", "Rush"), _opt("hide_target", "Keep target premium out of the email")]}),
    ]
    title = (m.get("customer") or "Submission")[:24]
    return {
        "type": "modal", "callback_id": "dcs_inputs",
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Apply"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({"m": manifest, "c": channel, "t": thread_ts}),
        "blocks": blocks,
    }


def _open_inputs(ctx, payload, manifest, channel, thread_ts):
    try:
        ctx.web.views_open(trigger_id=payload.get("trigger_id"), view=inputs_view(manifest, channel, thread_ts))
    except Exception as e:
        ctx.log.info(f"DAYCARE inputs modal failed: {e}")
        _respond(payload, f"Couldn't open the form: {e}")


def _value(state: dict, block: str):
    el = ((state.get(block) or {}).get("v") or {})
    t = el.get("type")
    if t == "datepicker":
        d = el.get("selected_date")
        return datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d/%Y") if d else ""
    if t == "plain_text_input":
        return (el.get("value") or "").strip()
    if t == "static_select":
        return ((el.get("selected_option") or {}).get("value")) or ""
    if t in ("multi_static_select", "checkboxes"):
        return [o.get("value") for o in el.get("selected_options") or []]
    return ""


def summarize_inputs(state: dict, manifest: dict) -> list[str]:
    """Human-readable bullets for what was entered; empty fields are skipped."""
    out = []
    loss_text = dict(LOSS_OPTIONS)
    if _value(state, "eff"):
        out.append(f"Effective {_value(state, 'eff')}")
    if _value(state, "target"):
        out.append(f"Target premium {_value(state, 'target')}")
    if _value(state, "prior"):
        out.append(f"Prior: {_value(state, 'prior')}")
    if _value(state, "loss"):
        out.append(f"Loss runs: {loss_text.get(_value(state, 'loss'), _value(state, 'loss'))}")
    chosen = _value(state, "brokers") or []
    current = sorted(d.get("broker") for d in manifest.get("drafts") or [] if d.get("broker"))
    if chosen and sorted(chosen) != current:
        out.append("Desks: " + ", ".join(chosen))
    flags = _value(state, "flags") or []
    if "rush" in flags:
        out.append("Rush")
    if "hide_target" in flags:
        out.append("Keep target premium out of the email")
    if _value(state, "facts"):
        out.append(_value(state, "facts"))
    return out


def handle_view_submission(ctx: Ctx, payload: dict) -> bool:
    view = payload.get("view") or {}
    if view.get("callback_id") == "dcs_edit_submit":
        user = (payload.get("user") or {}).get("id", "")
        if user not in ctx.writers:
            return True
        meta = json.loads(view.get("private_metadata") or "{}")
        values = ((view.get("state") or {}).get("values") or {})
        body = ((values.get("body") or {}).get("v") or {}).get("value")
        sms = ((values.get("sms") or {}).get("v") or {}).get("value") if "sms" in values else None
        threading.Thread(target=_save_edit, args=(ctx, meta.get("m", ""), meta.get("target", ""), body, sms, user,
                                                  meta.get("c", ""), meta.get("t", "")), daemon=True).start()
        return True
    if view.get("callback_id") == "dcs_run_submit":
        user = (payload.get("user") or {}).get("id", "")
        if user not in ctx.writers:
            return True
        meta = json.loads(view.get("private_metadata") or "{}")
        values = (view.get("state") or {}).get("values") or {}
        notes = (((values.get("notes") or {}).get("v") or {}).get("value") or "").strip()
        mode = (((values.get("mode") or {}).get("v") or {}).get("selected_option") or {}).get("value") or "both"
        _start_run(ctx, {}, mode if mode in RUN_MODES else "both", notes, user,
                   meta.get("c", ""), meta.get("t", ""), meta.get("card", ""), meta.get("cust", ""))
        return True
    if view.get("callback_id") != "dcs_inputs":
        return False
    user = (payload.get("user") or {}).get("id", "")
    if user not in ctx.writers:
        return True
    meta = json.loads(view.get("private_metadata") or "{}")
    manifest_path, channel, thread_ts = meta.get("m", ""), meta.get("c", ""), meta.get("t", "")
    manifest = _safe_manifest(manifest_path)
    items = summarize_inputs((view.get("state") or {}).get("values") or {}, manifest)
    if not items:
        return True
    customer = manifest.get("customer", "this customer")
    bullets = "\n".join(f"• {i}" for i in items)
    # `text` is what Loma reads as the turn; the blocks are what people see.
    instruction = (
        f"<@{ctx.bot_user_id}> Inputs from <@{user}> for {customer}:\n{bullets}\n"
        f"Apply them to the intake ({manifest.get('intake_path', '')}), refill the forms, and regenerate the "
        f"drafts with --replace. Keep anything negative or unconfirmed out of the emails. Then refresh the "
        f"submission card (slack_ui.py card --manifest {manifest_path}). Reply in two lines at most."
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Inputs* · <@{user}>\n{bullets}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Updating intake and drafts…"}]},
    ]
    try:
        res = ctx.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=instruction, blocks=blocks)
        ts = res["ts"]
    except Exception as e:
        ctx.log.info(f"DAYCARE inputs post failed: {e}")
        return True
    ctx.log.info(f"DAYCARE INPUTS for {customer} by {user}: {len(items)} item(s) → worker on {ts}")
    ctx.dispatch(channel, ts, user, instruction.replace("\n", " ")[:300], thread_ts,
                 "", False, instruction, False)
    return True


# ── Run buttons on Angie's submission cards ───────────────────────────────────
# Angie posts every Hub submission to #daycare-submission. Loma answers in that
# thread with buttons that do exactly what "@Loma /daycare-intake and
# /daycare-submit" does, so nobody has to type it.

SUBMISSION_CHANNEL = os.environ.get("DAYCARE_SUBMISSION_CHANNEL", "C0BFP2X1JCF")
# Angie Slack app rebuilt 2026-10-22 (new user U0CBAL1JRB6); keep the old id for history.
ANGIE_USER_IDS = set(os.environ.get("ANGIE_SLACK_USER_IDS", "U0CBAL1JRB6,U09QG7Y6X1C").split(","))
RUN_CARD_DELAY_SECONDS = 8  # let Angie's summary and intake file land first
RUN_MODES = {
    "both": ("Intake + submit", "/daycare-intake and /daycare-submit"),
    "intake": ("Intake", "/daycare-intake"),
    # Desk card "Submit": reuse a fresh intake instead of rebuilding it.
    "submit": ("Submit", "/daycare-submit (run /daycare-intake first only if this customer has no "
                         "client-intake.md from the last 7 days)"),
}
RUN_ACTIONS = {"dcs_run_both": "both", "dcs_run_intake": "intake", "dcs_run_full": "submit"}
_run_cards: set[str] = set()


def is_submission_card(evt: dict) -> bool:
    return (
        evt.get("type") == "message"
        and evt.get("channel") == SUBMISSION_CHANNEL
        and evt.get("user") in ANGIE_USER_IDS
        and not evt.get("subtype")
        and evt.get("thread_ts") in (None, "", evt.get("ts"))
        and "made the submission for" in (evt.get("text") or "")
    )


def run_card_blocks(note: str = "") -> list[dict]:
    blocks: list[dict] = [{
        "type": "actions",
        "block_id": "dcs_run",
        "elements": [
            {"type": "button", "action_id": "dcs_run_both", "style": "primary",
             "text": {"type": "plain_text", "text": "Run intake + submit"}, "value": "{}"},
            {"type": "button", "action_id": "dcs_run_intake",
             "text": {"type": "plain_text", "text": "Intake only"}, "value": "{}"},
            {"type": "button", "action_id": "dcs_run_notes",
             "text": {"type": "plain_text", "text": "Run with notes…"}, "value": "{}"},
        ],
    }]
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return blocks


def post_run_buttons(ctx, evt: dict) -> None:
    """Called for every channel message; acts only on a new Angie submission card.

    Channels with the submission desk on (DAYCARE_DESK_CHANNELS) get the desk card
    from daycare_desk.on_parent instead.
    """
    import daycare_watch
    if daycare_watch.is_desk_channel(evt.get("channel", "")) or not is_submission_card(evt):
        return
    ts = evt.get("ts", "")
    with _busy_lock:
        if ts in _run_cards:
            return
        _run_cards.add(ts)
    threading.Timer(RUN_CARD_DELAY_SECONDS, _post_run_card, args=(ctx, evt.get("channel", ""), ts)).start()


def _post_run_card(ctx, channel: str, thread_ts: str) -> None:
    try:
        replies = ctx.web.conversations_replies(channel=channel, ts=thread_ts, limit=50).get("messages") or []
        if any(m.get("user") == ctx.bot_user_id
               and any(b.get("block_id") == "dcs_run" for b in m.get("blocks") or []) for m in replies):
            return
        ctx.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Run Loma on this submission",
                                 blocks=run_card_blocks())
        ctx.log.info(f"DAYCARE RUN CARD posted in {channel}/{thread_ts}")
    except Exception as e:
        ctx.log.info(f"DAYCARE RUN CARD failed for {channel}/{thread_ts}: {e}")


def _open_run_notes(ctx, payload: dict, channel: str, thread_ts: str, customer: str = "") -> None:
    import daycare_watch
    desk = daycare_watch.is_desk_channel(channel)
    modes = ["submit", "intake"] if desk else ["both", "intake"]
    view = {
        "type": "modal", "callback_id": "dcs_run_submit",
        "title": {"type": "plain_text", "text": "Run Loma"},
        "submit": {"type": "plain_text", "text": "Run"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({"c": channel, "t": thread_ts, "cust": customer,
                                        "card": (payload.get("message") or {}).get("ts", "")}),
        "blocks": [
            {"type": "input", "block_id": "mode", "label": {"type": "plain_text", "text": "Run"},
             "element": {"type": "radio_buttons", "action_id": "v",
                         "initial_option": {"text": {"type": "plain_text", "text": RUN_MODES[modes[0]][0]},
                                            "value": modes[0]},
                         "options": [{"text": {"type": "plain_text", "text": RUN_MODES[k][0]}, "value": k}
                                     for k in modes]}},
            {"type": "input", "block_id": "notes", "optional": True,
             "label": {"type": "plain_text", "text": "Notes for Loma"},
             "element": {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 1500,
                         "placeholder": {"type": "plain_text",
                                         "text": "e.g. returned lead — reuse prior info; dates from next Monday"}}},
        ],
    }
    try:
        ctx.web.views_open(trigger_id=payload.get("trigger_id"), view=view)
    except Exception as e:
        ctx.log.info(f"DAYCARE run modal failed: {e}")
        _respond(payload, f"Couldn't open the notes box: {e}")


def _start_run(ctx, payload: dict, mode: str, notes: str, user: str, channel: str, thread_ts: str,
               card_ts: str, customer: str = "") -> None:
    if not channel or not thread_ts:
        return
    if not _claim(f"run:{channel}:{thread_ts}", 15):
        _tell(ctx, payload, user, channel, thread_ts, "Already starting a run in this thread.")
        return
    label, command = RUN_MODES[mode]
    instruction = (f"<@{ctx.bot_user_id}> {command}" + (f" — customer: {customer}" if customer else "")
                   + (f"\n{notes}" if notes else ""))
    visible = f":arrow_forward: {label} · <@{user}>" + (f"\n>{notes[:500]}" if notes else "")
    try:
        res = ctx.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=instruction,
                                       blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": visible}}])
    except Exception as e:
        ctx.log.info(f"DAYCARE RUN post failed: {e}")
        _tell(ctx, payload, user, channel, thread_ts, f"Couldn't start the run: {e}")
        return
    ctx.log.info(f"DAYCARE RUN {mode} by {user} in {channel}/{thread_ts} → worker on {res['ts']}")
    ctx.dispatch(channel, res["ts"], user, instruction.replace("\n", " ")[:300], thread_ts,
                 "", False, instruction, False)
    import daycare_desk
    if daycare_desk.record_run(ctx, channel, thread_ts, label, user):
        return
    if card_ts:
        try:
            ctx.web.chat_update(channel=channel, ts=card_ts, text="Run Loma on this submission",
                                blocks=run_card_blocks(f"Last run: {label} by <@{user}> "
                                                       f"<!date^{int(time.time())}^{{time}}|now>"))
        except Exception as e:
            ctx.log.info(f"DAYCARE RUN card update failed: {e}")
