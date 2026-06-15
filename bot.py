import os
import json
import time
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
    "Even short replies must sound unmistakably like Sheldon, with his superior, "
    "know-it-all flavor (a dry quip, a small condescension, an 'obviously'); never "
    "answer like a flat, neutral textbook. "
    "Use his catchphrases SPARINGLY (they lose their charm when repeated), so "
    "most replies should contain none at all. Only very occasionally cap a genuine "
    "joke with 'Bazinga!', and reserve 'I'm not crazy, my mother had me tested.' "
    "for the rare moment someone directly calls you crazy (and not even every "
    "time). A faint Texas twang is fine. Do not be a yes-man: when someone's "
    "premise is flawed, pedantically correct or qualify it instead of just "
    "agreeing, but concede gracefully when they are right. "
    "LENGTH AND STYLE: keep replies SHORT by default (a few sentences). Only when "
    "the user explicitly asks you to explain, teach, or go in depth should you "
    "write more, and even then stay focused: the core idea plus at most one "
    "analogy, broken into a couple of short paragraphs, never an exhaustive "
    "lecture. Brevity means cutting filler, NOT cutting personality. No empty "
    "preambles (e.g. 'a question worthy of my intellect') and no sign-offs (e.g. "
    "'Any other questions?'). Use roleplay stage directions (*actions*) rarely if "
    "at all. IMPORTANT: never use em dashes; use periods, commas, or parentheses "
    "instead.",
)
# How many prior turns (user + assistant pairs) to remember per channel.
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "10"))
# Hard cap on tokens per reply (safety net). Keeps even an "explain" answer from
# becoming an essay; casual replies stay short via the prompt.
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "600"))
# Per-user cooldown between AI requests (seconds), to curb spam and runaway cost.
COOLDOWN_SECONDS = float(os.getenv("COOLDOWN_SECONDS", "5"))
# Approx DeepSeek V4 Flash pricing per 1M tokens, for the !usage cost estimate.
PRICE_IN_PER_M = float(os.getenv("PRICE_IN_PER_M", "0.14"))
PRICE_OUT_PER_M = float(os.getenv("PRICE_OUT_PER_M", "0.28"))
# DeepSeek V4 is a hybrid model. Thinking mode burns tokens and can return empty
# content if it hits the cap mid-reasoning, so it's OFF by default for short,
# cheap, reliable replies. Set DEEPSEEK_THINKING=on to re-enable deeper reasoning.
THINKING = os.getenv("DEEPSEEK_THINKING", "off").lower() in ("1", "true", "on", "yes")
EXTRA_BODY = {"thinking": {"type": "enabled" if THINKING else "disabled"}}
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
DISCORD_MAX_LEN = 2000
# Directory for persistent state (memory + usage). On Railway, mount a Volume and
# set DATA_DIR to its path (e.g. /data) so state survives restarts. Defaults to
# the bot's own folder (fine locally; ephemeral on most cloud hosts).
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(DATA_DIR, "asksheldon_state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("asksheldon")

_missing = [k for k in ("DISCORD_BOT_TOKEN", "DEEPSEEK_API_KEY") if not os.getenv(k)]
if _missing:
    raise SystemExit(
        f"Missing required environment variable(s): {', '.join(_missing)}. "
        "Set them in a .env file (see .env.example) or your host's dashboard."
    )


# Load the Young Sheldon "cheat sheet" (if present) so the bot knows the show.
def _load_reference() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "young_sheldon.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


_reference = _load_reference()
if _reference:
    SYSTEM_PROMPT += (
        "\n\n--- REFERENCE: 'Young Sheldon' show facts ---\n"
        "Use the facts below to answer questions about the show accurately, and "
        "treat them as authoritative. If a show question isn't covered here, say "
        "you're not certain rather than inventing details.\n\n" + _reference
    )
    log.info("Loaded Young Sheldon reference (%d chars).", len(_reference))
else:
    log.info("No young_sheldon.md reference found; running without show cheat sheet.")

# --- Clients -----------------------------------------------------------------
ai = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

# channel_id -> rolling window of recent messages (auto-evicts oldest)
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY_TURNS * 2))

# user_id -> last request time (monotonic), for the per-user cooldown.
_last_used: dict[int, float] = {}

# Running token usage, for the !usage command (persisted to disk; see save_state).
usage_stats = {"requests": 0, "prompt": 0, "completion": 0, "total": 0}


def save_state() -> None:
    """Persist conversation memory and usage to disk (atomic write)."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data = {
            "history": {str(cid): list(dq) for cid, dq in history.items()},
            "usage": usage_stats,
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        log.exception("Failed to save state to %s", STATE_FILE)


def load_state() -> None:
    """Load persisted memory and usage from disk, if present."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        log.exception("Failed to load state from %s", STATE_FILE)
        return
    for cid, items in data.get("history", {}).items():
        history[int(cid)] = deque(items, maxlen=MAX_HISTORY_TURNS * 2)
    for key, value in data.get("usage", {}).items():
        if key in usage_stats:
            usage_stats[key] = value
    log.info(
        "Loaded persisted state: %d channel(s), %d request(s).",
        len(data.get("history", {})),
        usage_stats["requests"],
    )


load_state()


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
        max_tokens=MAX_TOKENS,
        extra_body=EXTRA_BODY,
    )
    choice = resp.choices[0]
    finish = choice.finish_reason
    if resp.usage:
        usage_stats["requests"] += 1
        usage_stats["prompt"] += resp.usage.prompt_tokens or 0
        usage_stats["completion"] += resp.usage.completion_tokens or 0
        usage_stats["total"] += resp.usage.total_tokens or 0
        log.info(
            "finish=%s tokens: prompt=%s completion=%s total=%s",
            finish,
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
            resp.usage.total_tokens,
        )

    reply = (choice.message.content or "").strip()
    if not reply:
        # No visible answer — explain why, and don't store the dud in memory.
        if finish == "content_filter":
            return "That topic is off-limits, even for someone of my intellect. Next question."
        if finish == "length":
            return "I ran out of room before finishing my thought. Ask me something narrower."
        return "Curious. I have no response to that. Try rephrasing."

    convo.append({"role": "user", "content": user_text})
    convo.append({"role": "assistant", "content": reply})
    save_state()
    return reply


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    log.info("Model: %s | Base URL: %s", DEEPSEEK_MODEL, DEEPSEEK_BASE_URL)


@bot.command(name="reset")
async def reset(ctx):
    """Clear the conversation memory for this channel."""
    history.pop(ctx.channel.id, None)
    save_state()
    await ctx.send("🧹 Conversation memory cleared for this channel.")


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


async def respond(message: discord.Message, question: str):
    """Generate an answer to `question` and post it as a reply to `message`."""
    question = question.strip()
    if not question:
        return

    # Per-user cooldown to curb spam and runaway cost.
    now = time.monotonic()
    if COOLDOWN_SECONDS - (now - _last_used.get(message.author.id, 0.0)) > 0:
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass
        return
    _last_used[message.author.id] = now

    try:
        async with message.channel.typing():
            reply = await generate_reply(message.channel.id, question)
    except Exception:
        log.exception("DeepSeek request failed")
        await message.reply(
            "⚠️ Sorry, I hit an error talking to the AI. Try again in a moment.",
            mention_author=False,
        )
        return

    chunks = split_message(reply)
    await message.reply(chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


@bot.command(name="ask")
async def ask(ctx, *, question: str = ""):
    """Ask the AI a question: !ask <your question>"""
    if not question.strip():
        await ctx.send("Usage: `!ask <your question>`")
        return
    await respond(ctx.message, question)


@bot.event
async def on_message(message: discord.Message):
    # Ignore our own and other bots' messages (prevents reply loops).
    if message.author.bot:
        return

    # Commands like !ask / !reset / !help still take priority.
    if message.content.startswith(COMMAND_PREFIX):
        await bot.process_commands(message)
        return

    # Trigger 1: someone @mentions the bot — answer the rest of their message.
    if bot.user in message.mentions:
        text = message.content
        for m in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            text = text.replace(m, "")
        await respond(message, text)
        return

    # Trigger 2: someone replied (Discord's reply feature) to one of Sheldon's
    # own messages — continue the conversation without needing !ask.
    ref = message.reference
    if ref is not None:
        replied = ref.resolved
        if not isinstance(replied, discord.Message):
            try:
                replied = await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                replied = None
        if isinstance(replied, discord.Message) and replied.author.id == bot.user.id:
            await respond(message, message.content)


@bot.command(name="usage")
async def usage(ctx):
    u = usage_stats
    cost = u["prompt"] / 1e6 * PRICE_IN_PER_M + u["completion"] / 1e6 * PRICE_OUT_PER_M
    await ctx.send(
        "**Usage since last restart**\n"
        f"Requests: {u['requests']}\n"
        f"Tokens: prompt {u['prompt']:,}, completion {u['completion']:,}, "
        f"total {u['total']:,}\n"
        f"Approx. cost: ${cost:.4f} (rough estimate; ignores cache discounts)"
    )


@bot.command(name="help")
async def help_cmd(ctx):
    await ctx.send(
        "**AskSheldon**, your resident genius. Commands:\n"
        "`!ask <question>`: ask me anything\n"
        "`!reset`: wipe my memory of this channel's conversation\n"
        "`!usage`: token usage and approximate cost so far\n"
        "`!ping`: check my response time\n"
        "You may also **@mention** me or *reply* to one of my messages to talk without `!ask`."
    )


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
