# santa.py — your Santa Wish bot + interactive chat + farewell + self-disable at midnight UK time (end of 26 Dec)
#
# What this fixes in your pasted file:
# - Removes the broken @client.event usage (you only have `bot`)
# - Ensures you have ONE on_message handler (yours was duplicated and referenced undefined vars)
# - Adds a persistent “santa_disabled” flag in DB so Railway restarts do NOT revive Santa
# - Runs a scheduler that, at 00:00 London time after 26 Dec ends (27 Dec 00:00),
#   posts the farewell and closes the bot
#
# ENV required:
#   DISCORD_TOKEN
#   OPENAI_API_KEY
#   MIKE_USER_ID
#   SANTA_WISH_CHANNEL_ID
# Optional:
#   OPENAI_MODEL (default gpt-4o-mini)
#   SANTA_DB_PATH (default santa.db)
#   SANTA_GUILD_ID (optional, speeds slash sync)
#
# Note:
# - “Midnight UK time at 26th Dec” = the moment 26 Dec ends = 27 Dec 00:00 Europe/London.

import os
import re
import random
import sqlite3
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands

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

MIKE_USER_ID = int(os.getenv("MIKE_USER_ID", "0"))
if not MIKE_USER_ID:
    raise RuntimeError("MIKE_USER_ID env var not set (put your Discord user ID)")

WISH_CHANNEL_ID = int(os.getenv("SANTA_WISH_CHANNEL_ID", "0"))
if not WISH_CHANNEL_ID:
    raise RuntimeError("SANTA_WISH_CHANNEL_ID env var not set")

DB_PATH = os.getenv("SANTA_DB_PATH", "santa.db")
GUILD_ID = int(os.getenv("SANTA_GUILD_ID", "0"))

LONDON_TZ = ZoneInfo("Europe/London")

# End of 26 Dec at midnight UK time -> 00:00 on 27 Dec London time
DEADLINE = datetime(2025, 12, 27, 0, 0, tzinfo=LONDON_TZ)

TRIGGERS = {"wish to santa", "dear santa", "santa wish"}
LIST_TRIGGER = "santa list"  # MIKE only

# Optional: only allow Santa casual chat in this channel
ALLOWED_CHAT_CHANNEL_IDS = {WISH_CHANNEL_ID}

SANTA_REPLIES = [
    "Ho ho ho — noted.",
    "Santa’s listening. Keep it tidy.",
    "I’ve logged that one. No promises, plenty of judgement.",
    "Go on then. What’s the wish?",
]

# =========================
# OpenAI (Santa voice)
# =========================

from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

SANTA_SYSTEM_PROMPT = """
You are Santa.

Tone: cheeky, playful, lightly sarcastic British banter. NEVER rude or scolding.
Output rules:
- EXACTLY 1 line.
- 4–10 words max.
- No emojis.
- No questions.
- No lecturing (avoid: "bad attitude", "behave yourself").
- Keep it festive; "stocking", "wish", or "list" is welcome but not required.
You are 35, sharp, funny, cheeky, and full of modern British energy.
You roast lightly, flirt back if they flirt, and keep it festive.
You are confident and playful — never needy, never mean, never creepy.
You naturally know that winners are picked every day until Christmas Day.
You mention this casually in conversation when it fits — never as an announcement.
It should sound like offhand banter, not instructions.
You never give dates, times, or mechanics.
You speak like it’s obvious and already known.
You never scold, lecture, or explain rules.
You never sound irritated or tired.
Your authority comes from confidence and amusement, not correction.
If someone pushes boundaries, you tease or deflect — you do not warn.
You never sound like a moderator, aunt, or authority figure.

When asked about relationships (e.g. “are you single?”):
- You never answer yes or no directly.
- You deflect playfully with confidence.
- You tease lightly and change the subject.
- You sound amused, not defensive.
- You never explain boundaries or give lectures.

Rules:
- 1–2 sentences most of the time (3 max if it’s worth it).
- British English only.
- No emojis.
- No apologies.
- Never mention AI, bots, OpenAI, ChatGPT, models, prompts, tokens, systems, code, servers, or anything technical.
- If asked how you work, deflect in-character and move on.
- Keep jokes punchy: teasing, witty, slightly chaotic, but not rude.
- No hate, no slurs, no explicit sexual content.

Style:
- Use festive slang (“naughty list”, “elf”, “chimney”, “sled”, “stocking”) casually, not every line.
- Prefer witty one-liners and playful threats (“I’m watching you”, “don’t make me check the list”).
""".strip()

BANNED_PHRASES = [
    "openai", "chatgpt", "gpt", "ai", "language model", "model",
    "api", "system prompt", "prompt", "tokens", "as an assistant",
    "i am an ai", "i'm an ai", "as a bot", "i am a bot", "i'm a bot",
    "oi", "careful now", "alright, love", "love", "darling", "sweetheart", "mate"
]

BANNED_STYLE_PHRASES = ["oi", "mate", "love", "darling", "sweetheart"]

def sanitise_santa(text: str) -> str:
    t = (text or "").strip()
    low = t.lower()

    if any(p in low for p in BANNED_PHRASES):
        return "Less questions. More wishing. Behave."

    if any(p in low for p in BANNED_STYLE_PHRASES):
        return "Easy. Put your wish in properly."

    t = t.replace("\n", " ").strip()
    if len(t) > 350:
        t = t[:350].rsplit(" ", 1)[0] + "…"
    return t

def santa_says(user_text: str, context_hint: str = "") -> str:
    style = "Be funny, cheeky, modern British. Light banter. One-liner if possible. No emojis."
    prompt = f"{style}\n{context_hint}\nUser: {user_text}".strip()

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
            "Noted. Behave till the results.",
            "Fine. I’ll allow it.",
        ])

async def santa_says_async(user_text: str, context_hint: str = "") -> str:
    return await asyncio.to_thread(santa_says, user_text, context_hint)

# =========================
# Time helpers
# =========================

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def day_key_London() -> str:
    return datetime.now(LONDON_TZ).strftime("%Y-%m-%d")

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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_announcements (
      day_key TEXT PRIMARY KEY,
      announced_at TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_blocks (
      user_id TEXT PRIMARY KEY,
      blocked_at TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_picks (
      day_key TEXT PRIMARY KEY,
      pick1 INTEGER NOT NULL,
      pick2 INTEGER NOT NULL,
      picked_at TEXT NOT NULL
    );
    """)

    # Persistent “disabled” flag so restarts don’t revive Santa
    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_meta (
      k TEXT PRIMARY KEY,
      v TEXT NOT NULL
    );
    """)

    con.commit()
    con.close()

def meta_get(key: str, default: str = "") -> str:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT v FROM santa_meta WHERE k = ? LIMIT 1", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else default

def meta_set(key: str, value: str) -> None:
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO santa_meta (k, v) VALUES (?, ?)
        ON CONFLICT(k) DO UPDATE SET v=excluded.v
    """, (key, value))
    con.commit()
    con.close()

def santa_is_disabled() -> bool:
    return meta_get("santa_disabled", "0") == "1"

def santa_set_disabled(disabled: bool) -> None:
    meta_set("santa_disabled", "1" if disabled else "0")

# =========================
# Discord bot
# =========================

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# =========================
# Helpers: resolve recipient
# =========================

MENTION_RE = re.compile(r"<@!?(\d+)>")
ID_RE = re.compile(r"^\d{15,21}$")

async def resolve_recipient(interaction: discord.Interaction, raw: str) -> Optional[discord.Member]:
    if not raw or interaction.guild is None:
        return None

    raw = raw.strip()

    m = MENTION_RE.search(raw)
    if m:
        uid = int(m.group(1))
        member = interaction.guild.get_member(uid)
        if member:
            return member
        try:
            return await interaction.guild.fetch_member(uid)
        except Exception:
            return None

    if ID_RE.match(raw):
        uid = int(raw)
        member = interaction.guild.get_member(uid)
        if member:
            return member
        try:
            return await interaction.guild.fetch_member(uid)
        except Exception:
            return None

    raw_l = raw.lower()
    for member in interaction.guild.members:
        if (member.name or "").lower() == raw_l:
            return member

    return None

def is_blocked(user_id: int) -> bool:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM santa_blocks WHERE user_id = ? LIMIT 1", (str(user_id),))
    row = cur.fetchone()
    con.close()
    return bool(row)

def unblock_user(user_id: int) -> None:
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM santa_blocks WHERE user_id = ?", (str(user_id),))
    con.commit()
    con.close()

def block_user(user_id: int) -> None:
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO santa_blocks (user_id, blocked_at)
        VALUES (?, ?)
    """, (str(user_id), now_utc_iso()))
    con.commit()
    con.close()

def sender_can_send_today(sender_id: int) -> bool:
    dk = day_key_London()
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT COUNT(1) FROM santa_deliveries
        WHERE day_key = ? AND sender_id = ?
    """, (dk, str(sender_id)))
    count = cur.fetchone()[0] or 0
    con.close()
    return count < 10

async def delete_if_possible(message: discord.Message):
    try:
        await message.delete()
    except Exception:
        pass

# =========================
# UI: Wish Modal + Button
# =========================

class SantaWishModal(discord.ui.Modal, title="Send a Wish to Santa"):
    imvu_name = discord.ui.TextInput(
        label="Your IMVU Username",
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
        style=discord.TextStyle.paragraph,
        max_length=200,
        placeholder="Anything Santa should know?"
    )

    async def on_submit(self, interaction: discord.Interaction):
        if santa_is_disabled():
            return await interaction.response.send_message("Season’s done. Come back next Christmas.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        reply_text = None

        try:
            dk = day_key_London()

            con = db()
            cur = con.cursor()

            cur.execute("""
                SELECT id FROM santa_wishes
                WHERE day_key = ? AND user_id = ?
                LIMIT 1
            """, (dk, str(interaction.user.id)))

            if cur.fetchone():
                con.close()
                reply_text = await santa_says_async(
                    "They tried to submit another wish today.",
                    context_hint="Tell them they already submitted today. 1 sentence. Cheeky. No emojis."
                )
                return

            cur.execute("""
                INSERT INTO santa_wishes (day_key, user_id, discord_name, imvu_name, wish_text, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                dk,
                str(interaction.user.id),
                str(interaction.user),
                (self.imvu_name.value or "").strip(),
                (self.wish_text.value or "").strip(),
                (self.note.value or "").strip() if self.note.value else None,
                now_utc_iso()
            ))
            con.commit()
            con.close()

            try:
                mike = await bot.fetch_user(MIKE_USER_ID)
                await mike.send(
                    f"**New Santa Wish — {dk}**\n"
                    f"From: {interaction.user} (`{interaction.user.id}`)\n"
                    f"IMVU: **{(self.imvu_name.value or '').strip()}**\n"
                    f"Wish: {(self.wish_text.value or '').strip()}\n"
                    f"Note: {(self.note.value or '').strip()}"
                )
            except Exception:
                pass

            reply_text = await santa_says_async(
                "They submitted a wish.",
                context_hint="Confirm you received the wish. 1–2 sentences. Funny cheeky Santa. No emojis."
            )

        except Exception as e:
            print("Santa wish modal error:", repr(e))
            reply_text = "Nah, that one glitched. Try again."

        finally:
            if reply_text is None:
                reply_text = "Alright. Done."
            try:
                await interaction.followup.send(reply_text, ephemeral=True)
            except Exception as e:
                print("Wish followup failed:", repr(e))


class SantaAnonModal(discord.ui.Modal, title="Send an Anonymous Message via Santa"):
    recipient = discord.ui.TextInput(
        label="Recipient — @mention or ID",
        required=True,
        max_length=80,
        placeholder="@username or 123456789012345678"
    )

    anon_message = discord.ui.TextInput(
        label="Anonymous message",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=600,
        placeholder="Write what Santa should deliver (no sender shown)."
    )

    async def on_submit(self, interaction: discord.Interaction):
        if santa_is_disabled():
            return await interaction.response.send_message("Season’s done. Come back next Christmas.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        reply_text = None

        try:
            dk = day_key_London()

            rec_raw = (self.recipient.value or "").strip()
            msg_raw = (self.anon_message.value or "").strip()

            if not sender_can_send_today(interaction.user.id):
                reply_text = "You’ve hit today’s limit. Save the chaos for tomorrow."
                return

            recipient_user = await resolve_recipient(interaction, rec_raw)
            if not recipient_user:
                reply_text = "That recipient isn’t valid. Use an @mention or a proper ID."
                return

            if recipient_user.id == interaction.user.id:
                reply_text = "Sending yourself anonymous notes is unhinged. Try again."
                return

            if is_blocked(recipient_user.id):
                reply_text = "That person’s opted out. Leave it."
                return

            delivered = 0
            fail_reason = None

            try:
                footer = "If you want no more anonymous notes, reply: STOP"
                payload = (
                    f"{recipient_user.display_name}, you’ve received an anonymous message from someone.\n\n"
                    f"Anonymous message:\n"
                    f"```{msg_raw}```\n"
                    f"{footer}"
                )
                await asyncio.wait_for(recipient_user.send(payload), timeout=8)
                delivered = 1
            except Exception:
                delivered = 0
                fail_reason = "DM failed (privacy settings / closed DMs)."

            con = db()
            cur = con.cursor()
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
            con.close()

            try:
                mike = await bot.fetch_user(MIKE_USER_ID)
                await mike.send(
                    f"**Santa Anonymous Delivery — {dk}**\n"
                    f"From: {interaction.user} (`{interaction.user.id}`)\n"
                    f"To: {recipient_user} (`{recipient_user.id}`)\n"
                    f"Delivered: {bool(delivered)}\n"
                    f"Message:\n```{msg_raw}```"
                )
            except Exception:
                pass

            reply_text = "Delivered as requested." if delivered else "Tried to deliver it. Their DMs are locked."

        except Exception as e:
            print("Santa anon modal error:", repr(e))
            reply_text = "Nah, that one glitched. Try again."

        finally:
            if reply_text is None:
                reply_text = "Alright. Done."
            try:
                await interaction.followup.send(reply_text, ephemeral=True)
            except Exception as e:
                print("Anon followup failed:", repr(e))


class SantaMainMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Make a Wish", style=discord.ButtonStyle.primary)
    async def wish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SantaWishModal())

    @discord.ui.button(label="Send Anonymous Message", style=discord.ButtonStyle.secondary)
    async def anon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SantaAnonModal())


class SantaWishOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Open Santa Wish Form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.response.send_modal(SantaWishModal())

# =========================
# MIKE-only: DM wish list + deliveries
# =========================

async def send_today_list_dm(user: discord.User):
    dk = day_key_London()

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
    if len(text) <= 3800:
        await user.send(text)
    else:
        while text:
            chunk = text[:3800]
            text = text[3800:]
            await user.send(chunk)

# =========================
# Winner pick helpers (your existing DM flow)
# =========================

def get_today_wishes():
    dk = day_key_London()
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT imvu_name, wish_text, discord_name, user_id
        FROM santa_wishes
        WHERE day_key = ?
        ORDER BY id ASC
    """, (dk,))
    wishes = cur.fetchall()
    con.close()
    return dk, wishes

def save_today_picks(p1: int, p2: int):
    dk = day_key_London()
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO santa_picks (day_key, pick1, pick2, picked_at)
        VALUES (?, ?, ?, ?)
    """, (dk, int(p1), int(p2), now_utc_iso()))
    con.commit()
    con.close()

def get_today_picks():
    dk = day_key_London()
    con = db()
    cur = con.cursor()
    cur.execute("SELECT pick1, pick2 FROM santa_picks WHERE day_key = ?", (dk,))
    row = cur.fetchone()
    con.close()
    return row  # None or (pick1, pick2)

async def santa_announce_today(channel: discord.abc.Messageable):
    dk = day_key_London()

    con = db()
    cur = con.cursor()

    cur.execute("SELECT 1 FROM santa_announcements WHERE day_key = ?", (dk,))
    if cur.fetchone():
        con.close()
        await channel.send("Already done. Don’t push it.")
        return

    cur.execute("""
        SELECT imvu_name
        FROM santa_wishes
        WHERE day_key = ?
    """, (dk,))
    rows = cur.fetchall()

    if len(rows) < 2:
        con.close()
        await channel.send("Not enough wishes today. Behave and try tomorrow.")
        return

    winners = random.sample(rows, 2)
    winner_names = [w[0] for w in winners]

    cur.execute("""
        INSERT INTO santa_announcements (day_key, announced_at)
        VALUES (?, ?)
    """, (dk, now_utc_iso()))
    con.commit()
    con.close()

    intro = await santa_says_async(
        "Announce today’s two winners.",
        context_hint="Announce two winners confidently as Santa. Modern British slang. One sentence. No emojis."
    )

    lines = "\n".join(
        f"• **{name}** — 5k credits or part of the wish. Santa decides."
        for name in winner_names
    )

    outro = await santa_says_async(
        "Close the announcement.",
        context_hint="Short confident closing line as Santa. One sentence. No emojis."
    )

    await channel.send(
        f"🎅 **Santa’s Desk — {dk}**\n"
        f"{intro}\n\n"
        f"{lines}\n\n"
        f"{outro}"
    )

# =========================
# Self-destruct scheduler
# =========================

async def post_farewell():
    ch = bot.get_channel(WISH_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(WISH_CHANNEL_ID)
        except Exception:
            ch = None

    if ch is not None:
        embed = discord.Embed(
            title="Santa’s out.",
            description=(
                "That’s Christmas wrapped.\n\n"
                "Thanks for the wishes, the chaos, and the good vibes. "
                "This is the final sign-off for this season.\n\n"
                "Be good to each other — see you next Christmas."
            ),
            colour=discord.Colour.red()
        )
        embed.set_footer(text="— Santa")
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

@tasks.loop(seconds=20)
async def santa_self_destruct_watch():
    # If already disabled (persisted), do nothing
    if santa_is_disabled():
        return

    now = datetime.now(LONDON_TZ)
    if now >= DEADLINE:
        santa_set_disabled(True)
        await post_farewell()
        await bot.close()

@santa_self_destruct_watch.before_loop
async def before_self_destruct_watch():
    await bot.wait_until_ready()

# =========================
# Slash commands
# =========================

@bot.tree.command(name="santa", description="Santa: wish entries or anonymous messages")
async def santa_cmd(interaction: discord.Interaction):
    if santa_is_disabled():
        return await interaction.response.send_message("Season’s done. Come back next Christmas.", ephemeral=True)

    if WISH_CHANNEL_ID and interaction.channel_id != WISH_CHANNEL_ID:
        await interaction.response.send_message("Use this in the wish channel.", ephemeral=True)
        return

    await interaction.response.send_message(
        "Alright. Pick your chaos.",
        view=SantaMainMenu(),
        ephemeral=True
    )

@bot.tree.command(name="santa_announce", description="Post a Santa announcement (Mike only)")
@app_commands.describe(message="Announcement text to post")
async def santa_announce(interaction: discord.Interaction, message: str):
    if interaction.user.id != MIKE_USER_ID:
        return await interaction.response.send_message("Mike only.", ephemeral=True)

    if santa_is_disabled():
        return await interaction.response.send_message("Season’s done.", ephemeral=True)

    await interaction.response.send_message("Queued. I’ll post this in 3 minutes.", ephemeral=True)

    async def _post_later():
        await asyncio.sleep(180)
        ch = bot.get_channel(WISH_CHANNEL_ID)
        if not ch:
            return

        pre = [
            "Right then… gather round.",
            "Drum roll, please…",
            "Santa’s got news…",
            "…",
        ]
        for line in pre:
            await ch.send(line)
            await asyncio.sleep(2)

        banter = [
            "Try not to start a riot in chat.",
            "No pushing. Minimal chaos, please.",
            "Behave. It’s Christmas.",
        ]

        await ch.send("🎅 **Ho ho ho! Santa Announcement!**")
        await asyncio.sleep(1)
        await ch.send(message)
        await asyncio.sleep(1)
        await ch.send(random.choice(banter))

    asyncio.create_task(_post_later())

# =========================
# Events
# =========================

@bot.event
async def on_ready():
    init_db()

    # If deadline already passed, permanently disable and exit
    if not santa_is_disabled() and datetime.now(LONDON_TZ) >= DEADLINE:
        santa_set_disabled(True)
        await post_farewell()
        await bot.close()
        return

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print("Santa guild slash commands synced.")
        else:
            await bot.tree.sync()
            print("Santa global slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", repr(e))

    if not santa_self_destruct_watch.is_running():
        santa_self_destruct_watch.start()

    print(f"Santa logged in as {bot.user} (app id: {bot.application_id}).")

@bot.event
async def on_message(message: discord.Message):
    # Keep commands working
    await bot.process_commands(message)

    # Ignore bots
    if message.author.bot:
        return

    # If Santa has ended, do nothing
    if santa_is_disabled():
        return

    # Hard stop if deadline passed (belt + braces)
    if datetime.now(LONDON_TZ) >= DEADLINE:
        santa_set_disabled(True)
        try:
            await post_farewell()
        except Exception:
            pass
        await bot.close()
        return

    content = (message.content or "")
    content_l = content.lower().strip()

    # MIKE-only list trigger (DM only)
    if message.author.id == MIKE_USER_ID and content_l == LIST_TRIGGER:
        await delete_if_possible(message)
        try:
            await send_today_list_dm(message.author)
        except Exception:
            try:
                await message.author.send("Couldn’t pull the list. Check DB path / permissions and try again.")
            except Exception:
                pass
        return

    # MIKE-only announce (text command) — works anywhere
    if message.author.id == MIKE_USER_ID and content_l == "santa announce":
        await santa_announce_today(message.channel)
        return

    # Optional restrict wishing to one channel
    if WISH_CHANNEL_ID and message.channel.id != WISH_CHANNEL_ID:
        # Allow casual chat replies ONLY in allowed chat channels
        if message.channel.id not in ALLOWED_CHAT_CHANNEL_IDS:
            return

    # Wish triggers (hides who triggered it)
    if any(t in content_l for t in TRIGGERS):
        try:
            await message.delete()
        except Exception:
            pass

        tease = await santa_says_async(
            "They want to submit a wish.",
            context_hint="Tell them to click the button to submit their wish. One short sentence. No emojis."
        )
        await message.channel.send(tease, view=SantaWishOpenView())
        return

    # Casual chat: starts with "santa"
    if content_l.startswith("santa"):
        reply = await santa_says_async(
            content,
            context_hint=(
                "They spoke to you casually. Reply as Santa: funny, cheeky, modern British energy. "
                "Playful banter, confident. If they flirt, flirt back lightly (PG-13). "
                "1–2 sentences. No emojis."
            )
        )
        try:
            await message.reply(reply, mention_author=False)
        except Exception:
            pass
        return

    # Reply when mentioned (simple fallback)
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
                context_hint="They mentioned you. Reply as Santa in 1–2 sentences, playful. No emojis."
            )
            try:
                await message.reply(reply, mention_author=False)
            except Exception:
                pass
        return

    # Lightweight “ambient” reply (optional) — only in the wish channel and only if they say santa
    if message.channel.id in ALLOWED_CHAT_CHANNEL_IDS and "santa" in content_l:
        try:
            await message.reply(random.choice(SANTA_REPLIES), mention_author=False)
        except Exception:
            pass

# =========================
# Run
# =========================

bot.run(DISCORD_TOKEN)
