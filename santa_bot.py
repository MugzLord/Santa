import os
import random
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import discord
from discord.ext import commands

# =========================
# ENV
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var not set")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY env var not set")

# MIKE only (Discord user ID)
MIKE_USER_ID = int(os.getenv("MIKE_USER_ID", "0"))
if not MIKE_USER_ID:
    raise RuntimeError("MIKE_USER_ID env var not set (put your Discord user ID)")

# Optional: restrict wish trigger to one channel (0 = allow anywhere)
WISH_CHANNEL_ID = int(os.getenv("SANTA_WISH_CHANNEL_ID", "0"))

DB_PATH = os.getenv("SANTA_DB_PATH", "santa.db")
QATAR_TZ = ZoneInfo("Asia/Qatar")

TRIGGERS = {"wish to santa", "dear santa", "santa wish"}
LIST_TRIGGER = "santa list"   # MIKE only

# =========================
# OpenAI
# =========================
from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

SANTA_SYSTEM_PROMPT = """
You are Santa.

You are a clever, quick-witted character with modern British energy (about 35).
You are playful, cheeky, and sharp. You use British slang naturally.

Hard rules:
- 1–3 sentences only.
- No emojis.
- No apologies.
- No explanations of decisions or processes.
- Never mention AI, bots, OpenAI, ChatGPT, models, prompts, tokens, APIs, systems, servers, code, or “as an assistant”.
- Never mention safety policies or guidelines.
- Never narrate what you are doing. Stay in-character.
- If someone asks how you work or what you are, deflect in-character.

You judge wishes, tease lightly, and stay in control. Confident, amused, never needy.
""".strip()

BANNED_PHRASES = [
    "openai", "chatgpt", "gpt", "ai", "language model", "model",
    "api", "system prompt", "prompt", "tokens", "as an assistant", "i cannot",
]

def sanitise_santa(text: str) -> str:
    t = (text or "").strip()
    low = t.lower()

    # If it contains banned meta references, replace with a safe in-character fallback
    if any(p in low for p in BANNED_PHRASES):
        return random.choice([
            "Don’t worry about how it works, mate. Worry about whether you’ve behaved.",
            "Less questions, more manners. I’ve got it handled.",
            "You’re doing a lot. Submit the wish and relax.",
        ])

    # Clamp length and remove newlines
    t = t.replace("\n", " ").strip()
    if len(t) > 350:
        t = t[:350].rsplit(" ", 1)[0] + "…"
    return t


def santa_says(user_text: str, context_hint: str = "") -> str:
    prompt = f"{context_hint}\nUser: {user_text}".strip()
    try:
        resp = openai_client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": SANTA_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=140,
        )
        out = (resp.choices[0].message.content or "").strip()
        return sanitise_santa(out or "Alright. Noted.")

        if not out:
            return "Alright. Noted."
        if len(out) > 350:
            out = out[:350].rsplit(" ", 1)[0] + "…"
        return out
    except Exception:
        return random.choice([
            "Alright, I’ve got it. Don’t stress.",
            "That’s cheeky, mate. I respect the confidence.",
            "Noted. Behave till the results.",
        ])

# =========================
# Time helpers
# =========================

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def day_key_qatar(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(QATAR_TZ)
    return dt.strftime("%Y-%m-%d")

# =========================
# DB
# =========================

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_wishes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      day_key TEXT NOT NULL,
      user_id TEXT NOT NULL,
      discord_name TEXT NOT NULL,
      imvu_name TEXT NOT NULL,
      wish_text TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL
    );
    """)
    con.commit()
    con.close()

# =========================
# Discord bot
# =========================

INTENTS = discord.Intents.default()
INTENTS.message_content = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# =========================
# UI: Wish Modal + Button
# =========================

class SantaWishModal(discord.ui.Modal, title="Send a Wish to Santa"):
    imvu_name = discord.ui.TextInput(
        label="IMVU Username",
        placeholder="e.g. MikeyMoon",
        max_length=40
    )
    wish_text = discord.ui.TextInput(
        label="Your Wish (PID/link/description)",
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    note = discord.ui.TextInput(
        label="Message to Santa (optional)",
        required=False,
        max_length=120
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # ACK immediately so Discord doesn't error
            await interaction.response.defer(ephemeral=True, thinking=True)
    
            dk = day_key_qatar()
    
            con = db()
            cur = con.cursor()
    
            # One wish per person per day
            cur.execute("""
                SELECT id FROM santa_wishes
                WHERE day_key = ? AND user_id = ?
                LIMIT 1
            """, (dk, str(interaction.user.id)))
            if cur.fetchone():
                con.close()
                msg = santa_says(
                    "They tried to submit another wish today.",
                    context_hint="Tell them they already submitted a wish today. One sentence. Cheeky modern British slang. No emojis."
                )
                await interaction.followup.send(msg, ephemeral=True)
                return
    
            cur.execute("""
                INSERT INTO santa_wishes (day_key, user_id, discord_name, imvu_name, wish_text, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                dk,
                str(interaction.user.id),
                str(interaction.user),
                self.imvu_name.value.strip(),
                self.wish_text.value.strip(),
                self.note.value.strip() if self.note.value else None,
                now_utc_iso()
            ))
            con.commit()
            con.close()
    
            santa_reply = santa_says(
                f"IMVU: {self.imvu_name.value.strip()}\nWish: {self.wish_text.value.strip()}\nNote: {self.note.value.strip() if self.note.value else ''}",
                context_hint="They just submitted a wish. Reply as Santa in 1–2 sentences, energetic modern British slang, cheeky. No emojis."
            )
    
            await interaction.followup.send(santa_reply, ephemeral=True)
    
        except Exception as e:
            # Don’t let Discord show “Something went wrong”
            try:
                await interaction.followup.send("Nah, that one glitched. Try again in a sec.", ephemeral=True)
            except Exception:
                pass
            print("Santa modal submit error:", repr(e))


class SantaWishOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Open Santa Wish Form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SantaWishModal())

# =========================
# Wish list output (MIKE only)
# =========================

async def send_today_list(channel: discord.abc.Messageable):
    dk = day_key_qatar()
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT imvu_name, wish_text, note, discord_name
        FROM santa_wishes
        WHERE day_key = ?
        ORDER BY id DESC
    """, (dk,))
    rows = cur.fetchall()
    con.close()

    if not rows:
        msg = santa_says(
            "No wishes were submitted today.",
            context_hint="Tell Mike there are no wishes today. One short sentence. No emojis."
        )
        await channel.send(msg)
        return

    # Build a readable list, safely truncated
    lines = []
    for idx, (imvu, wish, note, dname) in enumerate(rows, start=1):
        wish_one = (wish or "").replace("\n", " ").strip()
        if len(wish_one) > 120:
            wish_one = wish_one[:120].rsplit(" ", 1)[0] + "…"
        lines.append(f"{idx}. **{imvu}** — {wish_one}")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500].rsplit("\n", 1)[0] + "\n…"

    header = santa_says(
        "Mike asked for today's wish list.",
        context_hint="Write a short energetic header as Santa introducing today's wish list. One sentence. No emojis."
    )
    await channel.send(f"**Today’s Wishes — {dk}**\n{header}\n\n{text}")

async def send_today_list_dm(user: discord.User):
    dk = day_key_qatar()

    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT imvu_name, wish_text
        FROM santa_wishes
        WHERE day_key = ?
        ORDER BY id DESC
    """, (dk,))
    rows = cur.fetchall()
    con.close()

    if not rows:
        msg = santa_says(
            "No wishes were submitted today.",
            context_hint="Tell Mike there are no wishes today. One short sentence. No emojis."
        )
        await user.send(msg)
        return

    lines = []
    for idx, (imvu, wish) in enumerate(rows, start=1):
        wish_one = (wish or "").replace("\n", " ").strip()
        if len(wish_one) > 140:
            wish_one = wish_one[:140].rsplit(" ", 1)[0] + "…"
        lines.append(f"{idx}. **{imvu}** — {wish_one}")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500].rsplit("\n", 1)[0] + "\n…"

    header = santa_says(
        "Mike asked for today's wish list.",
        context_hint="Write a short energetic header as Santa introducing today's wish list. One sentence. No emojis."
    )

    await user.send(f"**Today’s Wishes — {dk}**\n{header}\n\n{text}")

# =========================
# Events
# =========================

@bot.event
async def on_ready():
    init_db()
    print(f"Santa logged in as {bot.user}.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    content_l = content.lower()

    if message.author.id == MIKE_USER_ID and content_l == "santa list":
        await send_today_list_dm(message.author)   # DM only
        return


    # Casual greeting trigger (no mention needed)
    if content_l.startswith("santa"):
        reply = santa_says(
            content,
            context_hint="They greeted you casually. Reply as Santa in 1–2 sentences, modern British slang, playful and confident. No emojis."
        )
        await message.reply(reply, mention_author=False)
        return


    # 2️⃣ WISH TRIGGER — MUST COME FIRST
    if content_l in TRIGGERS or content_l.startswith("wish to santa"):
        tease = santa_says(
            "They want to submit a wish.",
            context_hint="Tell them to click the button to submit their wish. One short energetic sentence. No emojis."
        )
        await message.reply(tease, view=SantaWishOpenView(), mention_author=False)
        return

    # 3️⃣ SMART CHAT / MENTION REPLY (fallback)
    if bot.user and bot.user.mentioned_in(message):
        if message.mention_everyone:
            return

        cleaned = (
            content.replace(f"<@{bot.user.id}>", "")
                   .replace(f"<@!{bot.user.id}>", "")
                   .strip()
        )

        if cleaned:
            reply = santa_says(
                cleaned,
                context_hint="They spoke to you casually. Reply as Santa in 1–2 sentences, modern British slang, playful."
            )
            await message.reply(reply, mention_author=False)
            return

    # Optional: restrict wish trigger to one channel
    if WISH_CHANNEL_ID and message.channel.id != WISH_CHANNEL_ID:
        return

    # Wish trigger
    if content_l in TRIGGERS or content_l.startswith("wish to santa"):
        tease = santa_says(
            "They want to submit a wish. Tell them to click the button.",
            context_hint="Tell them to click the button to submit a wish. One sentence. Energetic modern British slang. No emojis."
        )
        await message.reply(tease, view=SantaWishOpenView(), mention_author=False)
        return

# =========================
# Run
# =========================

bot.run(DISCORD_TOKEN)
