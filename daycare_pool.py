#!/usr/bin/env python3
"""One structured analysis on the Angie Claude pool, under the shared admission.

Run as a subprocess (needs python-dotenv for the pool client):
  uv run --with python-dotenv python daycare_pool.py < request.json > result.json

stdin:  {"prompt": str, "schema": {...}, "key": str, "lead_id": str}
stdout: {"ok": true, "payload": {...}, "session_id": str} | {"ok": false, "error": str}

Reuses the intake-QA transport (`angie_pool_client._request`) and its cross-process
admission (`angie_admission.admission`) with the SAME journal directory, so every
producer shares one owned-job count (daytime cap 8 per lead-chat's capacity policy).
Journals are named daycare-watch-<key>.json; completed ones are reused, so a retry
after a crash never starts a second session for the same request.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

QA_DIR = Path(os.environ.get("ANGIE_QA_DIR", "/Users/pranav/work/sales/sales-qa/intake-live-2026-09-24"))
sys.path.insert(0, str(QA_DIR))

TERMINAL = {"completed", "failed", "rate_limited", "cancelled"}
LOG = Path(os.environ.get("CLAUDE_SLACK_INSTALL_DIR", Path.home() / ".claude/claude-slack-bot")) / "daycare-pool.log"


def _emit(event: str, **fields) -> None:
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _save(path: Path, value: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def run(req: dict) -> dict:
    import fcntl
    import angie_pool_client
    from angie_pool_client import SESSION_DIR, _request, _decode_result  # noqa: E402
    from angie_admission import admission  # noqa: E402

    # The intake-QA client still names the pre-move path; the pool credentials now
    # live in sales/sales-follow-ups/.env. Read by the client itself, never printed.
    if not Path(angie_pool_client.POOL_ENV).exists():
        angie_pool_client.POOL_ENV = Path(os.environ.get("ANGIE_POOL_ENV", "/Users/pranav/work/sales/sales-follow-ups/.env"))

    prompt, schema = req["prompt"], req["schema"]
    if not 1 <= len(prompt) <= 45_000:
        return {"ok": False, "error": f"prompt length {len(prompt)} out of range"}
    body = {
        "prompt": prompt + '\n\nReturn the required structured output envelope: action="silent", '
                           'text="Daycare watch", attach=[], payload=<schema result>. Do not post to Slack or use tools.',
        "json_schema": json.dumps(schema, separators=(",", ":")),
        "metadata": {"trigger": "daycare_watch", "lead_id": req.get("lead_id", ""), "phase": "daycare_watch"},
        "model": req.get("model", "sonnet"),
        "timeout_seconds": 600, "max_budget_usd": 2.0,
        "chrome": False, "clean": True, "allow_project_mcp": False,
    }
    key = "daycare-watch-" + (req.get("key") or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:40])
    body["metadata"]["request_key"] = key
    SESSION_DIR.mkdir(mode=0o700, exist_ok=True)
    path = SESSION_DIR / (key + ".json")
    info = {"lead_id": req.get("lead_id", ""), "phase": "daycare_watch", "request_key": key}
    with (SESSION_DIR / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(path.read_text()) if path.exists() else {}
        if saved.get("status") == "completed":
            return {"ok": True, "payload": saved["payload"], "session_id": saved.get("session_id"), "cached": True}
        sid = saved.get("session_id")
        if saved.get("status") == "submitting" and not sid:
            return {"ok": False, "error": "acceptance unknown for " + key + " — reconcile before retrying"}
        if not sid or saved.get("status") in TERMINAL | {"rejected"}:
            with admission(SESSION_DIR, _request, _emit):
                _save(path, {"status": "submitting", **info})
                try:
                    accepted = _request("POST", "/claude/session", body)
                except RuntimeError as exc:
                    import urllib.error
                    cause = exc.__cause__
                    if isinstance(cause, urllib.error.HTTPError) and cause.code in {400, 401, 403, 404, 413, 422, 429}:
                        _save(path, {**info, "status": "rejected", "http_status": cause.code})
                    raise
                sid = accepted.get("session_id")
                if not sid:
                    return {"ok": False, "error": "pool returned no session id"}
                saved = {"status": "accepted", "session_id": sid, **info}
                _save(path, saved)
                _emit("accepted", **info, session_id=sid)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            time.sleep(5)
            result = _request("GET", f"/claude/session/{sid}")
            status = result.get("status")
            if status == "completed":
                account = (result.get("metadata") or {}).get("claude_account") or {}
                if account.get("execution_lane") != "angie_pool" or account.get("auth") != "account_pool":
                    _save(path, {**saved, "status": "failed"})
                    return {"ok": False, "error": f"session {sid} did not run on the account pool"}
                payload = _decode_result(result.get("result"))
                _save(path, {**saved, "status": "completed", "payload": payload})
                _emit("completed", **info, session_id=sid)
                return {"ok": True, "payload": payload, "session_id": sid}
            if status in TERMINAL:
                _save(path, {**saved, "status": status})
                _emit("terminal", **info, session_id=sid, status=status)
                return {"ok": False, "error": f"session {sid} {status}"}
        return {"ok": False, "error": f"session {sid} still running; retained for the next tick"}


def _cleanup_lock(req: dict) -> None:
    """A request that never reserved (no journal) leaves no lock file behind."""
    key = req.get("key")
    if not key:
        return
    d = QA_DIR / ".angie-sessions"
    if not (d / f"daycare-watch-{key}.json").exists():
        try:
            (d / f"daycare-watch-{key}.lock").unlink()
        except OSError:
            pass


if __name__ == "__main__":
    req = {}
    try:
        req = json.load(sys.stdin)
        out = run(req)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    _cleanup_lock(req)
    print(json.dumps(out))
