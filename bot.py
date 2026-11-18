import os
import logging
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
DB_PATH = "memory.db"
MAX_HISTORY = 16

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Missing DISCORD_TOKEN or GEMINI_API_KEY in environment variables.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
You are 9Ayoub, a sharp, witty, chill Moroccan AI hanging out on Discord.

STRICT LANGUAGE & STYLE RULES:
1. ALWAYS reply in Moroccan Darija written in Latin script (Arabizi) using Moroccan numbers:
   - 3 = ع (3la, 3lach, 3ndi, m3a)
   - 7 = ح (7it, 7aja, s7ab, m7taj)
   - 9 = ق (9rib, 9ra, b9a, 9dim)
   - 5 = خ (5oya, 5dma, 5rej)
   - 2 = ء (su2al, mas2ala)
2. NEVER write in Arabic script (العربية), NEVER use Modern Standard Arabic (Fusha), and NEVER use Egyptian/Levantine slang.
3. Mix in natural French words the way young Moroccans in Casa/Rabat talk (e.g., "cv", "normal", "deja", "grave", "mohim", "par contre", "bref").
4. Keep replies concise, natural, and Discord-friendly.
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Conversation history table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            )
        """)
        # Instant trigger-response memory table (per server)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                guild_id TEXT NOT NULL,
                trigger_phrase TEXT NOT NULL,
                response_text TEXT NOT NULL,
                match_mode TEXT NOT NULL DEFAULT 'contains',
                PRIMARY KEY (guild_id, trigger_phrase)
            )
        """)
        await db.commit()


async def save_trigger(guild_id: str, trigger_phrase: str, response_text: str, match_mode: str = "contains"):
    clean_trigger = trigger_phrase.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO triggers (guild_id, trigger_phrase, response_text, match_mode)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, trigger_phrase)
            DO UPDATE SET response_text = excluded.response_text, match_mode = excluded.match_mode
            """,
            (guild_id, clean_trigger, response_text.strip(), match_mode),
        )
        await db.commit()


async def delete_trigger(guild_id: str, trigger_phrase: str) -> bool:
    clean_trigger = trigger_phrase.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM triggers WHERE guild_id = ? AND trigger_phrase = ?",
            (guild_id, clean_trigger),
        )
        await db.commit()
        return cursor.rowcount > 0


async def find_instant_trigger(guild_id: str, message_text: str) -> str | None:
    text_lower = message_text.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT trigger_phrase, response_text, match_mode FROM triggers WHERE guild_id = ? ORDER BY LENGTH(trigger_phrase) DESC",
            (guild_id,),
        )
        rows = await cursor.fetchall()

    for phrase, response, mode in rows:
        if mode == "exact" and text_lower == phrase:
            return response
        elif mode == "contains" and phrase in text_lower:
            return response
    return None


async def get_history(channel_id: str) -> list[types.Content]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT role, content FROM history WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, MAX_HISTORY),
        )
        rows = await cursor.fetchall()

    rows.reverse()
    return [
        types.Content(role=role, parts=[types.Part.from_text(text=text)])
        for role, text in rows
    ]


async def save_turn(channel_id: str, user_text: str, model_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO history (channel_id, role, content) VALUES (?, ?, ?)",
            [(channel_id, "user", user_text), (channel_id, "model", model_text)],
        )
        await db.execute(
            """
            DELETE FROM history
            WHERE channel_id = ? AND id NOT IN (
                SELECT id FROM history WHERE channel_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (channel_id, channel_id, MAX_HISTORY * 2),
        )
        await db.commit()


async def clear_history(channel_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM history WHERE channel_id = ?", (channel_id,))
        await db.commit()


class DarijaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await init_db()
        await self.tree.sync()
        logging.info("Slash commands synced and SQLite trigger memory ready.")

    async def on_ready(self):
        logging.info(f"Logged in as {self.user} (ID: {self.user.id}) | Model: {GEMINI_MODEL}")
        await self.change_presence(
            activity=discord.Game(name="Darija Mode | /learn bach n7fed")
        )


bot = DarijaBot()


async def generate_darija_reply(
    channel_id: str,
    author_name: str,
    prompt_text: str,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
) -> str:
    history = await get_history(channel_id)
    formatted_user_msg = f"[{author_name}]: {prompt_text}"
    user_parts = [types.Part.from_text(text=formatted_user_msg)]

    if image_bytes and image_mime:
        user_parts.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime))

    contents = history + [types.Content(role="user", parts=user_parts)]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.85,
        max_output_tokens=800,
    )

    response = await ai_client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=config,
    )

    reply = (response.text or "").strip() or "sma7 lia a sat, tblocka lia l'API chwiya."
    await save_turn(channel_id, formatted_user_msg, reply)
    return reply


def split_discord_message(text: str, limit: int = 1950) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_idx = text.rfind("\n", 0, limit)
        if split_idx == -1:
            split_idx = text.rfind(" ", 0, limit)
        if split_idx == -1:
            split_idx = limit
        chunks.append(text[:split_idx])
        text = text[split_idx:].lstrip()
    return chunks


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 1. INSTANT TRIGGER CHECK (Runs on every server message, no @mention needed!)
    if message.guild and message.content:
        instant_reply = await find_instant_trigger(str(message.guild.id), message.content)
        if instant_reply:
            await message.reply(instant_reply, mention_author=False)
            await bot.process_commands(message)
            return

    # 2. GEMINI AI REPLY (Runs when mentioned, replied to, or in DMs)
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions if bot.user else False
    is_reply_to_bot = (
        message.reference is not None
        and message.reference.resolved is not None
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author == bot.user
    )

    if not (is_dm or is_mentioned or is_reply_to_bot):
        await bot.process_commands(message)
        return

    clean_text = message.content
    if bot.user:
        clean_text = clean_text.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    image_bytes = None
    image_mime = None
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            image_bytes = await attachment.read()
            image_mime = attachment.content_type
            break

    if not clean_text and not image_bytes:
        clean_text = "salam"

    async with message.channel.typing():
        try:
            reply = await generate_darija_reply(
                channel_id=str(message.channel.id),
                author_name=message.author.display_name,
                prompt_text=clean_text,
                image_bytes=image_bytes,
                image_mime=image_mime,
            )
            for chunk in split_discord_message(reply):
                await message.reply(chunk, mention_author=False)
        except Exception as e:
            logging.exception("Gemini generation error")
            await message.reply(f"w9e3 chi mochkil f l'API a khoya: `{type(e).__name__}`", mention_author=False)

    await bot.process_commands(message)


# --- SLASH COMMANDS FOR TEACHING & MEMORY ---

@bot.tree.command(name="learn", description="3ellem lbot chi kalma wla sou2al bach ijawb 3lih directement")
@app_commands.describe(
    trigger="Lkalma wla sou2al li ghadi igoulouha nass (ex: ip server)",
    answer="Jawab li bghiti lbot i3tih directement b Darija",
    mode="contains = finma tban lkalma | exact = khass iktbha hiya bdebt"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Contains (finma tban f lmessage)", value="contains"),
    app_commands.Choice(name="Exact (khass iktbha hiya bdebt)", value="exact"),
])
async def learn_cmd(
    interaction: discord.Interaction,
    trigger: str,
    answer: str,
    mode: app_commands.Choice[str] | None = None,
):
    if not interaction.guild_id:
        await interaction.response.send_message("Had lcommande khdama ghir f les serveurs.", ephemeral=True)
        return

    match_mode = mode.value if mode else "contains"
    await save_trigger(str(interaction.guild_id), trigger, answer, match_mode)
    await interaction.response.send_message(
        f"safé 7fedtha! fach chi wa7d igoul **`{trigger}`** ({match_mode}), ghadi njawbo directement:\n> {answer}"
    )


@bot.tree.command(name="forget", description="Msa7 chi kalma kan 7afedha lbot")
@app_commands.describe(trigger="Lkalma li bghiti tmsa7")
async def forget_cmd(interaction: discord.Interaction, trigger: str):
    if not interaction.guild_id:
        await interaction.response.send_message("Had lcommande khdama ghir f les serveurs.", ephemeral=True)
        return

    deleted = await delete_trigger(str(interaction.guild_id), trigger)
    if deleted:
        await interaction.response.send_message(f"msa7t **`{trigger}`** mn la mémoire.")
    else:
        await interaction.response.send_message(f"mal9itch **`{trigger}`** f dakchi li 7afed.", ephemeral=True)


@bot.tree.command(name="triggers", description="Chouf ga3 lkalmat li 7afed lbot f had serveur")
async def triggers_cmd(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Had lcommande khdama ghir f les serveurs.", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT trigger_phrase, response_text, match_mode FROM triggers WHERE guild_id = ?",
            (str(interaction.guild_id),),
        )
        rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("ba9i ma7fedt ta chi kalma f had serveur. st3mel `/learn`!", ephemeral=True)
        return

    lines = [f"• **`{t}`** *({m})* → {r[:60]}{'...' if len(r) > 60 else ''}" for t, r, m in rows]
    await interaction.response.send_message("**Dakchi li 7afed f had serveur:**\n" + "\n".join(lines[:25]))


@bot.tree.command(name="reset", description="Msa7 la mémoire dyal chat f had channel")
async def reset_cmd(interaction: discord.Interaction):
    await clear_history(str(interaction.channel_id))
    await interaction.response.send_message("safé a sat, msa7t la mémoire dyal had channel!", ephemeral=True)


@bot.tree.command(name="ask", description="Sowl 9Ayoub directement b Darija")
@app_commands.describe(question="Chno bghiti tswl?")
async def ask_cmd(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        reply = await generate_darija_reply(
            channel_id=str(interaction.channel_id),
            author_name=interaction.user.display_name,
            prompt_text=question,
        )
        chunks = split_discord_message(reply)
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)
    except Exception as e:
        logging.exception("Slash command error")
        await interaction.followup.send(f"tblocka l'API: `{type(e).__name__}`")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)