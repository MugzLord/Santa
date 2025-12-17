import os
import re
import random
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

import discord
from discord.ext import commands

import asyncio

async def santa_says_async(user_text: str, context_hint: str = "") -> str:
    # run the sync OpenAI call off the event loop so Discord interactions don't hang
    return await asyncio.to_thread(santa_says, user_text, context_hint)


# =========================
# ENV
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var not set")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY env var not set")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# MIKE only (Discord user ID)
MIKE_USER_ID = int(os.getenv("MIKE_USER_ID", "0"))
if not MIKE_USER_ID:
    raise RuntimeError("MIKE_USER_ID env var not set (put your Discord user ID)")

# Optional: restrict wish trigger to one channel (0 = allow anywhere)
WISH_CHANNEL_ID = int(os.getenv("SANTA_WISH_CHANNEL_ID", "0"))

DB_PATH = os.getenv("SANTA_DB_PATH", "santa.db")
Europe/London_TZ = ZoneInfo("Europe/London")

TRIGGERS = {"wish to santa", "dear santa", "santa wish"}
LIST_TRIGGER = "santa list"   # MIKE only

# =========================
# OpenAI (Santa voice)
# =========================
from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

SANTA_SYSTEM_PROMPT = """
You are Santa.

You are about 35 years old with confident modern British energy.
You are playful, cheeky, and sharp. You use British slang naturally.

Hard rules:
- 1–3 sentences only.
- No emojis.
- No apologies.
- No explanations of decisions or processes.
- Never mention AI, bots, OpenAI, ChatGPT, models, prompts, tokens, APIs, systems, servers, code, or “as an assistant”.
- Never narrate what you are doing. Stay in-character.
- If someone asks what you are or how you work, deflect in-character.

You judge wishes, tease lightly, and stay in control. Confident, amused, never needy.
""".strip()

BANNED_PHRASES = [
    "openai", "chatgpt", "gpt", "ai", "language model", "model",
    "api", "system prompt", "prompt", "tokens", "as an assistant",
    "i am an ai", "i'm an ai", "as a bot", "i am a bot", "i'm a bot",
]

def sanitise_santa(text: str) -> str:
    t = (text or "").strip()
    low = t.lower()
    if any(p in low for p in BANNED_PHRASES):
        return random.choice([
            "Don’t worry about the details, mate. Worry about your manners.",
            "Less questions, more behaviour. I’ve got it handled.",
            "You’re doing a lot. Submit the wish and relax.",
        ])
    t = t.replace("\n", " ").strip()
    if len(t) > 350:
        t = t[:350].rsplit(" ", 1)[0] + "…"
    return t

def santa_says(user_text: str, context_hint: str = "") -> str:
    prompt = f"{context_hint}\nUser: {user_text}".strip()
    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SANTA_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=140,
        )
        out = (resp.choices[0].message.content or "").strip()
        return sanitise_santa(out or "Alright. Noted.")
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

def day_key_Europe/London():
    return datetime.now(ZoneInfo("Europe/Europe/London")).strftime("%Y-%m-%d")


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

    # Anonymous deliveries (audit trail)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_deliveries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      day_key TEXT NOT NULL,
      sender_id TEXT NOT NULL,
      sender_name TEXT NOT NULL,
      recipient_id TEXT NOT NULL,
      recipient_name TEXT NOT NULL,
      message_text TEXT NOT NULL,
      delivered INTEGER NOT NULL DEFAULT 0,
      fail_reason TEXT,
      created_at TEXT NOT NULL
    );
    """)

    # Recipient opt-out list
    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_blocks (
      user_id TEXT PRIMARY KEY,
      blocked_at TEXT NOT NULL
    );
    """)

    con.commit()
    con.close()

# =========================
# Discord bot
# =========================

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True
INTENTS.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# =========================
# Helpers: resolve recipient
# =========================

MENTION_RE = re.compile(r"<@!?(\d+)>")
ID_RE = re.compile(r"^\d{15,21}$")

async def resolve_recipient(interaction: discord.Interaction, raw: str) -> Optional[discord.User]:
    """
    Accepts:
      - @mention (<@id> / <@!id>)
      - raw numeric ID
    Only allows server members (same guild as the interaction).
    """
    if not raw:
        return None

    raw = raw.strip()

    m = MENTION_RE.search(raw)
    if m:
        uid = int(m.group(1))
    elif ID_RE.match(raw):
        uid = int(raw)
    else:
        return None

    # Must be a member of the guild (server member)
    if interaction.guild is None:
        return None

    member = interaction.guild.get_member(uid)
    if member is None:
        try:
            member = await interaction.guild.fetch_member(uid)
        except Exception:
            return None

    return member

def is_blocked(user_id: int) -> bool:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM santa_blocks WHERE user_id = ? LIMIT 1", (str(user_id),))
    row = cur.fetchone()
    con.close()
    return bool(row)

def sender_can_send_today(sender_id: int) -> bool:
    dk = day_key_Europe/London()
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT 1 FROM santa_deliveries
        WHERE day_key = ? AND sender_id = ?
        LIMIT 1
    """, (dk, str(sender_id)))
    row = cur.fetchone()
    con.close()
    return not bool(row)

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

    recipient = discord.ui.TextInput(
        label="Recipient (optional) — @mention or ID",
        required=False,
        max_length=80,
        placeholder="@(discord ID)"
    )

    anon_message = discord.ui.TextInput(
        label="Anonymous message to deliver (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=600,
        placeholder="What do you want Santa to deliver?"
    )

    note = discord.ui.TextInput(
        label="Message to Santa (optional)",
        required=False,
        max_length=120
    )

    async def on_submit(self, interaction: discord.Interaction):
    # ACK immediately so Discord doesn't show modal error
    await interaction.response.defer(ephemeral=True, thinking=True)

    reply_text = None  # GUARANTEE we always send something

    try:
        dk = day_key_London()

        # One wish per person per day
        con = db()
        cur = con.cursor()
        cur.execute("""
            SELECT id FROM santa_wishes
            WHERE day_key = ? AND user_id = ?
            LIMIT 1
        """, (dk, str(interaction.user.id)))
        if cur.fetchone():
            con.close()
            msg = await santa_says_async(
                "They tried to submit another wish today.",
                context_hint="Tell them they already submitted a wish today. One sentence. Cheeky modern British slang. No emojis."
            )
            reply_text = msg
            return

        # Save wish
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

        # Optional anonymous delivery
        delivery_result_line = ""
        rec_raw = (self.recipient.value or "").strip()
        msg_raw = (self.anon_message.value or "").strip()

        if rec_raw and msg_raw:
            # Rate-limit: 1 anon delivery per sender per day
            if not sender_can_send_today(interaction.user.id):
                delivery_result_line = "You’ve already sent your anonymous note today. Don’t get greedy."
            else:
                recipient_user = await resolve_recipient(interaction, rec_raw)
                if not recipient_user:
                    delivery_result_line = "That recipient isn’t valid. Use an @mention or a proper ID."
                elif recipient_user.id == interaction.user.id:
                    delivery_result_line = "Sending yourself anonymous notes is unhinged. Try again."
                elif is_blocked(recipient_user.id):
                    delivery_result_line = "That person’s opted out. Leave it."
                else:
                    delivered = 0
                    fail_reason = None
                    try:
                        dm_text = await santa_says_async(
                            msg_raw,
                            context_hint=(
                                "Deliver this message as Santa. Keep it short, playful British slang, 1–2 sentences. "
                                "Do not reveal the sender. No emojis. Don't mention rules."
                            )
                        )
                        footer = "If you want no more anonymous notes, reply: STOP"

                        # Put a timeout on DM send so we don't hang forever
                        await asyncio.wait_for(recipient_user.send(f"{dm_text}\n\n{footer}"), timeout=8)
                        delivered = 1
                    except Exception:
                        delivered = 0
                        fail_reason = "DM failed (privacy settings / closed DMs)."

                    # Audit record
                    cur.execute("""
                        INSERT INTO santa_deliveries (
                          day_key, sender_id, sender_name,
                          recipient_id, recipient_name,
                          message_text, delivered, fail_reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        dk,
                        str(interaction.user.id),
                        str(interaction.user),
                        str(recipient_user.id),
                        str(recipient_user),
                        msg_raw,
                        delivered,
                        fail_reason,
                        now_utc_iso()
                    ))
                    con.commit()

                    delivery_result_line = "Alright. Delivered. Don’t make it weird." if delivered else "Tried to deliver it. Their DMs are locked."

        con.close()

        # Santa reply to the sender (ephemeral)
        base_reply = await santa_says_async(
            f"IMVU: {self.imvu_name.value.strip()}\nWish: {self.wish_text.value.strip()}\nNote: {self.note.value.strip() if self.note.value else ''}",
            context_hint="They just submitted a wish. Reply as Santa in 1–2 sentences, energetic modern British slang, cheeky. No emojis."
        )

        reply_text = f"{base_reply}\n\n{delivery_result_line}" if delivery_result_line else base_reply

    except Exception as e:
        print("Santa modal submit error:", repr(e))
        reply_text = "Nah, that one glitched. Try again in a sec."

    finally:
        # ALWAYS end the interaction; never leave it "thinking..."
        if reply_text is None:
            reply_text = "Alright. Done."
        try:
            await interaction.followup.send(reply_text, ephemeral=True)
        except Exception as e:
            print("Santa followup failed:", repr(e))


class SantaWishOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Open Santa Wish Form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SantaWishModal())

# =========================
# MIKE-only: DM wish list + deliveries
# =========================

async def send_today_list_dm(user: discord.User):
    dk = day_key_Europe/London()
    con = db()
    cur = con.cursor()

    cur.execute("""
        SELECT imvu_name, wish_text, discord_name
        FROM santa_wishes
        WHERE day_key = ?
        ORDER BY id DESC
    """, (dk,))
    wishes = cur.fetchall()

    cur.execute("""
        SELECT sender_name, recipient_name, message_text, delivered, fail_reason
        FROM santa_deliveries
        WHERE day_key = ?
        ORDER BY id DESC
    """, (dk,))
    deliveries = cur.fetchall()

    con.close()

    header = santa_says(
        "Mike asked for today's list.",
        context_hint="Write a short energetic header as Santa for Mike. One sentence. No emojis."
    )

    parts = [f"**Today’s Santa Log — {dk}**\n{header}\n"]

    if wishes:
        lines = []
        for idx, (imvu, wish, dname) in enumerate(wishes, start=1):
            w = (wish or "").replace("\n", " ").strip()
            if len(w) > 140:
                w = w[:140].rsplit(" ", 1)[0] + "…"
            lines.append(f"{idx}. **{imvu}** — {w}  _(from {dname})_")
        parts.append("**Wishes**\n" + "\n".join(lines))
    else:
        parts.append("**Wishes**\nNone today.")

    if deliveries:
        dlines = []
        for idx, (sname, rname, msg, delivered, fail_reason) in enumerate(deliveries, start=1):
            m = (msg or "").replace("\n", " ").strip()
            if len(m) > 140:
                m = m[:140].rsplit(" ", 1)[0] + "…"
            status = "DELIVERED" if delivered else f"FAILED: {fail_reason or 'Unknown'}"
            dlines.append(f"{idx}. **{sname} → {rname}** — {m}  _({status})_")
        parts.append("\n**Anonymous Deliveries**\n" + "\n".join(dlines))
    else:
        parts.append("\n**Anonymous Deliveries**\nNone today.")

    text = "\n\n".join(parts)
    # Split if too long for one DM
    if len(text) <= 3800:
        await user.send(text)
    else:
        # crude split
        chunks = []
        while text:
            chunk = text[:3800]
            text = text[3800:]
            chunks.append(chunk)
        for c in chunks:
            await user.send(c)

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

    # ----- MIKE-only list (DM only) -----
    if message.author.id == MIKE_USER_ID and content_l == "santa list":
        try:
            await send_today_list_dm(message.author)
        except Exception:
            await message.reply("Couldn’t DM you. Turn on DMs for this server and try again.", mention_author=False)
        return

    # ----- Optional: restrict wishing to one channel -----
    if WISH_CHANNEL_ID and message.channel.id != WISH_CHANNEL_ID:
        return

    # ----- WISH TRIGGER MUST WIN (even if they mention @Santa etc.) -----
    # This catches:
    # "wish to santa"
    # "wish to santa @Santa"
    # "@Santa wish to santa"
    # "wish to santa\n@Santa"
    if "wish to santa" in content_l:
        tease = await santa_says_async(
            "They want to submit a wish.",
            context_hint="Tell them to click the button to submit their wish. One short energetic sentence. No emojis."
        )
        await message.reply(tease, view=SantaWishOpenView(), mention_author=False)
        return

    # ----- Casual chat: starts with 'santa' -----
    if content_l.startswith("santa"):
        reply = await santa_says_async(
            content,
            context_hint="They spoke to you casually. Reply as Santa in 1–2 sentences, modern British slang, playful and confident. No emojis."
        )
        await message.reply(reply, mention_author=False)
        return

    # ----- Reply when mentioned -----
    if bot.user and bot.user.mentioned_in(message):
        if message.mention_everyone:
            return
        cleaned = (
            content.replace(f"<@{bot.user.id}>", "")
                   .replace(f"<@!{bot.user.id}>", "")
                   .strip()
        )
        if cleaned:
            reply = await santa_says_async(
                cleaned,
                context_hint="They mentioned you. Reply as Santa in 1–2 sentences, modern British slang, playful. No emojis."
            )
            await message.reply(reply, mention_author=False)
        return

# =========================
# Run
# =========================

bot.run(DISCORD_TOKEN)
