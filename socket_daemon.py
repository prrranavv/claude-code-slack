#!/usr/bin/env python3
"""Real-time Slack event daemon for claude-code-slack.

Opens a WebSocket to Slack via Socket Mode, receives app_mention + message.im
events as they happen, and spawns worker.sh per qualifying event.
"""
import errno
import json
import logging
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


# ─── Config loading ────────────────────────────────────────────────────────────

INSTALL_DIR = os.environ.get(
    "CLAUDE_SLACK_INSTALL_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "claude-slack-bot"),
)

def _load_config_env():
    """Load config.env from INSTALL_DIR into os.environ (only for unset keys)."""
    path = os.path.join(INSTALL_DIR, "config.env")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)', line)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = val
    except OSError:
        pass

_load_config_env()


def _load_from_zshrc(var: str) -> str:
    existing = os.environ.get(var)
    if existing:
        return existing
    try:
        with open(os.path.expanduser("~/.zshrc")) as f:
            for line in f:
                m = re.match(rf'^export\s+{re.escape(var)}=(.*)$', line.strip())
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ─── Runtime config (all from env / config.env) ───────────────────────────────

LOG_FILE = os.path.join(INSTALL_DIR, "watch.log")
DETACH = os.path.join(INSTALL_DIR, "detach.py")
WORKER = os.path.join(INSTALL_DIR, "worker.sh")
STATUS_DIR = "/tmp/claude-slack-bot-status"
SESSION_DIR = os.path.join(INSTALL_DIR, "sessions")
CLAUDE_WORKSPACE = os.environ.get("CLAUDE_WORKSPACE", os.path.expanduser("~"))
BOT_NAME = os.environ.get("BOT_NAME", "ClaudeBot")
DEFAULT_MODEL = "opus"
FORWARD_CHANNEL = os.environ.get("FORWARD_CHANNEL", "")
STALE_RUNNING_AFTER = 2 * 3600
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "10"))
BACKLOG_DRAIN_INTERVAL = 20
PREFETCH_CONTEXT = os.environ.get("PREFETCH_CONTEXT", "off")
THREAD_MSG_CAP = 30
IMAGE_COUNT_CAP = 20
IMAGE_SIZE_CAP = 5 * 1024 * 1024
PREFETCH_WORKERS = 8
PREFETCH_TIMEOUT = 3.0
IMAGE_DL_TIMEOUT = 6.0
THINKING_STATUS = "is thinking…"
SILENT_STATUS_ERRORS = {"channel_not_supported", "not_in_thread", "thread_not_found"}
SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "bot_message",
    "message_changed", "message_deleted",
    "thread_broadcast",
}

BOT_USER_ID = os.environ.get("BOT_USER_ID", "")
AUTHORIZED_USER = os.environ.get("AUTHORIZED_USER_ID", "")
AUTHORIZED_USER_NAME = os.environ.get("AUTHORIZED_USER_NAME", "Owner")
BOT_TOKEN = _load_from_zshrc("SLACK_BOT_TOKEN")
APP_TOKEN = _load_from_zshrc("SLACK_APP_TOKEN")

if not BOT_TOKEN or not APP_TOKEN:
    sys.stderr.write("ERROR: missing SLACK_BOT_TOKEN or SLACK_APP_TOKEN\n"
                     "Set them in config.env or export them before starting.\n")
    sys.exit(1)
if not BOT_USER_ID:
    sys.stderr.write("ERROR: BOT_USER_ID is not set\n")
    sys.exit(1)
if not AUTHORIZED_USER:
    sys.stderr.write("ERROR: AUTHORIZED_USER_ID is not set\n")
    sys.exit(1)


# ─── Trigger parsing ──────────────────────────────────────────────────────────

MODEL_TRIGGER_RE = re.compile(r"(?:^|\s|>)!(haiku|sonnet|opus)\b", re.IGNORECASE)
FAST_TRIGGER_RE = re.compile(r"(?:^|\s|>)!fast\b", re.IGNORECASE)
DELETE_TRIGGER_RE = re.compile(r"(?:^|\s|>)!delete\b", re.IGNORECASE)
BOT_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
STATUS_QUERY_RE = re.compile(r"^(?:status\??|\?)$", re.IGNORECASE)
STOP_QUERY_RE = re.compile(r"^stop\??$", re.IGNORECASE)

STOP_REACTIONS = {"octagonal_sign"}
SPINNER_EMOJIS = ("eyes", "zzz", "hourglass_flowing_sand", "brain", "writing_hand")

STAGE_MAP = {
    "eyes": "starting",
    "zzz": "queued",
    "hourglass_flowing_sand": "working",
    "brain": "thinking",
    "writing_hand": "writing reply",
    "white_check_mark": "completed",
    "speech_balloon": "waiting on clarification",
    "x": "failed",
    "octagonal_sign": "stopped",
}


def parse_model_trigger(text: str) -> str:
    m = MODEL_TRIGGER_RE.search(text or "")
    return m.group(1).lower() if m else ""

def parse_fast_trigger(text: str) -> bool:
    return bool(FAST_TRIGGER_RE.search(text or ""))

def is_delete_trigger(text: str) -> bool:
    return bool(DELETE_TRIGGER_RE.search(text or ""))

def is_status_query(text: str) -> bool:
    if not text:
        return False
    return bool(STATUS_QUERY_RE.match(BOT_MENTION_RE.sub("", text).strip()))

def is_stop_query(text: str) -> bool:
    if not text:
        return False
    return bool(STOP_QUERY_RE.match(BOT_MENTION_RE.sub("", text).strip()))


# ─── Logging ──────────────────────────────────────────────────────────────────

class _Fmt(logging.Formatter):
    def format(self, record):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        return f"[{ts}] {record.getMessage()}"

os.makedirs(INSTALL_DIR, exist_ok=True)
_handler = logging.FileHandler(LOG_FILE)
_handler.setFormatter(_Fmt())
log = logging.getLogger("sockd")
log.setLevel(logging.INFO)
log.addHandler(_handler)


# ─── State ────────────────────────────────────────────────────────────────────

web = WebClient(token=BOT_TOKEN)
seen_keys = set()
seen_lock = threading.Lock()
backlog = deque()
backlog_lock = threading.Lock()

_prefetch_executor = ThreadPoolExecutor(max_workers=PREFETCH_WORKERS, thread_name_prefix="prefetch")
_image_download_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="img-dl")
_user_display_cache = OrderedDict()
_user_cache_lock = threading.Lock()
_USER_CACHE_MAX = 256
_channel_name_cache = OrderedDict()
_channel_cache_lock = threading.Lock()
_CHANNEL_CACHE_MAX = 128


def running_claudes() -> int:
    """Count live workers by reading non-stale .running sidecars."""
    try:
        now = time.time()
        entries = os.listdir(STATUS_DIR)
        return sum(
            1 for fn in entries
            if fn.endswith(".running")
            and now - os.path.getmtime(f"{STATUS_DIR}/{fn}") <= STALE_RUNNING_AFTER
        )
    except OSError:
        return 0


def try_claim_eyes(channel: str, ts: str) -> bool:
    try:
        web.reactions_add(channel=channel, timestamp=ts, name="eyes")
        return True
    except SlackApiError as e:
        err = e.response.get("error", "")
        if err == "already_reacted":
            return False
        log.info(f"reactions.add failed for {channel}/{ts}: {err}")
        return True


def set_thinking_status(channel: str, thread_ts: str) -> None:
    try:
        web.api_call(
            "assistant.threads.setStatus",
            json={"channel_id": channel, "thread_ts": thread_ts, "status": THINKING_STATUS},
        )
    except SlackApiError as e:
        err = e.response.get("error", "")
        if err not in SILENT_STATUS_ERRORS:
            log.info(f"setStatus failed for {channel}/{thread_ts}: {err}")
    except Exception as e:
        log.info(f"setStatus exception for {channel}/{thread_ts}: {e}")


def write_running_file(channel: str, ts: str, user: str, thread_ts: str,
                       model: str, pid: int = 0, fast: bool = False) -> None:
    path = f"{STATUS_DIR}/{ts.replace('.', '_')}.running"
    data = {
        "channel": channel, "ts": ts, "thread_ts": thread_ts,
        "user": user, "model": model or DEFAULT_MODEL,
        "thinking_off": bool(fast), "spawned_at": int(time.time()), "pid": pid,
    }
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError as e:
        log.info(f"running file write failed for {ts}: {e}")


def read_thread_session(thread_ts: str):
    path = f"{SESSION_DIR}/{thread_ts.replace('.', '_')}.json"
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def resolve_effective_model(thread_ts: str, user: str, requested_model: str) -> str:
    sess = read_thread_session(thread_ts)
    if sess and sess.get("first_user_id") == user and sess.get("model"):
        return sess["model"]
    return requested_model or DEFAULT_MODEL


def spawn_worker(channel: str, ts: str, user: str, thread_ts: str,
                 model: str = "", is_dm: bool = False,
                 mention_text: str = "", fast: bool = False) -> None:
    effective_model = resolve_effective_model(thread_ts, user, model)
    env = {**os.environ, "MODEL": effective_model}
    env["THREAD_TS"] = thread_ts
    env["IS_DM"] = "1" if is_dm else "0"
    env["CLAUDE_SLACK_INSTALL_DIR"] = INSTALL_DIR
    if fast:
        env["FAST"] = "1"
    env["MENTION_TEXT"] = (mention_text or "")[:500]

    gate_ok = False
    if PREFETCH_CONTEXT == "shadow":
        gate_ok = True
    elif PREFETCH_CONTEXT == "dm":
        gate_ok = is_dm
    elif PREFETCH_CONTEXT == "1":
        gate_ok = True

    if gate_ok:
        resume_sessions_env = os.environ.get("RESUME_SESSIONS", "0")
        resume_feature_on = (resume_sessions_env == "1") or (resume_sessions_env == "dm" and is_dm)
        is_resume = False
        last_turn_ts = None
        if resume_feature_on:
            sess = read_thread_session(thread_ts)
            if (sess and sess.get("first_user_id") == user
                    and (sess.get("model") or "") == effective_model
                    and int(sess.get("consecutive_failures") or 0) < 2
                    and sess.get("last_turn_ts")):
                is_resume = True
                last_turn_ts = sess.get("last_turn_ts")
        try:
            future = _prefetch_executor.submit(
                build_thread_context, channel, ts, thread_ts, is_dm, is_resume, last_turn_ts,
            )
            context_block = future.result(timeout=PREFETCH_TIMEOUT)
        except Exception as e:
            log.info(f"prefetch: timeout/error ({type(e).__name__}: {e})")
            context_block = None
        if context_block is not None:
            wrote = write_context_sidecar(ts, context_block)
            if wrote and PREFETCH_CONTEXT == "shadow":
                env["CONTEXT_MODE"] = "shadow"

    proc = subprocess.Popen(
        ["/usr/bin/python3", DETACH, "/bin/bash", WORKER, channel, ts, user],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, env=env,
    )
    write_running_file(channel, ts, user, thread_ts, effective_model, pid=proc.pid, fast=fast)


def list_running_in_thread(channel: str, thread_ts: str) -> list:
    out = []
    now = time.time()
    try:
        entries = os.listdir(STATUS_DIR)
    except OSError:
        return out
    for fn in entries:
        if not fn.endswith(".running"):
            continue
        path = f"{STATUS_DIR}/{fn}"
        try:
            if now - os.path.getmtime(path) > STALE_RUNNING_AFTER:
                continue
            with open(path) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("channel") == channel and d.get("thread_ts") == thread_ts:
            out.append(d)
    return sorted(out, key=lambda x: x.get("spawned_at", 0))


def current_stage(channel: str, ts: str) -> str:
    try:
        r = web.reactions_get(channel=channel, timestamp=ts, full=False)
        reactions = (r.data.get("message") or {}).get("reactions", []) or []
        for react in reactions:
            if BOT_USER_ID in (react.get("users") or []):
                return STAGE_MAP.get(react.get("name", ""), react.get("name", ""))
        return "no reaction yet"
    except Exception:
        return "unknown"


def fmt_elapsed(seconds: int) -> str:
    m, s = divmod(max(0, seconds), 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h{m}m"
    if m: return f"{m}m{s}s"
    return f"{s}s"


def fmt_elapsed_human(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 45: return "just now"
    if s < 90: return "1m ago"
    m, _ = divmod(s, 60)
    if m < 60: return f"{m}m ago"
    h, m = divmod(m, 60)
    if h < 24: return f"{h}h{m}m ago" if m else f"{h}h ago"
    d, h = divmod(h, 24)
    return f"{d}d ago"


def fmt_tokens_short(n: int) -> str:
    if n < 1000: return str(n)
    if n < 1_000_000: return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


def _worker_permalink(channel: str, worker_ts: str) -> str:
    try:
        pr = web.chat_getPermalink(channel=channel, message_ts=worker_ts)
        link = pr.data.get("permalink", "")
        if link:
            return f"<{link}|mention>"
    except Exception:
        pass
    return f"`{worker_ts}`"


# ─── Thread context pre-fetch ─────────────────────────────────────────────────

def resolve_user_display_name(user_id: str) -> str:
    if not user_id:
        return "?"
    with _user_cache_lock:
        if user_id in _user_display_cache:
            _user_display_cache.move_to_end(user_id)
            return _user_display_cache[user_id]
    name = user_id
    try:
        r = web.users_info(user=user_id)
        u = r.data.get("user") or {}
        profile = u.get("profile") or {}
        name = (profile.get("display_name") or profile.get("real_name")
                or u.get("real_name") or u.get("name") or user_id)
    except Exception:
        pass
    with _user_cache_lock:
        _user_display_cache[user_id] = name
        _user_display_cache.move_to_end(user_id)
        while len(_user_display_cache) > _USER_CACHE_MAX:
            _user_display_cache.popitem(last=False)
    return name


def resolve_channel_label(channel: str, is_dm: bool) -> str:
    if is_dm:
        return f"DM ({channel})"
    with _channel_cache_lock:
        cached = _channel_name_cache.get(channel)
        if cached:
            _channel_name_cache.move_to_end(channel)
            return f"#{cached} ({channel})"
    name = None
    try:
        r = web.conversations_info(channel=channel)
        name = ((r.data.get("channel") or {}).get("name")) or None
    except Exception:
        pass
    if name:
        with _channel_cache_lock:
            _channel_name_cache[name] = name
            _channel_name_cache.move_to_end(channel)
            while len(_channel_name_cache) > _CHANNEL_CACHE_MAX:
                _channel_name_cache.popitem(last=False)
        return f"#{name} ({channel})"
    return f"({channel})"


def _download_slack_image(file_info: dict):
    file_id = file_info.get("id") or ""
    size = int(file_info.get("size") or 0)
    if not file_id:
        return (None, size)
    if size and size > IMAGE_SIZE_CAP:
        return (None, size)
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        return (None, size)
    mimetype = file_info.get("mimetype") or ""
    ext = mimetypes.guess_extension(mimetype) or ""
    if ext == ".jpe":
        ext = ".jpg"
    path = f"/tmp/slackimg_{file_id}{ext}"
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return (path, os.path.getsize(path))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=IMAGE_DL_TIMEOUT) as resp:
            written = 0
            with open(path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > IMAGE_SIZE_CAP:
                        raise IOError("exceeded IMAGE_SIZE_CAP mid-stream")
                    f.write(chunk)
        return (path, written)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        return (None, size)


def _fmt_hms_local(ts_str: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts_str)).strftime("%H:%M:%S")
    except Exception:
        return ts_str or "?"


def _fmt_iso_utc(ts_str: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts_str), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ts_str or "?"


def _fmt_size(n: int) -> str:
    if not n: return "?"
    if n < 1024: return f"{n} B"
    if n < 1024 * 1024: return f"{n // 1024} KB"
    return f"{n / (1024*1024):.1f} MB"


def build_thread_context(channel: str, trigger_ts: str, thread_ts: str,
                         is_dm: bool, is_resume: bool, last_turn_ts) -> str:
    t0 = time.time()
    try:
        messages = []
        if is_resume and last_turn_ts:
            r = web.conversations_replies(channel=channel, ts=thread_ts,
                                          oldest=last_turn_ts, limit=THREAD_MSG_CAP)
            raw = r.data.get("messages", []) or []
            messages = [m for m in raw if (m.get("ts") or "") > str(last_turn_ts)]
        else:
            use_replies = is_dm or (bool(thread_ts) and thread_ts != trigger_ts)
            if use_replies:
                r = web.conversations_replies(channel=channel, ts=thread_ts, limit=THREAD_MSG_CAP)
                messages = r.data.get("messages", []) or []
            else:
                r = web.conversations_history(channel=channel, latest=trigger_ts,
                                              inclusive=True, limit=THREAD_MSG_CAP)
                messages = list(reversed(r.data.get("messages", []) or []))

        if len(messages) > THREAD_MSG_CAP:
            messages = messages[-THREAD_MSG_CAP:]

        image_files = []
        seen_file_ids = set()
        ordered = []
        trigger_msg = next((m for m in messages if (m.get("ts") or "") == trigger_ts), None)
        if trigger_msg is not None:
            ordered.append(trigger_msg)
        for m in reversed(messages):
            if m is trigger_msg:
                continue
            ordered.append(m)
        for msg in ordered:
            for f in (msg.get("files") or []):
                mt = (f.get("mimetype") or "")
                if not mt.startswith("image/"):
                    continue
                fid = f.get("id") or ""
                if not fid or fid in seen_file_ids:
                    continue
                if len(image_files) >= IMAGE_COUNT_CAP:
                    break
                image_files.append(f)
                seen_file_ids.add(fid)
            if len(image_files) >= IMAGE_COUNT_CAP:
                break

        image_paths = {}
        image_sizes = {}
        if image_files:
            futures = {f.get("id"): _image_download_executor.submit(_download_slack_image, f)
                       for f in image_files if f.get("id")}
            for fid, fut in futures.items():
                try:
                    path, sz = fut.result(timeout=IMAGE_DL_TIMEOUT)
                except Exception:
                    path, sz = (None, 0)
                image_paths[fid] = path
                image_sizes[fid] = sz

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ch_label = resolve_channel_label(channel, is_dm)
        lines = []
        if is_resume and last_turn_ts:
            lines.append(f"_Delta since your last turn (updated_at={_fmt_iso_utc(last_turn_ts)})_")
        else:
            count = min(len(messages), THREAD_MSG_CAP)
            lines.append(f"_Snapshot at {now_iso} — last {count} message(s) of thread_")
        lines.append("")
        lines.append(f"- **Channel:** {ch_label}")
        lines.append(f"- **Thread ts:** {thread_ts}")
        lines.append(f"- **Trigger ts:** {trigger_ts} (the message you are acting on)")
        lines.append("")

        if not messages and is_resume:
            lines.append("_No new messages since last turn; only the trigger is below._")
        else:
            header = "### New messages" if (is_resume and last_turn_ts) else "### Messages"
            lines.append(header)
            lines.append("")
            for msg in messages:
                mts = msg.get("ts") or ""
                uid = msg.get("user") or ""
                if uid:
                    author = "@" + resolve_user_display_name(uid)
                elif msg.get("bot_id"):
                    author = "@" + (msg.get("username") or msg.get("bot_id") or "bot")
                else:
                    author = "@?"
                if mts == trigger_ts:
                    lines.append(">>> TRIGGER MESSAGE <<<")
                text = (msg.get("text") or "").rstrip()
                lines.append(f"[{_fmt_hms_local(mts)}] {author}:")
                if text:
                    for tl in text.splitlines():
                        lines.append(f"  {tl}")
                else:
                    lines.append("  _(no text)_")
                for f in (msg.get("files") or []):
                    mt = (f.get("mimetype") or "")
                    fid = f.get("id") or ""
                    size_s = _fmt_size(int(f.get("size") or 0))
                    if mt.startswith("image/") and image_paths.get(fid):
                        lines.append(f"  [attachment: image {image_paths[fid]} ({size_s})]")
                    elif mt.startswith("image/"):
                        lines.append(f"  [attachment: image id={fid} not downloaded ({size_s})]")
                    else:
                        lines.append(f"  [attachment: {mt or 'file'} id={fid} ({size_s})]")
                lines.append("")

        elapsed = time.time() - t0
        n_imgs = sum(1 for p in image_paths.values() if p)
        n_bytes = sum(image_sizes.values() or [0])
        log.info(f"prefetch: built context (messages={len(messages)}, images={n_imgs}, "
                 f"bytes={n_bytes}, elapsed={elapsed:.2f}s, mode={'delta' if is_resume else 'fresh'})")
        return "\n".join(lines).rstrip() + "\n"
    except SlackApiError as e:
        log.info(f"prefetch: failed (slack={e.response.get('error')}, elapsed={time.time()-t0:.2f}s)")
        return None
    except Exception as e:
        log.info(f"prefetch: failed ({type(e).__name__}: {e}, elapsed={time.time()-t0:.2f}s)")
        return None


def write_context_sidecar(ts: str, context_block) -> bool:
    if context_block is None:
        return False
    path = f"{STATUS_DIR}/{ts.replace('.', '_')}.context"
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(context_block)
        return True
    except OSError as e:
        log.info(f"prefetch: sidecar write failed for {ts}: {e}")
        return False


def read_session_context_tokens(session_uuid: str) -> int:
    if not session_uuid:
        return 0
    workspace_key = CLAUDE_WORKSPACE.replace("/", "-")
    path = f"{os.path.expanduser('~')}/.claude/projects/{workspace_key}/{session_uuid}.jsonl"
    if not os.path.exists(path):
        return 0
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 500_000))
            chunk = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return 0
    last = 0
    for line in chunk.splitlines():
        try:
            d = json.loads(line)
            if d.get("type") == "assistant":
                u = d.get("message", {}).get("usage") or {}
                t = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                     + u.get("cache_creation_input_tokens", 0))
                if t:
                    last = t
        except Exception:
            pass
    return last


# ─── Status / stop handlers ───────────────────────────────────────────────────

def handle_status_query(channel: str, ts: str, user: str, thread_ts: str) -> None:
    workers = [w for w in list_running_in_thread(channel, thread_ts) if w.get("ts") != ts]
    now = int(time.time())
    sess = read_thread_session(thread_ts)
    lines = []
    if sess:
        turn = sess.get("turn_count", "?")
        model = sess.get("model") or DEFAULT_MODEL
        age = fmt_elapsed_human(now - int(sess.get("updated_at", now)))
        fails = sess.get("consecutive_failures", 0)
        tokens = read_session_context_tokens(sess.get("session_uuid", ""))
        pct = int(round(100 * tokens / 1_000_000)) if tokens else 0
        ctx_str = (f"*{fmt_tokens_short(tokens)} / 1M* tokens ({pct}%)"
                   if tokens else "*context unknown*")
        fail_note = f"  ·  ⚠️ fails={fails}" if fails else ""
        lines.append(f"🧠  *turn {turn}* on `{model}`  ·  {ctx_str}  ·  auto-compacts at 400k{fail_note}")
        activity_parts = [f"⏱️  last active {age}"]
        if len(workers) == 1:
            w = workers[0]
            elapsed = fmt_elapsed(now - w.get("spawned_at", now))
            stage = current_stage(channel, w.get("ts"))
            activity_parts.append(f"⚡ running {elapsed} _{stage}_")
        elif len(workers) > 1:
            activity_parts.append(f"⚡ {len(workers)} sessions running (see below)")
        lines.append("  ·  ".join(activity_parts))
        if len(workers) > 1:
            lines.append("")
            for w in workers:
                elapsed = fmt_elapsed(now - w.get("spawned_at", now))
                stage = current_stage(channel, w.get("ts"))
                lines.append(f"  • {_worker_permalink(channel, w.get('ts'))} — {elapsed} _{stage}_")
        text = "\n".join(lines)
    elif workers:
        w = workers[0]
        elapsed = fmt_elapsed(now - w.get("spawned_at", now))
        stage = current_stage(channel, w.get("ts"))
        model = w.get("model") or DEFAULT_MODEL
        text = (f"🧠  _First turn — no thread memory yet._\n"
                f"⚡  running on `{model}` for {elapsed} _{stage}_")
    else:
        text = "_No thread memory and no active sessions in this thread._"
    try:
        web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        web.reactions_add(channel=channel, timestamp=ts, name="information_source")
    except SlackApiError as e:
        log.info(f"status post failed for {channel}/{ts}: {e.response.get('error')}")
    log.info(f"STATUS QUERY: {channel}/{ts} by {user} — {len(workers)} active")


def cleanup_spinner_reactions(channel: str, ts: str) -> None:
    for emoji in SPINNER_EMOJIS:
        try:
            web.reactions_remove(channel=channel, timestamp=ts, name=emoji)
        except SlackApiError:
            pass


def sweep_mcp_orphans_if_idle() -> None:
    try:
        entries = [f for f in os.listdir(STATUS_DIR) if f.endswith(".running")]
    except OSError:
        entries = []
    if entries:
        return
    subprocess.run(["pkill", "-f", "chrome-devtools-mcp"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _pgroup_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError as e:
        return e.errno == errno.EPERM


def do_stop(channel: str, thread_ts: str, trigger_ts: str, trigger_kind: str) -> int:
    sidecars = list_running_in_thread(channel, thread_ts)
    if not sidecars:
        return 0
    killed = []
    for s in sidecars:
        pgid = int(s.get("pid") or 0)
        if pgid <= 0:
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
            killed.append(s)
            log.info(f"STOP: SIGTERM pgroup={pgid} worker_ts={s.get('ts')}")
        except ProcessLookupError:
            log.info(f"STOP: pgroup={pgid} already gone, worker_ts={s.get('ts')}")
        except OSError as e:
            log.info(f"STOP: killpg {pgid} failed: {e}")
    time.sleep(0.5)
    for s in killed:
        pgid = int(s.get("pid") or 0)
        if pgid > 0 and _pgroup_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
                log.info(f"STOP: SIGKILL pgroup={pgid} (SIGTERM didn't take)")
            except (OSError, ProcessLookupError):
                pass
    time.sleep(0.3)
    for s in killed:
        worker_ts = s.get("ts")
        if not worker_ts:
            continue
        cleanup_spinner_reactions(channel, worker_ts)
        try:
            web.reactions_add(channel=channel, timestamp=worker_ts, name="octagonal_sign")
        except SlackApiError as e:
            if e.response.get("error") != "already_reacted":
                log.info(f"STOP: stamp 🛑 on {worker_ts} failed: {e.response.get('error')}")
    try:
        web.chat_postMessage(channel=channel, thread_ts=thread_ts,
                             text=f"🛑 Stopped by {AUTHORIZED_USER_NAME}.")
    except SlackApiError as e:
        log.info(f"STOP: chat.postMessage failed: {e.response.get('error')}")
    if trigger_kind == "mention" and trigger_ts:
        try:
            web.reactions_add(channel=channel, timestamp=trigger_ts, name="octagonal_sign")
        except SlackApiError:
            pass
    sweep_mcp_orphans_if_idle()
    return len(killed)


def handle_stop_mention(channel: str, ts: str, user: str, thread_ts: str) -> None:
    if user != AUTHORIZED_USER:
        log.info(f"STOP MENTION REJECTED (user={user}): {channel}/{ts}")
        try:
            web.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                 text="_Only the authorized user can stop sessions._")
            web.reactions_add(channel=channel, timestamp=ts, name="no_entry")
        except SlackApiError:
            pass
        return
    killed = do_stop(channel, thread_ts, ts, trigger_kind="mention")
    if killed == 0:
        try:
            web.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                 text="_No active sessions to stop._")
            web.reactions_add(channel=channel, timestamp=ts, name="information_source")
        except SlackApiError:
            pass
    log.info(f"STOP MENTION: {channel}/{ts} by {user} — killed {killed} in thread {thread_ts}")


def handle_delete_thread(channel: str, ts: str, user: str, thread_ts: str) -> None:
    if user != AUTHORIZED_USER:
        log.info(f"DELETE REJECTED (user={user}): {channel}/{ts}")
        return
    deleted = 0
    try:
        cursor = None
        while True:
            kwargs = dict(channel=channel, ts=thread_ts, limit=200)
            if cursor:
                kwargs["cursor"] = cursor
            r = web.conversations_replies(**kwargs)
            msgs = r.data.get("messages") or []
            for m in msgs:
                if m.get("user") == BOT_USER_ID or (m.get("bot_id") and m.get("ts") != thread_ts):
                    try:
                        web.chat_delete(channel=channel, ts=m["ts"])
                        deleted += 1
                    except SlackApiError as e:
                        log.info(f"DELETE: chat.delete {m['ts']} failed: {e.response.get('error')}")
            meta = r.data.get("response_metadata") or {}
            cursor = meta.get("next_cursor")
            if not cursor:
                break
    except SlackApiError as e:
        log.info(f"DELETE: conversations.replies failed: {e.response.get('error')}")
    try:
        web.reactions_add(channel=channel, timestamp=ts, name="broom")
    except SlackApiError:
        pass
    log.info(f"DELETE: {channel}/{thread_ts} by {user} — deleted {deleted} bot messages")


def resolve_thread_ts(channel: str, ts: str) -> str:
    try:
        for fn in os.listdir(STATUS_DIR):
            if not fn.endswith(".running"):
                continue
            try:
                with open(f"{STATUS_DIR}/{fn}") as f:
                    d = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("channel") == channel and d.get("ts") == ts:
                return d.get("thread_ts") or ts
    except OSError:
        pass
    try:
        r = web.conversations_replies(channel=channel, ts=ts, limit=1)
        msgs = r.data.get("messages", []) or []
        if msgs:
            return msgs[0].get("thread_ts") or msgs[0].get("ts") or ts
    except SlackApiError as e:
        log.info(f"resolve_thread_ts: replies({channel},{ts}) failed: {e.response.get('error')}")
    return ts


def handle_reaction_added(evt: dict) -> None:
    reaction = evt.get("reaction", "")
    if reaction not in STOP_REACTIONS:
        return
    user = evt.get("user", "")
    if user != AUTHORIZED_USER:
        return
    item = evt.get("item") or {}
    if item.get("type") != "message":
        return
    channel = item.get("channel")
    item_ts = item.get("ts")
    if not channel or not item_ts:
        return
    key = ("reaction_stop", channel, item_ts, user)
    with seen_lock:
        if key in seen_keys:
            return
        seen_keys.add(key)
    thread_ts = resolve_thread_ts(channel, item_ts)
    killed = do_stop(channel, thread_ts, item_ts, trigger_kind="reaction")
    log.info(f"STOP REACTION (:{reaction}:): {channel}/{item_ts} by {user} — killed {killed}")


def forward_thread_start(channel: str, ts: str, user: str, text: str, is_dm: bool) -> None:
    if not FORWARD_CHANNEL:
        return
    try:
        try:
            pr = web.chat_getPermalink(channel=channel, message_ts=ts)
            permalink = pr.data.get("permalink", "")
        except Exception:
            permalink = ""
        source = "DM" if is_dm else resolve_channel_label(channel, is_dm)
        display_text = (text or "").strip()[:400]
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*New thread* from <@{user}> in {source}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f">{display_text}"}},
        ]
        if permalink:
            blocks.append({"type": "actions", "elements": [{
                "type": "button", "text": {"type": "plain_text", "text": "Open thread"},
                "url": permalink,
            }]})
        web.chat_postMessage(
            channel=FORWARD_CHANNEL,
            text=f"New thread from <@{user}> in {source}: {display_text[:100]}",
            blocks=blocks,
        )
        log.info(f"FORWARD: new thread {channel}/{ts} by {user} → {FORWARD_CHANNEL}")
    except Exception as e:
        log.info(f"FORWARD: exception for {channel}/{ts}: {e}")


# ─── Dispatch ─────────────────────────────────────────────────────────────────

def should_process(evt: dict) -> bool:
    if evt.get("user") == BOT_USER_ID or evt.get("bot_id"):
        return False
    if evt.get("subtype") in SKIP_SUBTYPES:
        return False
    if not (evt.get("text") or "").strip():
        return False
    return True


def dispatch(channel: str, ts: str, user: str, text_preview: str, thread_ts: str,
             model: str = "", is_dm: bool = False, mention_text: str = "", fast: bool = False) -> None:
    key = (channel, ts)
    with seen_lock:
        if key in seen_keys:
            return
        seen_keys.add(key)

    running = running_claudes()
    if running >= MAX_PARALLEL:
        with backlog_lock:
            backlog.append((channel, ts, user, text_preview, thread_ts, model, is_dm, mention_text, fast))
        log.info(f"DEFERRED ({running}/{MAX_PARALLEL} running, backlog={len(backlog)}): {channel}/{ts} by {user}")
        return

    if not try_claim_eyes(channel, ts):
        log.info(f"SKIP already-claimed: {channel}/{ts}")
        return

    model_tag = f" [model={model}]" if model else ""
    fast_tag = " [thinking=off]" if fast else ""
    dm_tag = f" [dm={'1' if is_dm else '0'}]"
    log.info(f"NEW MENTION ({running + 1}/{MAX_PARALLEL}){model_tag}{fast_tag}{dm_tag}: {channel}/{ts} by {user}: {text_preview}")
    set_thinking_status(channel, thread_ts)
    spawn_worker(channel, ts, user, thread_ts, model, is_dm, mention_text, fast)


def backlog_drainer() -> None:
    while True:
        time.sleep(BACKLOG_DRAIN_INTERVAL)
        with backlog_lock:
            if not backlog:
                continue
            batch = list(backlog)
            backlog.clear()
        for channel, ts, user, text_preview, thread_ts, model, is_dm, mention_text, fast in batch:
            running = running_claudes()
            if running >= MAX_PARALLEL:
                with backlog_lock:
                    backlog.append((channel, ts, user, text_preview, thread_ts, model, is_dm, mention_text, fast))
                break
            with seen_lock:
                seen_keys.discard((channel, ts))
            dispatch(channel, ts, user, text_preview, thread_ts, model, is_dm, mention_text, fast)


def on_request(client: SocketModeClient, req: SocketModeRequest) -> None:
    try:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    except Exception as e:
        log.info(f"ack failed: {e}")

    if req.type != "events_api":
        return

    evt = (req.payload or {}).get("event") or {}
    etype = evt.get("type")

    if etype == "app_mention":
        pass
    elif etype == "message":
        if evt.get("channel_type") != "im":
            return
    elif etype == "reaction_added":
        handle_reaction_added(evt)
        return
    else:
        return

    if not should_process(evt):
        return

    channel = evt.get("channel")
    ts = evt.get("ts")
    user = evt.get("user", "?")
    if not channel or not ts:
        return

    text_preview = (evt.get("text") or "").replace("\n", " ")[:300]
    thread_ts = evt.get("thread_ts") or ts

    if is_status_query(evt.get("text") or ""):
        key = (channel, ts)
        with seen_lock:
            if key in seen_keys:
                return
            seen_keys.add(key)
        handle_status_query(channel, ts, user, thread_ts)
        return

    if is_stop_query(evt.get("text") or ""):
        key = (channel, ts)
        with seen_lock:
            if key in seen_keys:
                return
            seen_keys.add(key)
        handle_stop_mention(channel, ts, user, thread_ts)
        return

    if is_delete_trigger(evt.get("text") or ""):
        key = (channel, ts)
        with seen_lock:
            if key in seen_keys:
                return
            seen_keys.add(key)
        handle_delete_thread(channel, ts, user, thread_ts)
        return

    model = parse_model_trigger(evt.get("text") or "")
    fast = parse_fast_trigger(evt.get("text") or "")
    is_dm = evt.get("channel_type") == "im"

    _should_forward = False
    if is_dm:
        sess_path = f"{SESSION_DIR}/{thread_ts.replace('.', '_')}.json"
        _should_forward = not os.path.exists(sess_path)
    else:
        _should_forward = thread_ts == ts
    if _should_forward:
        forward_thread_start(channel, ts, user, evt.get("text") or "", is_dm)

    dispatch(channel, ts, user, text_preview, thread_ts, model, is_dm, evt.get("text") or "", fast)


def main() -> None:
    log.info("=== socket daemon starting ===")
    log.info(f"    install_dir={INSTALL_DIR}")
    log.info(f"    workspace={CLAUDE_WORKSPACE}")
    log.info(f"    bot_user={BOT_USER_ID}  authorized={AUTHORIZED_USER}")

    client = SocketModeClient(app_token=APP_TOKEN, web_client=web)
    client.socket_mode_request_listeners.append(on_request)

    def _shutdown(signum, _frame):
        log.info(f"=== socket daemon stopping (signal {signum}) ===")
        try:
            client.close()
        except Exception:
            pass
        try:
            _prefetch_executor.shutdown(wait=False, cancel_futures=True)
            _image_download_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    t = threading.Thread(target=backlog_drainer, daemon=True)
    t.start()

    client.connect()
    log.info("=== socket daemon connected ===")
    threading.Event().wait()


if __name__ == "__main__":
    main()
