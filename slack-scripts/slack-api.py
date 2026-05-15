#!/usr/bin/env python3
"""Slack API wrapper for claude-code-slack. All Slack interactions go through this script.

Usage: python3 slack-api.py <subcommand> [options]
Requires: SLACK_BOT_TOKEN env var

Subcommands: post, history, replies, search, user, channels, react, files, delete, update
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")


class SlackAPIError(Exception):
    def __init__(self, error_code, hint="", details=None):
        self.error_code = error_code
        self.hint = hint
        self.details = details
        super().__init__(error_code)


ERROR_HINTS = {
    "not_in_channel": "Add bot to channel first (/invite @YourBot)",
    "channel_not_found": "Check channel ID — use 'channels' subcommand to list available channels",
    "invalid_auth": "SLACK_BOT_TOKEN is invalid or expired",
    "token_revoked": "Token revoked — regenerate in Slack admin",
    "invalid_blocks": "Block Kit JSON malformed — validate at https://app.slack.com/block-kit-builder",
    "invalid_blocks_format": "Block Kit JSON malformed — validate at https://app.slack.com/block-kit-builder",
    "too_many_attachments": "Max 50 blocks per message — split into multiple messages",
    "msg_too_long": "Text exceeds 40k chars — split into multiple messages",
    "missing_scope": "Bot token lacks required scope — check OAuth scopes in Slack app settings",
    "ratelimited": "Rate limited by Slack — script retries automatically",
    "thread_not_found": "thread_ts does not match any existing message in this channel",
    "message_not_found": "Message not found — check channel and ts values",
    "no_text": "Must provide --text or --blocks-file with content",
    "not_authed": "No auth token provided — set SLACK_BOT_TOKEN",
}

_user_cache = {}


def _api_call(method, params=None, is_post=False, retry_on_ratelimit=True):
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {TOKEN}"}

    if is_post:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers)
    else:
        if params:
            import urllib.parse
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers=headers)

    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry_on_ratelimit:
            retry_after = int(e.headers.get("Retry-After", 5))
            print(json.dumps({"warning": f"Rate limited. Retrying in {retry_after}s..."}), file=sys.stderr)
            time.sleep(retry_after)
            return _api_call(method, params, is_post, retry_on_ratelimit=False)
        _fail(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        _fail(f"Network error: {e.reason}")

    result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        error_code = result.get("error", "unknown_error")
        hint = ERROR_HINTS.get(error_code, "")
        details = result.get("response_metadata", {}).get("messages")
        raise SlackAPIError(error_code, hint, details)
    return result


def _fail(message):
    print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
    sys.exit(1)


def _resolve_user(user_id):
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        result = _api_call("users.info", {"user": user_id})
        user = result.get("user", {})
        name = (user.get("profile", {}).get("display_name")
                or user.get("profile", {}).get("real_name")
                or user.get("real_name") or user_id)
        _user_cache[user_id] = name
        return name
    except SlackAPIError:
        _user_cache[user_id] = user_id
        return user_id


def _enrich_messages(messages):
    user_ids = {m.get("user") for m in messages if m.get("user")}
    for uid in user_ids:
        _resolve_user(uid)
    for m in messages:
        if m.get("user"):
            m["user_name"] = _user_cache.get(m["user"], m["user"])
    return messages


def _load_blocks_file(path):
    if not os.path.isfile(path):
        _fail(f"Blocks file not found: {path}")
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            _fail(f"Invalid JSON in blocks file: {e}")
    if isinstance(data, list):
        return None, data
    elif isinstance(data, dict):
        return data.get("text"), data.get("blocks", [])
    else:
        _fail("Blocks file must be a JSON array of blocks or an object with 'blocks' key")


def cmd_post(args):
    params = {"channel": args.channel}
    if args.blocks_file:
        fallback_text, blocks = _load_blocks_file(args.blocks_file)
        params["blocks"] = blocks
        params["text"] = args.text or fallback_text or ""
    elif args.text:
        params["text"] = args.text
    else:
        _fail("Must provide --text or --blocks-file")
    if args.thread_ts:
        params["thread_ts"] = args.thread_ts
    params["unfurl_links"] = args.unfurl
    params["unfurl_media"] = args.unfurl
    result = _api_call("chat.postMessage", params, is_post=True)
    print(json.dumps({"ok": True, "ts": result.get("ts"), "channel": result.get("channel")}, indent=2))


def cmd_history(args):
    params = {"channel": args.channel, "limit": args.limit}
    if args.oldest:
        params["oldest"] = args.oldest
    if args.latest:
        params["latest"] = args.latest
    result = _api_call("conversations.history", params)
    messages = _enrich_messages(result.get("messages", []))
    output = {"ok": True, "count": len(messages), "messages": messages}
    if result.get("has_more"):
        output["has_more"] = True
        if result.get("response_metadata", {}).get("next_cursor"):
            output["next_cursor"] = result["response_metadata"]["next_cursor"]
    print(json.dumps(output, indent=2))


def cmd_replies(args):
    params = {"channel": args.channel, "ts": args.ts, "limit": args.limit}
    result = _api_call("conversations.replies", params)
    messages = _enrich_messages(result.get("messages", []))
    output = {"ok": True, "count": len(messages), "messages": messages}
    if result.get("has_more"):
        output["has_more"] = True
    print(json.dumps(output, indent=2))


def cmd_search(args):
    params = {"query": args.query, "count": args.count, "sort": args.sort, "sort_dir": "desc"}
    result = _api_call("search.messages", params)
    matches = result.get("messages", {}).get("matches", [])
    output = {
        "ok": True, "count": len(matches),
        "total": result.get("messages", {}).get("total", 0),
        "matches": [{"text": m.get("text", ""), "user": m.get("user", ""),
                     "username": m.get("username", ""),
                     "channel": m.get("channel", {}).get("name", ""),
                     "ts": m.get("ts", ""), "permalink": m.get("permalink", "")}
                    for m in matches],
    }
    print(json.dumps(output, indent=2))


def cmd_user(args):
    result = _api_call("users.info", {"user": args.user_id})
    user = result.get("user", {})
    profile = user.get("profile", {})
    print(json.dumps({
        "ok": True, "id": user.get("id"), "name": user.get("name"),
        "real_name": user.get("real_name"), "display_name": profile.get("display_name"),
        "email": profile.get("email", ""), "title": profile.get("title", ""),
        "is_bot": user.get("is_bot", False),
    }, indent=2))


def cmd_channels(args):
    params = {"types": args.types, "exclude_archived": "true", "limit": 200}
    result = _api_call("conversations.list", params)
    channels = result.get("channels", [])
    print(json.dumps({
        "ok": True, "count": len(channels),
        "channels": [{"id": c.get("id"), "name": c.get("name"),
                      "is_private": c.get("is_private", False),
                      "purpose": c.get("purpose", {}).get("value", ""),
                      "num_members": c.get("num_members", 0)} for c in channels],
    }, indent=2))


def cmd_react(args):
    _api_call("reactions.add",
              {"channel": args.channel, "timestamp": args.ts, "name": args.name}, is_post=True)
    print(json.dumps({"ok": True}))


def cmd_files(args):
    import urllib.parse
    if not args.out:
        _fail("Must provide --out (absolute path to write the file to)")
    if not (args.file_id or args.url):
        _fail("Must provide --file-id or --url")
    if args.file_id and args.url:
        _fail("Provide --file-id OR --url, not both")

    meta = None
    if args.file_id:
        info = _api_call("files.info", {"file": args.file_id})
        meta = info.get("file", {})
        download_url = meta.get("url_private_download") or meta.get("url_private")
        if not download_url:
            _fail(f"File {args.file_id} has no download URL")
    else:
        download_url = args.url

    max_bytes = args.max_bytes
    if meta and meta.get("size", 0) > max_bytes:
        _fail(f"File is {meta['size']} bytes, exceeds --max-bytes={max_bytes}")

    req = urllib.request.Request(download_url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        _fail(f"Download failed: HTTP {e.code} {e.reason}")
    except urllib.error.URLError as e:
        _fail(f"Download failed: {e.reason}")

    content_type = resp.headers.get("Content-Type", "")
    if content_type.startswith("text/html"):
        _fail(f"Got HTML instead of file — token may lack files:read scope. Content-Type: {content_type}")

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    written = 0
    with open(out_path, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                f.close()
                os.unlink(out_path)
                _fail(f"File exceeded --max-bytes={max_bytes} mid-download")
            f.write(chunk)

    output = {"ok": True, "path": out_path, "bytes": written, "content_type": content_type}
    if meta:
        output.update({"mimetype": meta.get("mimetype"), "name": meta.get("name")})
    print(json.dumps(output, indent=2))


def cmd_delete(args):
    result = _api_call("chat.delete", {"channel": args.channel, "ts": args.ts}, is_post=True)
    print(json.dumps({"ok": True, "ts": result.get("ts"), "channel": result.get("channel")}, indent=2))


def cmd_update(args):
    params = {"channel": args.channel, "ts": args.ts}
    if args.blocks_file:
        fallback_text, blocks = _load_blocks_file(args.blocks_file)
        params["blocks"] = blocks
        params["text"] = args.text or fallback_text or ""
    elif args.text:
        params["text"] = args.text
    else:
        _fail("Must provide --text or --blocks-file")
    result = _api_call("chat.update", params, is_post=True)
    print(json.dumps({"ok": True, "ts": result.get("ts"), "channel": result.get("channel")}, indent=2))


def main():
    if not TOKEN:
        _fail("SLACK_BOT_TOKEN env var is not set")

    parser = argparse.ArgumentParser(description="Slack API wrapper for claude-code-slack")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("post", help="Post a message")
    p.add_argument("--channel", required=True)
    p.add_argument("--text")
    p.add_argument("--blocks-file")
    p.add_argument("--thread-ts")
    p.add_argument("--unfurl", action="store_true", default=False)

    p = sub.add_parser("history", help="Read channel history")
    p.add_argument("--channel", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--oldest")
    p.add_argument("--latest")

    p = sub.add_parser("replies", help="Read thread replies")
    p.add_argument("--channel", required=True)
    p.add_argument("--ts", required=True)
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("search", help="Search messages")
    p.add_argument("--query", required=True)
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--sort", default="relevance", choices=["relevance", "timestamp"])

    p = sub.add_parser("user", help="Look up a user")
    p.add_argument("--user-id", required=True)

    p = sub.add_parser("channels", help="List channels bot is in")
    p.add_argument("--types", default="public_channel,private_channel")

    p = sub.add_parser("react", help="Add a reaction")
    p.add_argument("--channel", required=True)
    p.add_argument("--ts", required=True)
    p.add_argument("--name", required=True)

    p = sub.add_parser("files", help="Download a Slack-hosted file")
    p.add_argument("--file-id")
    p.add_argument("--url")
    p.add_argument("--out", required=True)
    p.add_argument("--max-bytes", type=int, default=25 * 1024 * 1024)

    p = sub.add_parser("delete", help="Delete a message")
    p.add_argument("--channel", required=True)
    p.add_argument("--ts", required=True)

    p = sub.add_parser("update", help="Update a message")
    p.add_argument("--channel", required=True)
    p.add_argument("--ts", required=True)
    p.add_argument("--text")
    p.add_argument("--blocks-file")

    args = parser.parse_args()
    commands = {
        "post": cmd_post, "history": cmd_history, "replies": cmd_replies,
        "search": cmd_search, "user": cmd_user, "channels": cmd_channels,
        "react": cmd_react, "files": cmd_files, "delete": cmd_delete, "update": cmd_update,
    }
    try:
        commands[args.command](args)
    except SlackAPIError as e:
        output = {"ok": False, "error": e.error_code}
        if e.hint:
            output["hint"] = e.hint
        if e.details:
            output["details"] = e.details
        print(json.dumps(output, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
