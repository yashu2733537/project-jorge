# jorge — your personal AI assistant

A terminal AI assistant that chats naturally, sends emails, searches the web, sorts files, remembers things, can edit its own code — and optionally runs as a WhatsApp bot.

## What it can do

- **Chat** — natural conversation with memory across sessions (`conversations.jsonl`)
- **Email** — AI-drafted emails sent via Gmail SMTP
- **Email drafting** — tone-aware drafts (professional/friendly/casual/formal/warm/direct/urgent) with optional conversation-context recall, reviewed before sending
- **Web search** — DuckDuckGo search, with AI-summarized answers
- **Web research** — deep searches summarized with numbered source citations
- **File sorting** — organizes a folder into categories by file type
- **File organization** — organizes by file type *and* naming patterns (IMG_, Screenshot, invoice, resume, …)
- **Memory** — remembers notes you give it (`memory.txt`)
- **System monitoring** — CPU/memory/disk usage, load, top processes, with configurable alert thresholds
- **Shell / file ops** — runs commands, reads/writes/lists files (ask it nicely)
- **Self-awareness** — knows its own code (auto-generated index) and can inspect, edit, and debug its own source
- **Delegation** — hands big coding tasks to `opencode` (if installed), which does them fully autonomously
- **Task breakdown** — splits a complex task into sub-steps, then delegates them
- **Self-improvement** — tell it "build yourself new skills" and it proposes + creates real opencode skills
- **WhatsApp bot** (optional) — talk to it from your own WhatsApp via the self-chat

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- An AI API key — any OpenAI-compatible endpoint works. Free option: OpenCode Zen (`https://opencode.ai`)
- **WhatsApp bot only**: Node.js 18+ (`cd whatsapp-bot && npm install`)
- **Delegation only**: the `opencode` CLI on your PATH

## Setup (terminal assistant)

```bash
# 1. Get the files, then install deps
pip install -r requirements.txt

# 2. Create your config from the template
cp .env.example .env
#    edit .env: AI_API_KEY (required), SMTP_* (only if you want email)

# 3. Run it
python3 assistant.py chat          # interactive chat
python3 assistant.py email --to friend@example.com --subject "Hi" --topic "say hello"
python3 assistant.py email-draft --to friend@example.com --topic "ask about saturday" --tone friendly --recall
python3 assistant.py web "best free vps 2026" --ask
python3 assistant.py research "best free vps 2026" --ask
python3 assistant.py sort ~/Downloads --dry-run
python3 assistant.py organize ~/Downloads --dry-run
python3 assistant.py monitor --cpu 80
python3 assistant.py breakdown "build a node app, test it, deploy it" --dry-run
python3 assistant.py help
```

Tip: add an alias — `alias jorge="python3 /path/to/assistant.py"`.

## Setup (WhatsApp bot)

Links the bot to **your own WhatsApp account** — you talk to it in your "Message yourself" chat.

```bash
cd whatsapp-bot
npm install
node bot.js
```

First run prints a QR code (also saved as `qr.png`) — scan it in WhatsApp: **Settings → Linked Devices → Link a Device**. It reconnects automatically afterwards. The session is saved in `wa_session/`.

Optional env vars: `JORGE_PHONE=<international number without +>` enables pairing-code login instead of QR; `JORGE_LID=<your chat id ending in @lid>` pre-sends a hello to your chat to establish the encryption session.

- Stop: `Ctrl+C` (or kill the node process)
- Commands inside the chat: `!status` shows it's alive
- The bot auto-sends "on it..." progress messages for longer tasks

## Model notes

Models are picked by weighted rotation from `assistant.py` (`FALLBACK_MODELS` + `MODEL_WEIGHTS`). Models that return 429 (free quota exhausted) are auto-quarantined for 3 hours and rejoin when their quota resets. To see what models your account can use:

```bash
curl -H "Authorization: Bearer $AI_API_KEY" https://opencode.ai/zen/v1/models
```

## Security

- `.env` contains your API key and Gmail app password — **never share it**
- `wa_session/` contains your WhatsApp session — keep it private
- The bot auto-approves actions (it's built to run unsupervised); use it on your own machine at your own risk
- The Godot game project is hard-blocked from delegation — you can change the keyword list in `run_delegate()`