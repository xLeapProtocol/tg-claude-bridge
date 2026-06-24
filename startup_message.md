# 🤖 Bridge online

Claude Code is reachable from this thread. Below is everything the bridge can do.

## Slash commands

| Command | Effect |
|:--------|:-------|
| `/new` | Reset the conversation in the current working directory |
| `/new <path>` | Reset the conversation **and** switch to `<path>` as the working directory |
| `/clear` | Same as `/new` without changing the working directory |
| `/compact` | Ask Claude to summarise the conversation so far and continue with the summary as context |
| `/steer <text>` | Inject `<text>` into the current run *while Claude is still thinking* — no need to wait for the reply |
| `/usage` | Show remaining 5 h / 7 d subscription quota **and** cumulative token / cost stats (this chat + global) |
| `/list` | List every tmux pane on this host that is running Claude Code |
| `/connect <session> <pane>` | Bridge this thread directly to a live Claude pane (`/connect mywork 1.0`) |
| `/disconnect` | Detach from the tmux pane and go back to the normal bridge |

## Conversation model

- Each **Telegram thread** has its own `claude -p` conversation, working directory, and token-usage counter — they don't bleed into each other.
- Conversation ids survive restarts (persisted in `.tg_chat_map.json`), so the bot remembers where you left off.
- Hit the **🛑 Stop** button on the *Thinking…* message to abort the current run.

## Send any file — it just lands on disk

- **Documents / photos / videos** → auto-downloaded to `.tg_downloads/` and the path is fed to Claude in the next prompt.
- **Voice notes** → transcribed locally with `faster-whisper` (base model on CPU) and prepended to the message as `[Voice transcript: …]`.
- Claude can push files back to you with:

```bash
python3 tg_send.py --file  /abs/path "optional caption"
python3 tg_send.py --photo /abs/path "optional caption"
python3 tg_send.py "plain text notification"
python3 tg_send.py --rich  "**markdown** _notification_"
```

## Rich formatting (Bot API 10.1, June 2026)

Every reply is sent through the new `sendRichMessage` endpoint, so Claude can use the full CommonMark vocabulary natively:

- **Headings** `#` `##` `###` for sections
- **Tables** `| col | col |` with alignment markers `:---`, `:---:`, `---:`
- **Fenced code** with language tags — `python`, `rust`, `sql`, `bash`, …
- **LaTeX** — inline `$E=mc^2$` and display `$$ \int_0^\infty e^{-x^2}\,dx = \tfrac{\sqrt{\pi}}{2} $$`
- **Task lists** `- [ ]` / `- [x]`
- **Spoilers** `||hidden||`, strikethrough `~~struck~~`, blockquotes `> …`

### Mini demo

```python
async def hello():
    print("rich formatting in three lines")
```

| Feature | Status |
|:--------|:------:|
| Markdown headings | ✅ |
| Tables            | ✅ |
| LaTeX             | ✅ |
| Voice transcription | ✅ |
| tmux pane bridge  | ✅ |

$$ \nabla \cdot \mathbf{E} = \frac{\rho}{\varepsilon_0} $$

> Ready when you are — just type a message.
