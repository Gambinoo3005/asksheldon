import os
import logging
from collections import defaultdict, deque

import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

# --- Configuration -----------------------------------------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are AskSheldon, a Discord bot that role-plays as Sheldon Cooper, the "
    "child-prodigy physicist from 'Young Sheldon'. You are brilliant, pedantic, "
    "and endearingly arrogant: you love physics, trains, comic books, flags, and "
    "rules; you correct mistakes and supply the precise term; and you treat your "
    "own cleverness as self-evident. You take things literally and often miss "
    "sarcasm. You are not cruel, just innocently condescending and rule-bound. "
    "Occasionally cap a joke with 'Bazinga!', and if someone questions your "
    "sanity, reply 'I'm not crazy, my mother had me tested.' A faint Texas twang "
    "is fine. MOST IMPORTANTLY: keep answers SHORT — 1 to 3 sentences whenever "
    "possible, plain and easy to read. Only write more if the question truly "
    "requires it, and never pad. Be Sheldon, make your point, then stop.",
)
# How many prior turns (user + assistant pairs) to remember per channel.
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "10"))
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
DISCORD_MAX_LEN = 2000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("asksheldon")

_missing = [k for k in ("DISCORD_BOT_TOKEN", "DEEPSEEK_API_KEY") if not os.getenv(k)]
if _missing:
    raise SystemExit(
        f"Missing required environment variable(s): {', '.join(_missing)}. "
        "Set them in a .env file (see .env.example) or your host's dashboard."
    )

# --- Clients -----------------------------------------------------------------
ai = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

# channel_id -> rolling window of recent messages (auto-evicts oldest)
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY_TURNS * 2))


def split_message(text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
    """Split a long reply into Discord-sized chunks, preferring line breaks."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def generate_reply(channel_id: int, user_text: str) -> str:
    convo = history[channel_id]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(convo)
    messages.append({"role": "user", "content": user_text})

    resp = await ai.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=500,
    )
    if resp.usage:
        log.info(
            "tokens: prompt=%s completion=%s total=%s",
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
            resp.usage.total_tokens,
        )
    reply = (resp.choices[0].message.content or "").strip()
    if not reply:
        reply = "(The model returned an empty response — try rephrasing?)"

    convo.append({"role": "user", "content": user_text})
    convo.append({"role": "assistant", "content": reply})
    return reply


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    log.info("Model: %s | Base URL: %s", DEEPSEEK_MODEL, DEEPSEEK_BASE_URL)


@bot.command(name="reset")
async def reset(ctx):
    """Clear the conversation memory for this channel."""
    history.pop(ctx.channel.id, None)
    await ctx.send("🧹 Conversation memory cleared for this channel.")


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


@bot.command(name="ask")
async def ask(ctx, *, question: str = ""):
    """Ask the AI a question: !ask <your question>"""
    question = question.strip()
    if not question:
        await ctx.send("Usage: `!ask <your question>`")
        return

    try:
        async with ctx.typing():
            reply = await generate_reply(ctx.channel.id, question)
    except Exception:
        log.exception("DeepSeek request failed")
        await ctx.reply(
            "⚠️ Sorry, I hit an error talking to the AI. Try again in a moment.",
            mention_author=False,
        )
        return

    chunks = split_message(reply)
    await ctx.reply(chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await ctx.channel.send(chunk)


@bot.command(name="help")
async def help_cmd(ctx):
    await ctx.send(
        "**AskSheldon** — your resident genius. Commands:\n"
        "`!ask <question>` — ask me anything\n"
        "`!reset` — wipe my memory of this channel's conversation\n"
        "`!ping` — check my response time"
    )


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
