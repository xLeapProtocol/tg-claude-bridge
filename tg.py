#!/usr/bin/env python3
"""
Telegram <-> Claude CLI bridge (multi-chat).

Polls Telegram for new messages from ALL chats, spawns a dedicated
worker thread per chat, and routes messages to the right Claude
conversation.

Environment variables:
    TG_BOT_TOKEN  — bot token from @BotFather
    TG_ALLOWED_CHATS — (optional) comma-separated list of allowed chat IDs.
                        If empty, all chats are accepted.
"""

import json
import os
import subprocess
from datetime import datetime
import sys
import tempfile
import threading
import time
import queue
import urllib.request
import urllib.parse
import urllib.error

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
ALLOWED_CHATS = {
    cid.strip()
    for cid in os.environ.get("TG_ALLOWED_CHATS", "").split(",")
    if cid.strip()
}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tg_last_update")
CHAT_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tg_chat_map.json")
WORK_DIR = os.path.dirname(os.path.abspath(__file__))

BATCH_WAIT = 3  # seconds to wait for more messages after first one
THINKING_UPDATE_INTERVAL = 2


# ---------------------------------------------------------------------------
# Logging — output to stdout AND registered Telegram chats
# ---------------------------------------------------------------------------

def _parse_key(key):
    """Parse a 'chat_id:thread_id' key into (chat_id, thread_id)."""
    if ":" in key:
        cid, tid = key.split(":", 1)
        return cid, int(tid)
    return key, None


def log(msg, key=None):
    """Log a message to stdout only."""
    print(msg)


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def api(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def api_json(method, payload):
    """Send request with JSON body (needed for inline keyboards)."""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# File download helpers
# ---------------------------------------------------------------------------

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tg_downloads")


def download_tg_file(file_id: str, filename: str | None = None) -> str | None:
    """Download a file from Telegram by file_id. Returns local path or None."""
    try:
        result = api("getFile", file_id=file_id)
        file_path = result.get("result", {}).get("file_path", "")
        if not file_path:
            return None
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        ext = os.path.splitext(file_path)[1] or ""
        if filename:
            local_name = filename
        else:
            local_name = f"{file_id}{ext}"
        local_path = os.path.join(DOWNLOAD_DIR, local_name)
        urllib.request.urlretrieve(url, local_path)
        return local_path
    except Exception as e:
        log(f"[download error: {e}]")
        return None


def extract_file_from_message(msg: dict) -> tuple[str | None, str | None]:
    """Extract file_id and display name from a Telegram message.
    Returns (file_id, filename) or (None, None)."""
    # Document (any file type)
    doc = msg.get("document")
    if doc:
        return doc["file_id"], doc.get("file_name")

    # Photo — take the largest resolution
    photos = msg.get("photo")
    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        return best["file_id"], None

    # Audio
    audio = msg.get("audio")
    if audio:
        return audio["file_id"], audio.get("file_name")

    # Voice
    voice = msg.get("voice")
    if voice:
        return voice["file_id"], None

    # Video
    video = msg.get("video")
    if video:
        return video["file_id"], video.get("file_name")

    # Video note (round video)
    vnote = msg.get("video_note")
    if vnote:
        return vnote["file_id"], None

    # Sticker
    sticker = msg.get("sticker")
    if sticker:
        return sticker["file_id"], None

    return None, None


# ---------------------------------------------------------------------------
# Update offset persistence
# ---------------------------------------------------------------------------

_offset_lock = threading.Lock()


def load_offset():
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_offset(offset):
    with _offset_lock:
        with open(STATE_FILE, "w") as f:
            f.write(str(offset))


# ---------------------------------------------------------------------------
# Chat <-> Conversation ID persistence
# ---------------------------------------------------------------------------

_chat_map_lock = threading.Lock()
_chat_map: dict[str, dict] = {}  # chat_id -> {"conv_id": ..., "work_dir": ...}


def _load_chat_map():
    global _chat_map
    try:
        with open(CHAT_MAP_FILE) as f:
            raw = json.load(f)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        _chat_map = {}
        return
    # Migrate old format (chat_id -> conv_id string) to new format
    _chat_map = {}
    for k, v in raw.items():
        if isinstance(v, str):
            _chat_map[k] = {"conv_id": v, "work_dir": WORK_DIR}
        elif isinstance(v, dict):
            _chat_map[k] = v
        else:
            _chat_map[k] = {"conv_id": None, "work_dir": WORK_DIR}


def _save_chat_map():
    with _chat_map_lock:
        with open(CHAT_MAP_FILE, "w") as f:
            json.dump(_chat_map, f, indent=2)


def get_conversation_id(chat_id: str) -> str | None:
    entry = _chat_map.get(chat_id)
    return entry.get("conv_id") if entry else None


def set_conversation_id(chat_id: str, conv_id: str | None):
    if chat_id not in _chat_map:
        _chat_map[chat_id] = {"conv_id": conv_id, "work_dir": WORK_DIR}
    else:
        _chat_map[chat_id]["conv_id"] = conv_id
    _save_chat_map()


_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
)


def _empty_usage() -> dict:
    acc = {"runs": 0, "cost_usd": 0.0}
    for f in _USAGE_FIELDS:
        acc[f] = 0
    return acc


def add_usage(chat_id: str, cost: float, usage: dict):
    """Accumulate token/cost stats for a chat from a Claude `result` event."""
    if chat_id not in _chat_map:
        _chat_map[chat_id] = {"conv_id": None, "work_dir": WORK_DIR}
    acc = _chat_map[chat_id].setdefault("usage", _empty_usage())
    acc["runs"] += 1
    acc["cost_usd"] += cost or 0.0
    acc["input_tokens"] += usage.get("input_tokens", 0)
    acc["output_tokens"] += usage.get("output_tokens", 0)
    acc["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
    acc["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
    _save_chat_map()


def get_usage(chat_id: str) -> dict | None:
    entry = _chat_map.get(chat_id)
    return entry.get("usage") if entry else None


def get_global_usage() -> dict:
    """Sum usage across all known chats."""
    total = _empty_usage()
    for entry in _chat_map.values():
        u = entry.get("usage")
        if not u:
            continue
        total["runs"] += u.get("runs", 0)
        total["cost_usd"] += u.get("cost_usd", 0.0)
        for f in _USAGE_FIELDS:
            total[f] += u.get(f, 0)
    return total


def get_work_dir(chat_id: str) -> str:
    entry = _chat_map.get(chat_id)
    return entry.get("work_dir", WORK_DIR) if entry else WORK_DIR


def set_work_dir(chat_id: str, work_dir: str):
    if chat_id not in _chat_map:
        _chat_map[chat_id] = {"conv_id": None, "work_dir": work_dir}
    else:
        _chat_map[chat_id]["work_dir"] = work_dir
    _save_chat_map()


# ---------------------------------------------------------------------------
# Per-chat message sending
# ---------------------------------------------------------------------------

def send_message(key, text):
    chat_id, thread_id = _parse_key(key)
    chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)]
    for chunk in chunks:
        params = {"chat_id": chat_id, "text": chunk}
        if thread_id:
            params["message_thread_id"] = thread_id
        try:
            api("sendMessage", parse_mode="Markdown", **params)
        except urllib.error.HTTPError:
            api("sendMessage", **params)


def _stop_keyboard():
    return {"inline_keyboard": [[{"text": "\U0001f6d1 Stop", "callback_data": "force_stop"}]]}


def send_message_with_id(key, text, with_stop_button=False):
    chat_id, thread_id = _parse_key(key)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    if with_stop_button:
        payload["reply_markup"] = _stop_keyboard()
    try:
        resp = api_json("sendMessage", payload)
    except urllib.error.HTTPError:
        payload.pop("parse_mode", None)
        resp = api_json("sendMessage", payload)
    return resp.get("result", {}).get("message_id")


def edit_message(key, message_id, text, with_stop_button=False):
    if not message_id:
        return
    chat_id, _ = _parse_key(key)
    text = text[:4096]
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if with_stop_button:
        payload["reply_markup"] = _stop_keyboard()
    else:
        payload["reply_markup"] = {"inline_keyboard": []}
    try:
        api_json("editMessageText", payload)
    except urllib.error.HTTPError:
        payload.pop("parse_mode", None)
        try:
            api_json("editMessageText", payload)
        except urllib.error.HTTPError:
            pass


# ---------------------------------------------------------------------------
# Tool formatting
# ---------------------------------------------------------------------------

def _format_tool_line(name, inp):
    if name == "Read":
        path = inp.get("file_path", "")
        short_path = "/".join(path.rsplit("/", 2)[-2:]) if "/" in path else path
        return f"\U0001f4d6 *Read* `{short_path}`"
    elif name == "Glob":
        return f"\U0001f50d *Glob* `{inp.get('pattern', '?')}`"
    elif name == "Grep":
        pat = inp.get("pattern", "?")
        path = inp.get("path", "")
        suffix = f" in `{path.rsplit('/', 1)[-1]}`" if path else ""
        return f"\U0001f50d *Grep* `{pat}`{suffix}"
    elif name == "Edit":
        path = inp.get("file_path", "")
        short_path = "/".join(path.rsplit("/", 2)[-2:]) if "/" in path else path
        return f"\u270f\ufe0f *Edit* `{short_path}`"
    elif name == "Write":
        path = inp.get("file_path", "")
        short_path = "/".join(path.rsplit("/", 2)[-2:]) if "/" in path else path
        return f"\U0001f4dd *Write* `{short_path}`"
    elif name == "Bash":
        cmd = inp.get("command", "?")
        if len(cmd) > 60:
            cmd = cmd[:57] + "\u2026"
        return f"\U0001f4bb *Bash* `{cmd}`"
    elif name == "Agent":
        desc = inp.get("description", inp.get("prompt", "?")[:40])
        return f"\U0001f916 *Agent* {desc}"
    else:
        short = json.dumps(inp, ensure_ascii=False)
        if len(short) > 80:
            short = short[:77] + "\u2026"
        return f"\U0001f527 *{name}* `{short}`"


def _format_usage(u: dict, title: str) -> str:
    total_in = u["input_tokens"] + u["cache_read_tokens"] + u["cache_creation_tokens"]
    return (
        f"*{title}*\n"
        f"Runs: {u['runs']}\n"
        f"Est. cost: ${u['cost_usd']:.4f}\n"
        f"Input tokens: {total_in:,}\n"
        f"  • fresh: {u['input_tokens']:,}\n"
        f"  • cache read: {u['cache_read_tokens']:,}\n"
        f"  • cache write: {u['cache_creation_tokens']:,}\n"
        f"Output tokens: {u['output_tokens']:,}"
    )


# ---------------------------------------------------------------------------
# Remaining subscription quota (5h / weekly windows)
#
# Undocumented: the interactive `/usage` command sources its data from this
# OAuth endpoint, which `claude -p` does NOT expose. We reuse the OAuth token
# Claude Code stores locally. This is best-effort and may break on any Claude
# Code / API change.
# ---------------------------------------------------------------------------

CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _fmt_reset(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%a %H:%M")
    except (ValueError, TypeError):
        return iso


def _fmt_window(label: str, w: dict | None) -> str | None:
    if not w or w.get("utilization") is None:
        return None
    used = w["utilization"]
    left = max(0.0, 100.0 - used)
    line = f"{label}: *{left:.0f}%* left ({used:.0f}% used)"
    if w.get("resets_at"):
        line += f", resets {_fmt_reset(w['resets_at'])}"
    return line


def fetch_remaining_quota() -> tuple[str | None, str | None]:
    """Best-effort fetch of subscription limit windows. Returns (text, error)."""
    try:
        with open(CREDENTIALS_FILE) as f:
            token = json.load(f)["claudeAiOauth"]["accessToken"]
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as e:
        return None, f"no OAuth credentials ({e})"

    req = urllib.request.Request(OAUTH_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "token expired — run any claude command to refresh"
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return None, f"request failed: {e}"

    windows = (
        ("5h", "five_hour"),
        ("7d", "seven_day"),
        ("7d Opus", "seven_day_opus"),
        ("7d Sonnet", "seven_day_sonnet"),
    )
    lines = [ln for label, key in windows
             if (ln := _fmt_window(label, data.get(key)))]
    if not lines:
        return None, "no window data in response"
    return "\n".join(lines), None


# ---------------------------------------------------------------------------
# Claude CLI streaming call
# ---------------------------------------------------------------------------

def call_claude_streaming(chat_id, message, conversation_id=None,
                          thinking_msg_id=None, stop_event=None, work_dir=None):
    """Run claude CLI in streaming mode. Returns (response_text, conversation_id)."""
    cwd = work_dir or WORK_DIR
    env = os.environ.copy()
    env["IS_SANDBOX"] = "1"
    cmd = ["claude", "-p", "--verbose", "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--effort", "medium"]
    if conversation_id:
        cmd.extend(["--resume", conversation_id])
    cmd.append(message)

    if stop_event is None:
        stop_event = threading.Event()

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=env,
        )
    except FileNotFoundError:
        return "[error: claude CLI not found in PATH]", conversation_id

    thinking_text = ""
    tool_lines = []
    response_text = ""
    conv_id = conversation_id
    last_update = 0
    current_block_type = None
    current_tool_name = ""
    current_tool_input = ""

    def _build_display(final=False):
        parts = []
        parts.append("\u2705 *Done*" if final else "\u23f3 *Working\u2026*")
        if thinking_text:
            t = thinking_text if len(thinking_text) <= 1500 else "\u2026" + thinking_text[-1500:]
            parts.append(f"\n\U0001f4ad _{t}_")
        if tool_lines:
            parts.append("")
            parts.extend(tool_lines[-20:])
        display = "\n".join(parts)
        return display[-3900:] if len(display) > 3900 else display

    def update_thinking_msg(final=False):
        nonlocal last_update
        if not thinking_msg_id:
            return
        if not thinking_text and not tool_lines:
            return
        now = time.time()
        if not final and now - last_update < THINKING_UPDATE_INTERVAL:
            return
        edit_message(chat_id, thinking_msg_id, _build_display(final),
                     with_stop_button=not final)
        last_update = now

    def _add_tool(name, inp):
        if isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except (json.JSONDecodeError, TypeError):
                inp = {}
        if not isinstance(inp, dict):
            inp = {}
        line = _format_tool_line(name, inp)
        tool_lines.append(line)
        log(f"  [{chat_id}] {line}", chat_id)

    try:
        for line in proc.stdout:
            if stop_event.is_set():
                proc.kill()
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "message":
                for block in event.get("content", []):
                    btype = block.get("type", "")
                    if btype == "thinking":
                        t = block.get("thinking", "")
                        if t:
                            thinking_text += t
                            update_thinking_msg()
                    elif btype == "tool_use":
                        _add_tool(block.get("name", "unknown"), block.get("input", {}))
                        update_thinking_msg()
                    elif btype == "text":
                        response_text += block.get("text", "")

            elif etype == "content_block_start":
                block = event.get("content_block", {})
                current_block_type = block.get("type", "")
                current_tool_input = ""
                if current_block_type == "tool_use":
                    current_tool_name = block.get("name", "unknown")

            elif etype == "content_block_stop":
                if current_block_type == "tool_use":
                    _add_tool(current_tool_name, current_tool_input)
                    update_thinking_msg()
                current_block_type = None

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                dtype = delta.get("type", "")
                if dtype == "thinking_delta":
                    thinking_text += delta.get("thinking", "")
                    update_thinking_msg()
                elif dtype == "text_delta":
                    response_text += delta.get("text", "")
                elif dtype == "input_json_delta":
                    current_tool_input += delta.get("partial_json", "")

            elif etype == "assistant":
                msg = event.get("message", "")
                if isinstance(msg, dict):
                    for block in msg.get("content", []):
                        btype = block.get("type", "")
                        if btype == "thinking":
                            t = block.get("thinking", "")
                            if t:
                                thinking_text += t
                                update_thinking_msg()
                        elif btype == "text":
                            txt = block.get("text", "")
                            if txt:
                                tool_lines.append(f"\U0001f4ac {txt[:200]}")
                                update_thinking_msg()
                        elif btype == "tool_use":
                            _add_tool(block.get("name", "unknown"), block.get("input", {}))
                            update_thinking_msg()
                elif msg:
                    tool_lines.append(f"\U0001f4ac {msg}")
                    update_thinking_msg()

            elif etype == "result":
                conv_id = event.get("session_id", conv_id)
                add_usage(chat_id, event.get("total_cost_usd", 0.0),
                          event.get("usage", {}) or {})
                if not response_text:
                    response_text = event.get("result", "")

        proc.wait(timeout=300)
        update_thinking_msg(final=True)

        if stop_event.is_set():
            return "\U0001f6d1 *Stopped by user.*", conv_id

        if proc.returncode != 0:
            stderr = proc.stderr.read()
            if stderr and not response_text:
                return stderr.strip(), conv_id

        return response_text.strip() if response_text else "[empty response]", conv_id

    except Exception as e:
        proc.kill()
        return f"[error: {e}]", conv_id


# ---------------------------------------------------------------------------
# Per-chat worker thread
# ---------------------------------------------------------------------------

class ChatWorker:
    """Manages a dedicated thread for one Telegram chat."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.msg_queue: queue.Queue[str] = queue.Queue()
        self.steer_queue: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"chat-{chat_id}")
        self.thread.start()
        log(f"[ChatWorker spawned for chat {chat_id}]", chat_id)

    def enqueue(self, text: str):
        self.msg_queue.put(text)

    def enqueue_steer(self, text: str):
        self.steer_queue.put(text)

    def request_stop(self):
        self.stop_event.set()

    def _run(self):
        """Main loop for this chat's worker thread."""
        conversation_id = get_conversation_id(self.chat_id)
        self.work_dir = get_work_dir(self.chat_id)
        if conversation_id:
            log(f"  [{self.chat_id}] resumed conv {conversation_id} in {self.work_dir}", self.chat_id)

        while True:
            # Block until at least one message arrives
            try:
                first = self.msg_queue.get(timeout=60)
            except queue.Empty:
                continue
            messages = [first]

            # Batch: collect more messages for BATCH_WAIT seconds
            deadline = time.time() + BATCH_WAIT
            while time.time() < deadline:
                try:
                    messages.append(self.msg_queue.get(timeout=max(0, deadline - time.time())))
                except queue.Empty:
                    break

            combined = "\n".join(messages)
            log(f"[{self.chat_id}] received {len(messages)} msg(s): {combined[:80]}...", self.chat_id)

            # /clear resets conversation only
            if combined.strip() == "/clear":
                conversation_id = None
                set_conversation_id(self.chat_id, None)
                send_message(self.chat_id, "\U0001f504 Conversation cleared.")
                continue

            # /new [path] — reset conversation and optionally set working directory
            if combined.strip() == "/new" or combined.strip().startswith("/new "):
                arg = combined.strip()[len("/new"):].strip()
                conversation_id = None
                set_conversation_id(self.chat_id, None)
                if arg:
                    target = os.path.expanduser(arg)
                    target = os.path.abspath(target)
                    if not os.path.isdir(target):
                        send_message(self.chat_id,
                                     f"\u274c Directory not found: `{target}`")
                        continue
                    self.work_dir = target
                    set_work_dir(self.chat_id, target)
                    send_message(self.chat_id,
                                 f"\U0001f504 New conversation in `{target}`")
                else:
                    send_message(self.chat_id, "\U0001f504 Conversation cleared.")
                continue

            # /usage — remaining subscription quota + cumulative spend
            if combined.strip() == "/usage":
                quota, qerr = fetch_remaining_quota()
                if quota:
                    parts = ["\U0001f6e0 *Remaining (subscription limits)*\n" + quota]
                else:
                    parts = [f"\U0001f6e0 *Remaining:* unavailable — {qerr}"]
                g = get_global_usage()
                parts.append(_format_usage(g, "\U0001f4ca Spent — all chats (cumulative)"))
                mine = get_usage(self.chat_id)
                if mine:
                    parts.append(_format_usage(mine, "\U0001f464 Spent — this chat"))
                send_message(self.chat_id, "\n\n".join(parts))
                continue

            # /compact
            if combined.strip() == "/compact":
                if not conversation_id:
                    send_message(self.chat_id,
                                 "\u2139\ufe0f Nothing to compact \u2014 no active conversation.")
                    continue
                combined = (
                    "Please provide a very brief summary of our entire conversation so far, "
                    "then use that as context going forward. Be as concise as possible."
                )

            # /steer outside processing — just forward as a message
            if combined.strip().startswith("/steer"):
                steer_text = combined.strip()[len("/steer"):].strip()
                if not steer_text:
                    send_message(self.chat_id,
                                 "Usage: /steer <additional context or instructions>")
                    continue
                combined = steer_text

            # Reset stop flag before each call
            self.stop_event.clear()

            thinking_msg_id = send_message_with_id(
                self.chat_id, "\U0001f4ad *Thinking...*", with_stop_button=True)

            response, conversation_id = call_claude_streaming(
                self.chat_id, combined, conversation_id, thinking_msg_id,
                self.stop_event, self.work_dir,
            )
            set_conversation_id(self.chat_id, conversation_id)
            log(f"[{self.chat_id}] conv={conversation_id} resp: {response[:80]}...", self.chat_id)
            send_message(self.chat_id, response)

            # Process any /steer messages that arrived during processing
            while not self.steer_queue.empty():
                try:
                    steer_msg = self.steer_queue.get_nowait()
                except queue.Empty:
                    break
                log(f"[{self.chat_id}] delivering steer: {steer_msg[:60]}", self.chat_id)
                self.stop_event.clear()
                thinking_msg_id = send_message_with_id(
                    self.chat_id, "\U0001f9ed *Steering...*", with_stop_button=True)
                response, conversation_id = call_claude_streaming(
                    self.chat_id, steer_msg, conversation_id, thinking_msg_id,
                    self.stop_event, self.work_dir,
                )
                set_conversation_id(self.chat_id, conversation_id)
                send_message(self.chat_id, response)


# ---------------------------------------------------------------------------
# Central dispatcher
# ---------------------------------------------------------------------------

_workers: dict[str, ChatWorker] = {}
_workers_lock = threading.Lock()


def get_worker(chat_id: str) -> ChatWorker:
    with _workers_lock:
        if chat_id not in _workers:
            _workers[chat_id] = ChatWorker(chat_id)
        return _workers[chat_id]


def poll_loop():
    """Central polling loop — reads ALL updates and dispatches to workers."""
    while True:
        try:
            offset = load_offset()
            params = {"timeout": 30,
                      "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset:
                params["offset"] = offset
            result = api("getUpdates", **params)
        except (urllib.error.URLError, OSError) as e:
            log(f"[poll error: {e}, retrying...]")
            time.sleep(2)
            continue

        max_id = offset
        for update in result.get("result", []):
            uid = update["update_id"]
            if uid >= max_id:
                max_id = uid + 1

            # --- Callback queries (stop button) ---
            cb = update.get("callback_query")
            if cb and cb.get("data") == "force_stop":
                cb_msg = cb.get("message", {})
                cb_chat_id = str(cb_msg.get("chat", {}).get("id", ""))
                cb_thread_id = cb_msg.get("message_thread_id")
                cb_key = f"{cb_chat_id}:{cb_thread_id}" if cb_thread_id else cb_chat_id
                try:
                    api("answerCallbackQuery",
                        callback_query_id=cb["id"], text="Stopping...")
                except Exception:
                    pass
                if cb_key in _workers:
                    log(f"[force stop for {cb_key}]", cb_key)
                    _workers[cb_key].request_stop()
                continue

            # --- Regular messages ---
            msg = update.get("message", {})
            if not msg:
                continue
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "") or msg.get("caption", "") or ""

            # Check for file attachments
            file_id, file_name = extract_file_from_message(msg)

            if not chat_id or (not text and not file_id):
                continue

            # Download file and build message
            if file_id:
                local_path = download_tg_file(file_id, file_name)
                if local_path:
                    file_note = f"[File uploaded: {local_path}]"
                    if file_name:
                        file_note = f"[File uploaded: {file_name} -> {local_path}]"
                    text = f"{file_note}\n{text}" if text else file_note
                else:
                    text = f"[File upload failed to download]\n{text}" if text else "[File upload failed to download]"

            # Ignore messages from the "General" topic (no thread ID)
            thread_id = msg.get("message_thread_id")
            if not thread_id:
                continue

            # Build composite key: chat_id:thread_id
            key = f"{chat_id}:{thread_id}"

            # Allowlist filter (checks chat_id, not the thread)
            if ALLOWED_CHATS and chat_id not in ALLOWED_CHATS:
                log(f"  [update {uid}] chat={chat_id} BLOCKED (not in ALLOWED_CHATS)")
                continue

            log(f"  [update {uid}] {key} text={text[:40]!r}", key)

            worker = get_worker(key)

            # /steer during processing
            if text.strip().startswith("/steer"):
                steer_text = text.strip()[len("/steer"):].strip()
                if steer_text:
                    worker.enqueue_steer(steer_text)
                    send_message(key,
                                 "\U0001f9ed Steering received, will deliver after current response.")
                else:
                    worker.enqueue(text)
            else:
                worker.enqueue(text)

        if max_id > offset:
            save_offset(max_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        print("Error: TG_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    _load_chat_map()

    if ALLOWED_CHATS:
        log(f"[tg-claude bridge started, allowed chats: {ALLOWED_CHATS}]")
    else:
        log("[tg-claude bridge started, accepting ALL chats]")

    poll_loop()


if __name__ == "__main__":
    main()
