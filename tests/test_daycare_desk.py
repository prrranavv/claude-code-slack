"""Daycare submission desk: triage filters, watcher cursors, status, card builders, action routing.

Run: cd ~/.claude/claude-slack-bot && uvx --with pytest pytest tests/ -q
No network: Slack, lead-chat, the Angie pool and wins search are stubbed.
"""
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ["DAYCARE_DESK_CHANNELS"] = "CTEST,CPROD"
os.environ["DAYCARE_TEST_CHANNELS"] = "CTEST"

import daycare_watch as watch  # noqa: E402
import daycare_desk as desk  # noqa: E402
import daycare_actions as actions  # noqa: E402

BOT = "UBOT"
ANGIE = "U0CBAL1JRB6"


class Web:
    def __init__(self):
        self.calls = []
        self.n = 0

    def __getattr__(self, name):
        def f(**kw):
            self.calls.append((name, kw))
            self.n += 1
            if name == "conversations_replies":
                return {"messages": []}
            if name == "users_lookupByEmail":
                return {"user": {"id": "UOWNER"}}
            return {"ts": f"200.{self.n:04d}"}
        return f

    def named(self, name):
        return [kw for n, kw in self.calls if n == name]


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)


class Writers:
    def __contains__(self, u):
        return u in {"UPRANAV", "UKINJAL"}


class Ctx:
    def __init__(self):
        self.web, self.log, self.bot_user_id = Web(), Log(), BOT
        self.writers = Writers()
        self.dispatched = []

    def dispatch(self, *a):
        self.dispatched.append(a)


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(watch, "load_desks", lambda path=None: _desks())
    desk._triaged.clear()
    desk._claims.clear()
    yield


def _desks():
    jake = {"key": "jake-rt", "short": "Jake (RT)", "org": "Jake (RT)"}
    hedge = {"key": "hedge", "short": "Hedge", "org": "Hedge"}
    luke = {"key": "hedge-luke", "short": "Luke (Hedge)", "org": "Hedge"}
    return {"email": {"jake.behuniak@rtspecialty.com": jake, "submissions@hedgespecialty.com": hedge,
                      "lr@hedgespecialty.com": luke},
            "domain": {"rtspecialty.com": jake, "hedgespecialty.com": hedge}}


# ── triage ────────────────────────────────────────────────────────────────────

def evt(text, **kw):
    return {"type": "message", "channel": "CTEST", "user": "UKINJAL", "ts": "300.1", "text": text, **kw}


@pytest.mark.parametrize("e,reason", [
    (evt("can you resend the Bushel submission to Jake?", channel="COTHER"), "not a desk channel"),
    (evt("quote landed", user=ANGIE), "bot or system"),
    (evt("hi", bot_id="B1"), "bot or system"),
    (evt("x", subtype="channel_join"), "bot or system"),
    (evt(f"<@{BOT}> submit this"), "mentions Loma"),
    (evt("<@UKAUSHIK> can you call her?"), "addressed to a person"),
    (evt("thanks!"), "acknowledgement"),
    (evt("ok"), "acknowledgement"),
    (evt(":+1:"), "acknowledgement"),
    (evt("status"), "acknowledgement"),
    (evt("a@b.co made the submission for: TEST X", user="U0BE1F85H0Q"), "submission parent"),
    (evt("eff date is 11/15, she has no field trips"), ""),
])
def test_prefilter(e, reason):
    assert desk.prefilter(e, BOT) == reason


def test_parse_triage():
    assert desk.parse_triage('{"relevant": true, "why": "asks status"}') == (True, "asks status")
    assert desk.parse_triage('noise {"relevant": false, "why": "chit chat"} trailing')[0] is False
    assert desk.parse_triage("garbage") == (False, "unparseable triage output")


def test_triage_dispatches_only_relevant_and_once():
    ctx = Ctx()
    e = evt("did Jake reply on Bushel?", thread_ts="299.0", ts="300.2")
    assert desk.triage(ctx, e, model=lambda *a: (True, "status question")) is True
    assert len(ctx.dispatched) == 1 and ctx.dispatched[0][4] == "299.0"
    assert desk.triage(ctx, e, model=lambda *a: (True, "x")) is False  # same message twice
    assert desk.triage(ctx, evt("lunch?", ts="300.3"), model=lambda *a: (False, "chit chat")) is False
    assert len(ctx.dispatched) == 1


def test_triage_never_calls_model_for_filtered():
    ctx = Ctx()
    called = []
    desk.triage(ctx, evt("thanks", ts="300.4"), model=lambda *a: called.append(1) or (True, ""))
    desk.triage(ctx, evt("hey", user=BOT, ts="300.5"), model=lambda *a: called.append(1) or (True, ""))
    assert not called and not ctx.dispatched


# ── parent detection ──────────────────────────────────────────────────────────

def test_parent_parsing_and_detection():
    text = "<mailto:kinjalk@somainsure.com|kinjalk@somainsure.com> made the submission for: Early Childhood III, LLC"
    assert watch.parse_parent(text) == {"owner_email": "kinjalk@somainsure.com", "customer": "Early Childhood III, LLC"}
    assert watch.core_name("TEST Little Ducklings Academy LLC") == "Little Ducklings Academy"
    base = {"type": "message", "ts": "1.0", "text": text}
    assert desk.is_parent({**base, "channel": "CPROD", "user": ANGIE})
    assert not desk.is_parent({**base, "channel": "CPROD", "user": desk.OWNER_ID})  # stand-in only in test channels
    assert desk.is_parent({**base, "channel": "CTEST", "user": desk.OWNER_ID})
    assert not desk.is_parent({**base, "channel": "COFF", "user": ANGIE})  # flag off
    assert not desk.is_parent({**base, "channel": "CPROD", "user": ANGIE, "thread_ts": "0.5"})


def test_legacy_run_card_skips_desk_channels(monkeypatch):
    posted = []
    monkeypatch.setattr(actions, "SUBMISSION_CHANNEL", "CPROD")
    monkeypatch.setattr(actions.threading, "Timer", lambda *a, **k: posted.append(a) or type("T", (), {"start": lambda s: None})())
    e = {"type": "message", "channel": "CPROD", "user": ANGIE, "ts": "9.1", "text": "a@b.co made the submission for: X"}
    actions.post_run_buttons(Ctx(), e)
    assert posted == []  # desk card owns it


# ── watcher: cursor, status, cards ────────────────────────────────────────────

PARENT_TS = 1_800_000_000.0


def iso(offset_h):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(PARENT_TS + offset_h * 3600))


def item(iid, h, direction, typ="email", frm="", to=(), text="", labels=("SENT",), thread="g1", subject="Re: sub"):
    return {"id": iid, "at": iso(h), "type": typ, "direction": direction, "from": frm, "to": list(to),
            "text": text, "labels": list(labels), "thread_id": thread, "subject": subject, "attachments": []}


SENT_JAKE = item("i1", 1, "out", frm="team@somainsure.com", to=["jake.behuniak@rtspecialty.com"], text="submission")
OLD = item("i0", -30, "in", frm="customer@x.com", text="old history")
JAKE_ASK = item("i2", 3, "in", frm="jake.behuniak@rtspecialty.com", text="Need 5yr loss runs", labels=())
JAKE_QUOTE = item("i3", 5, "in", frm="jake.behuniak@rtspecialty.com",
                  text="Kinsale Insurance Company quote. Total: $6,590.82 incl taxes", labels=())


def make_watcher(ctx, items, events):
    w = watch.Watcher(ctx, state_path=watch.STATE_PATH)
    w.fetch = lambda lead: ([], {})
    w._items = lambda th: list(items)
    w.wins = lambda c, t: False
    w.pool_calls = []

    def pool(prompt, key, lead, **kw):
        w.pool_calls.append(prompt)
        return {"ok": True, "payload": {"events": events()}}
    w.pool = pool
    return w


def test_cursor_first_tick_baselines_history_and_no_cards(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: PARENT_TS + 2 * 3600)
    ctx = Ctx()
    items = [OLD, SENT_JAKE]
    w = make_watcher(ctx, items, lambda: [])
    w.register("CTEST", str(PARENT_TS), {"customer": "Bushel", "lead_id": "L1"}, card_ts="201.0")
    res = w.tick_thread(f"CTEST:{PARENT_TS}")
    assert res["incoming"] == 0 and res["cards"] == 0  # old customer email is history
    assert res["status"]["status"] == "yellow" and res["status"]["sent"] == ["Jake (RT)"]
    assert not w.pool_calls
    assert ctx.web.named("reactions_add")[0]["name"] == "large_yellow_circle"
    th = watch.read_state()["threads"][f"CTEST:{PARENT_TS}"]
    assert set(th["seen"]) == {"i0", "i1"}


def test_new_incoming_posts_one_card_and_restart_never_reposts(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: PARENT_TS + 6 * 3600)
    ctx = Ctx()
    items = [SENT_JAKE]
    ev = lambda: [{"interaction_id": "i2", "kind": "info_request", "material": True,
                   "summary": "Jake asked for 5-year loss runs", "reply_channel": "email_reply",
                   "reply_body": "Hi Jake,\nCollecting them now.\nThanks,\nPranav\nSoma Insurance Services"}]
    w = make_watcher(ctx, items, ev)
    key = f"CTEST:{PARENT_TS}"
    w.register("CTEST", str(PARENT_TS), {"customer": "Bushel", "lead_id": "L1"}, card_ts="201.0")
    w.tick_thread(key)
    items.append(JAKE_ASK)
    res = w.tick_thread(key, force=True)
    assert res["cards"] == 1 and len(w.pool_calls) == 1
    assert res["status"]["next"] == "answer Jake (RT)"
    cards = list(watch.read_state()["cards"].values())
    assert len(cards) == 1 and cards[0]["to"] == watch.TEST_EMAIL  # test channel → Pranav
    # restart: fresh Watcher, same state → nothing new, no model call, no post
    w2 = make_watcher(ctx, items, ev)
    posts_before = len(ctx.web.named("chat_postMessage"))
    res2 = w2.tick_thread(key, force=True)
    assert res2["new"] == 0 and not w2.pool_calls and len(ctx.web.named("chat_postMessage")) == posts_before


def test_pool_failure_retries_incoming_then_gives_up(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: PARENT_TS + 6 * 3600)
    ctx = Ctx()
    items = [SENT_JAKE]
    w = make_watcher(ctx, items, lambda: [])
    w.pool = lambda *a, **k: {"ok": False, "error": "busy"}
    key = f"CTEST:{PARENT_TS}"
    w.register("CTEST", str(PARENT_TS), {"customer": "B", "lead_id": "L1"})
    w.tick_thread(key)
    items.append(JAKE_ASK)
    for n in range(1, 4):
        w.tick_thread(key, force=True)
        seen = watch.read_state()["threads"][key]["seen"]
        assert ("i2" in seen) == (n >= 3)


def test_quote_card_verbatim_figures(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: PARENT_TS + 6 * 3600)
    ctx = Ctx()
    items = [SENT_JAKE]
    ev = lambda: [{"interaction_id": "i3", "kind": "quote", "material": True, "summary": "Jake quoted",
                   "carrier": "Kinsale Insurance Company", "premium_all_in": "$6,590.82",
                   "terms": ["Claims-made"], "subjectivities": ["Signed ACORD"],
                   "reply_channel": "none", "reply_body": "", "bind_request_body": "Hi Jake, what do you need to bind?"}]
    w = make_watcher(ctx, items, ev)
    key = f"CPROD:{PARENT_TS}"
    w.register("CPROD", str(PARENT_TS), {"customer": "Bushel", "lead_id": "L1"}, card_ts="201.0")
    w.tick_thread(key)
    items.append(JAKE_QUOTE)
    res = w.tick_thread(key, force=True)
    assert res["status"]["status"] == "green" and res["status"]["quoted"] == ["Jake (RT)"]
    card = list(watch.read_state()["cards"].values())[0]
    assert card["kind"] == "quote" and card["quote"]["premium"] == "$6,590.82"
    assert card["bind"]["to"] == "jake.behuniak@rtspecialty.com"
    blocks = json.dumps(desk.card_blocks(card))
    assert "dcd_present" in blocks and "dcd_bind" in blocks and "Kinsale Insurance Company" in blocks


def test_verbatim_rejects_model_math():
    src = ["Premium $5,894.00 Taxes $696.82"]
    assert watch.verbatim("$5,894.00", src) == "$5,894.00"
    assert watch.verbatim("$6,590.82", src) == ""  # a sum the source never states


def test_derive_status_precedence():
    d = _desks()
    th = {"thread_ts": str(PARENT_TS), "info": {}, "events": {}}
    assert watch.derive_status(th, [], d)["status"] == "red"
    assert watch.derive_status(th, [], d)["next"] == "send the submission"
    th["events"] = {"i9": {"kind": "decline", "desk": "Jake (RT)", "at": iso(4)}}
    st = watch.derive_status(th, [SENT_JAKE], d)
    assert st["status"] == "yellow" and st["declined"] == ["Jake (RT)"] and "all declined" in st["next"]
    th["events"]["m"] = {"kind": "quote", "desk": "Markel", "party": "markel", "at": iso(4)}
    assert watch.derive_status(th, [SENT_JAKE], d)["status"] == "yellow"  # Markel never counts
    th["won"] = True
    assert watch.derive_status(th, [SENT_JAKE], d)["status"] == "won"


def test_draft_not_sent_is_not_submission():
    d = _desks()
    draft = dict(SENT_JAKE, id="d1", labels=["DRAFT"])
    assert watch.sent_desks([draft], d, 0) == []


def test_header_line_hides_empty_counts():
    st = {"status": "yellow", "sent": ["Jake (RT)", "Hedge"], "quoted": [], "declined": ["Hedge"], "next": "waiting on Jake (RT)"}
    line = watch.header_line(st)
    assert "quoted" not in line and "Hedge declined" in line and "*Next:* waiting on Jake (RT)" in line


def test_in_hours():
    from datetime import datetime
    assert watch.in_hours(datetime(2026, 10, 27, 14, 0, tzinfo=watch.ET))
    assert not watch.in_hours(datetime(2026, 10, 27, 21, 30, tzinfo=watch.ET))
    assert not watch.in_hours(datetime(2026, 11, 1, 12, 0, tzinfo=watch.ET))  # Sunday


# ── cards: recipients + copy rules ────────────────────────────────────────────

INFO = {"customer": "Bushel", "contact_email": "leah@x.com", "contact_phone": "6105790010", "_desks_seen": ["Jake (RT)", "Hedge"]}


def rec(**kw):
    base = {"kind": "customer_question", "party": "customer", "type": "message", "from": "6105790010",
            "summary": "Leah asked when quotes arrive", "reply_channel": "sms", "at": iso(1), "desk": "",
            "reply_body": "Hi Leah, we expect options this week. — Lavish from Soma Insurance Services"}
    return {**base, **kw}


def test_customer_sms_recipient_from_hub_not_model():
    c = desk.make_card("c1", "suggest", "CPROD", "1.0", rec(), INFO)
    assert c["via"] == "sms" and c["to"] == "+16105790010" and c["problems"] == []
    c = desk.make_card("c1", "suggest", "CTEST", "1.0", rec(), INFO)
    assert c["to"] == watch.TEST_PHONE


def test_customer_copy_rules_block_send():
    c = desk.make_card("c2", "suggest", "CPROD", "1.0",
                       rec(reply_body="Kinsale came in at $5,894. — Lavish from Soma Insurance Services"), INFO)
    assert any("dollar" in p for p in c["problems"])
    blocks = json.dumps(desk.card_blocks(c))
    assert "dcd_send" not in blocks and "Fix before sending" in blocks and "dcd_edit" in blocks


def test_wholesaler_reply_market_neutral():
    r = rec(kind="info_request", party="wholesaler", type="email", from_="", desk="Jake (RT)",
            reply_channel="email_reply", reply_body="Hi Jake, Hedge already quoted this.\nThanks,\nPranav")
    r["from"] = "jake.behuniak@rtspecialty.com"
    c = desk.make_card("c3", "suggest", "CPROD", "1.0", r, INFO)
    assert c["to"] == "jake.behuniak@rtspecialty.com" and any("Hedge" in p for p in c["problems"])
    r["reply_body"] = "Hi Jake, collecting the loss runs now.\nThanks,\nPranav"
    assert desk.make_card("c3", "suggest", "CPROD", "1.0", r, INFO)["problems"] == []


def test_send_card_refuses_test_card_to_real_recipient():
    card = {"id": "x", "kind": "suggest", "test": True, "to": "leah@x.com", "via": "email_reply", "body": "hi", "problems": []}
    assert desk.send_card(card)["ok"] is False


# ── action routing ────────────────────────────────────────────────────────────

def _payload(aid, value, user="UPRANAV", channel="CTEST", ts="201.0", thread_ts="1.0"):
    return {"type": "block_actions", "user": {"id": user}, "channel": {"id": channel}, "trigger_id": "T",
            "message": {"ts": ts, "thread_ts": thread_ts, "blocks": [{"type": "actions"}]},
            "actions": [{"action_id": aid, "value": json.dumps(value)}]}


def _store(card):
    with watch.state() as s:
        s["cards"][card["id"]] = card


def test_non_writer_refused(monkeypatch):
    said = []
    monkeypatch.setattr(desk, "_respond", lambda p, t: said.append(t))
    _store({"id": "k1", "kind": "suggest", "status": "open", "channel": "CTEST", "thread_ts": "1.0", "ts": "201.0"})
    ctx = Ctx()
    assert desk.handle_block_action(ctx, _payload("dcd_dismiss", {"k": "k1"}, user="UGUEST")) is True
    assert said and watch.read_state()["cards"]["k1"]["status"] == "open"


def test_dismiss_and_routing_ignores_other_ids():
    ctx = Ctx()
    _store({"id": "k2", "kind": "suggest", "status": "open", "channel": "CTEST", "thread_ts": "1.0", "ts": "201.0",
            "who": "Jake (RT)", "summary": "s", "via": "none"})
    assert desk.handle_block_action(ctx, _payload("dcs_send:jake", {})) is False
    desk.handle_block_action(ctx, _payload("dcd_dismiss", {"k": "k2"}))
    c = watch.read_state()["cards"]["k2"]
    assert c["status"] == "dismissed" and c["by"] == "UPRANAV"
    assert "Dismissed" in json.dumps(ctx.web.named("chat_update")[-1]["blocks"])


def test_send_double_click_is_claimed_once(monkeypatch):
    ctx = Ctx()
    sent = []
    monkeypatch.setattr(desk, "send_card", lambda c: sent.append(c["id"]) or {"ok": True, "ref": "m1"})
    monkeypatch.setattr(desk, "_respond", lambda p, t: None)
    started = []
    monkeypatch.setattr(desk.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda s: started.append((target, args))})())
    _store({"id": "k3", "kind": "suggest", "status": "open", "channel": "CTEST", "thread_ts": "1.0", "ts": "201.0",
            "via": "sms", "to": watch.TEST_PHONE, "body": "hi", "problems": [], "who": "Customer", "summary": "s"})
    desk.handle_block_action(ctx, _payload("dcd_send", {"k": "k3"}))
    desk.handle_block_action(ctx, _payload("dcd_send", {"k": "k3"}))
    assert len(started) == 1
    target, args = started[0]
    target(*args)
    assert sent == ["k3"] and watch.read_state()["cards"]["k3"]["status"] == "sent"


def test_desk_card_buttons_and_submit_run(monkeypatch):
    ctx = Ctx()
    th = {"info": {"customer": "Bushel", "owner_id": "UOWNER", "state": "PA", "lines": ["GL"], "eff": "11/01/2026"},
          "last_status": {"status": "red", "sent": [], "quoted": [], "declined": [], "next": "send the submission"}}
    blocks = desk.desk_card_blocks(th)
    ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert ids == ["dcs_run_intake", "dcs_run_full", "dcs_run_notes"]
    assert "Not submitted yet" in json.dumps(blocks) and "<@UOWNER>" in json.dumps(blocks)
    with watch.state() as s:
        s["threads"]["CTEST:1.0"] = {"channel": "CTEST", "thread_ts": "1.0", "card_ts": "201.0", **th}
    monkeypatch.setattr(actions, "_claim", lambda *a, **k: True)
    actions.handle_block_action(ctx, _payload("dcs_run_full", {"c": "Bushel"}))
    assert ctx.dispatched and "/daycare-submit" in ctx.dispatched[0][7] and "Bushel" in ctx.dispatched[0][7]
    assert watch.read_state()["threads"]["CTEST:1.0"]["last_run"]["label"] == "Submit"


def test_slack_ui_honours_verified_writer(monkeypatch):
    env = actions.RUN_ENV
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw["env"])
        return type("P", (), {"stdout": "{}", "stderr": ""})()
    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    actions._ui("send", "--manifest", "m", "--broker", "all", "--user", "UKINJAL")
    assert captured.get("LOMA_VERIFIED_WRITER") == "UKINJAL"
    assert "LOMA_VERIFIED_WRITER" not in env


def test_shared_domain_rolls_up_and_later_quote_supersedes_ask():
    d = _desks()
    sent = item("s1", 1, "out", frm="team@somainsure.com", to=["submissions@hedgespecialty.com"])
    th = {"thread_ts": str(PARENT_TS), "info": {}, "events": {
        "a": {"kind": "info_request", "party": "wholesaler", "desk": "Luke (Hedge)", "org": "Hedge", "at": iso(2)}}}
    st = watch.derive_status(th, [sent], d)
    assert st["sent"] == ["Hedge"] and st["next"] == "answer Luke (Hedge)"
    th["events"]["q"] = {"kind": "quote", "party": "wholesaler", "desk": "Luke (Hedge)", "org": "Hedge", "at": iso(3)}
    st = watch.derive_status(th, [sent], d)
    assert st["status"] == "green" and st["quoted"] == ["Hedge"] and st["next"] == "present the Hedge quote"


def test_terms_with_unstated_figures_are_dropped():
    src = ["Premium: $1,250.00 Policy fee: $150.00 Total: $1,449.70"]
    lines = ["Premium $1,250.00 plus fee $150.00", "All-in $1,400.00", "Occurrence form"]
    assert watch.figures_verbatim(lines, src) == ["Premium $1,250.00 plus fee $150.00", "Occurrence form"]


def test_next_action_lists_two_open_asks():
    d = _desks()
    th = {"thread_ts": str(PARENT_TS), "info": {}, "events": {
        "a": {"kind": "info_request", "party": "wholesaler", "desk": "Jake (RT)", "org": "Jake (RT)", "at": iso(2)},
        "b": {"kind": "customer_question", "party": "customer", "at": iso(3)}}}
    assert watch.derive_status(th, [SENT_JAKE], d)["next"] == "reply to the customer · answer Jake (RT)"
