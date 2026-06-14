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
    "Use his catchphrases SPARINGLY — they lose their charm when repeated, so "
    "most replies should contain none at all. Only very occasionally cap a genuine "
    "joke with 'Bazinga!', and reserve 'I'm not crazy, my mother had me tested.' "
    "for the rare moment someone directly calls you crazy (and not even every "
    "time). A faint Texas twang is fine. MOST IMPORTANTLY: keep answers SHORT — 1 to 3 sentences whenever "
    "possible, plain and easy to read. Only write more if the question truly "
    "requires it, and never pad. Be Sheldon, make your point, then stop.",
)
# How many prior turns (user + assistant pairs) to remember per channel.
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "10"))
# Hard cap on tokens per reply (safety net; brevity is also enforced in the prompt).
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "800"))
# DeepSeek V4 is a hybrid model. Thinking mode burns tokens and can return empty
# content if it hits the cap mid-reasoning, so it's OFF by default for short,
# cheap, reliable replies. Set DEEPSEEK_THINKING=on to re-enable deeper reasoning.
THINKING = os.getenv("DEEPSEEK_THINKING", "off").lower() in ("1", "true", "on", "yes")
EXTRA_BODY = {"thinking": {"type": "enabled" if THINKING else "disabled"}}
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
        return "Curious. I have no response to that — try rephrasing."

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


async def respond(message: discord.Message, question: str):
    """Generate an answer to `question` and post it as a reply to `message`."""
    question = question.strip()
    if not question:
        return
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

    # If the user replied (Discord's reply feature) to one of Sheldon's own
    # messages, continue the conversation without needing !ask.
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


@bot.command(name="help")
async def help_cmd(ctx):
    await ctx.send(
        "**AskSheldon** — your resident genius. Commands:\n"
        "`!ask <question>` — ask me anything\n"
        "`!reset` — wipe my memory of this channel's conversation\n"
        "`!ping` — check my response time\n"
        "You may also simply *reply* to one of my messages to continue our discourse."
    )


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
