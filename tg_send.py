#!/usr/bin/env python3
"""
Out-of-band Telegram sender for the Claude agent running inside the bridge.

The bridge spawns `claude -p` with TG_BOT_TOKEN / TG_CHAT_ID / TG_THREAD_ID in
the environment. Claude's normal text reply is already relayed to Telegram by
the bridge, but it has no way to push *files* or *explicit notifications* on its
own. This script is that channel: it always delivers to the chat/thread named by
the TG_* environment variables.

Usage:
    python3 tg_send.py "a plain text notification"
    python3 tg_send.py --file  /abs/path/to/file [optional caption]
    python3 tg_send.py --photo /abs/path/to/image [optional caption]
"""

import json
import mimetypes
import os
import sys
import urllib.request
import urllib.error
import uuid

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
THREAD_ID = os.environ.get("TG_THREAD_ID", "")


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TOKEN}/{method}"


def _base_params() -> dict:
    params = {"chat_id": CHAT_ID}
    if THREAD_ID:
        params["message_thread_id"] = THREAD_ID
    return params


def send_text(text: str) -> dict:
    import urllib.parse
    params = _base_params()
    params["text"] = text
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(_api_url("sendMessage"), data=data)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _multipart(fields: dict, file_field: str, path: str) -> tuple[bytes, str]:
    """Build a multipart/form-data body."""
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    body = []
    for name, value in fields.items():
        body.append(b"--" + boundary.encode())
        body.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        body.append(b"")
        body.append(str(value).encode())
    filename = os.path.basename(path)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        file_data = f.read()
    body.append(b"--" + boundary.encode())
    body.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode()
    )
    body.append(f"Content-Type: {ctype}".encode())
    body.append(b"")
    body.append(file_data)
    body.append(b"--" + boundary.encode() + b"--")
    body.append(b"")
    return crlf.join(body), boundary


def send_file(path: str, caption: str = "", as_photo: bool = False) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    method = "sendPhoto" if as_photo else "sendDocument"
    file_field = "photo" if as_photo else "document"
    fields = _base_params()
    if caption:
        fields["caption"] = caption
    body, boundary = _multipart(fields, file_field, path)
    req = urllib.request.Request(_api_url(method), data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def main(argv: list[str]) -> int:
    if not TOKEN or not CHAT_ID:
        print("tg_send: TG_BOT_TOKEN / TG_CHAT_ID not set in environment", file=sys.stderr)
        return 1
    if not argv:
        print(__doc__)
        return 1

    try:
        if argv[0] in ("--file", "--photo"):
            as_photo = argv[0] == "--photo"
            if len(argv) < 2:
                print("tg_send: missing file path", file=sys.stderr)
                return 1
            path = os.path.abspath(os.path.expanduser(argv[1]))
            caption = " ".join(argv[2:])
            send_file(path, caption, as_photo=as_photo)
            print(f"tg_send: delivered {path}")
        else:
            send_text(" ".join(argv))
            print("tg_send: notification delivered")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        print(f"tg_send: failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
