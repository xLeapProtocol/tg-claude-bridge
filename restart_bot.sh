#!/usr/bin/env bash
# Detached helper: restart the tg-bridge bot in tmux, then send a Telegram alert.
# Run detached (setsid/nohup) so it survives the restart killing the caller.
set -u
cd /home/$USER/tg-claude-bridge || exit 1

sleep 3
tmux kill-session -t tg-bridge 2>/dev/null
sleep 1
# Ensure ~/.local/bin (typical Claude CLI install location) is on PATH so the
# bot's subprocess can find `claude` regardless of how the shell was launched.
tmux new -d -s tg-bridge 'export PATH="$HOME/.local/bin:$PATH"; set -a; . ./.tg-bridge.env; set +a; exec python3 tg.py'
sleep 2

# Load env and notify the configured thread that the restart is done.
set -a
# shellcheck disable=SC1091
. ./.tg-bridge.env
set +a
python3 tg_send.py --rich-file ./startup_message.md

