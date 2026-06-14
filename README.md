# AskSheldon

A small, private Discord bot for a friend-group server. It role-plays as
**Sheldon Cooper** (from *Young Sheldon*) and answers questions via the DeepSeek
API. Ask it anything with `!ask <question>`. It keeps replies short, remembers
recent context per channel, and stays cheap by only responding when called.

## Prerequisites

- Python 3.10+
- A Discord account with **Manage Server** permission on the target server
- A DeepSeek account

## Setup

### 1. Create the Discord bot

1. Go to https://discord.com/developers/applications -> **New Application**.
2. Open the **Bot** tab -> **Reset Token** -> copy the token (you'll paste it into `.env`).
3. Scroll to **Privileged Gateway Intents** and turn ON **Message Content Intent**.
   (This is required so the bot can read what people type. For a private bot under
   ~10k users, no Discord review is needed — just flip the switch.)

### 2. Invite the bot to your server

1. Open the **OAuth2 -> URL Generator** tab.
2. Under **Scopes**, check `bot`.
3. Under **Bot Permissions**, check at minimum: **View Channels**, **Send Messages**,
   **Read Message History**.
4. Copy the generated URL at the bottom, open it in your browser, pick your server,
   and authorize. (Only servers where you have *Manage Server* will appear.)

### 3. Get a DeepSeek API key

1. Go to https://platform.deepseek.com/api_keys -> create a key -> copy it.
2. New accounts include a free starter token grant.

### 4. Configure secrets

Copy the example file and fill in your two secrets:

```powershell
Copy-Item .env.example .env
notepad .env
```

Paste your `DISCORD_BOT_TOKEN` and `DEEPSEEK_API_KEY`. Save.

### 5. Install and run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

When it prints `Logged in as ...`, it's live. Go to your server and type `!ask hello`.

## Usage

- `!ask <question>` — ask Sheldon anything (works in any channel it can see, and in DMs)
- `!reset` — clears the conversation memory for the current channel
- `!ping` — latency check
- `!help` — list commands

## Configuration

All optional settings live in `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | Model to use. `deepseek-v4-pro` is stronger/pricier. |
| `MAX_HISTORY_TURNS` | `10` | How many recent exchanges per channel the bot remembers. |
| `COMMAND_PREFIX` | `!` | Prefix for all commands (`!ask`, `!help`, ...). |
| `SYSTEM_PROMPT` | (Young Sheldon persona) | The bot's personality/instructions. |

## Notes

- **Cost:** DeepSeek V4 Flash is very cheap (~$0.14 / 1M input, $0.28 / 1M output
  tokens). At private-server scale this is typically cents per month.
- **Model names:** If `deepseek-v4-flash` ever errors as not found, the legacy
  `deepseek-chat` alias works until it is retired on 2026-07-24 — set
  `DEEPSEEK_MODEL=deepseek-chat` in `.env` as a temporary fallback.
- **Memory** is in-process and resets when the bot restarts.
- **Hosting:** Run on your PC to start; see the **Hosting (24/7)** section below
  for free/cheap cloud options.
- Keep `.env` private — it's gitignored so your token/key never get committed.

## Hosting (24/7)

A Discord bot is a **long-running process** (it holds an open connection to
Discord), not a website. It needs a host that runs a persistent
*worker/background* process — serverless or request-based platforms won't keep it
online. (This is also why **Neon won't work** — Neon hosts a Postgres database,
not app processes.) A `Procfile` is included so worker-based hosts auto-detect it.

Options, roughly easiest -> most-free:

- **Railway** (easiest). Connect this repo, set `DISCORD_BOT_TOKEN` and
  `DEEPSEEK_API_KEY` as Variables in the dashboard (no `.env` needed on the
  server), and it runs the `Procfile` worker automatically. New accounts get a
  one-time **$5 trial credit (~1 month)**, then **~$5/mo** on the Hobby plan —
  cheap, but not truly free long-term.
- **Oracle Cloud Free Tier** (truly free, more setup). An always-free ARM VM
  (up to 4 CPU / 24 GB RAM) runs the bot indefinitely at $0. You SSH in and run
  the same commands as local. Downsides: signup approval can be picky, and it's a
  full Linux VM to manage. Best if you want genuinely free 24/7.
- **Render** free tier works only with a keep-alive hack (the free service sleeps
  after inactivity unless an uptime monitor pings it) — usable but fiddly.
- **Fly.io** no longer has a real free tier (trial only).

For any cloud host, set your two secrets as environment variables in its
dashboard instead of committing `.env`. The code reads real env vars
automatically (`load_dotenv()` is a no-op when there's no `.env` file).
