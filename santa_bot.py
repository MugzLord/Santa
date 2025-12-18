import os
import re
import random
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands


import asyncio

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
DB_PATH = os.getenv("SANTA_DB_PATH", "santa.db")

LONDON_TZ = ZoneInfo("Europe/London")

TRIGGERS = {"wish to santa", "dear santa", "santa wish"}
LIST_TRIGGER = "santa list"  # MIKE only

GUILD_ID = int(os.getenv("SANTA_GUILD_ID", "0"))

# =========================
# OpenAI (Santa voice)
# =========================
from openai import OpenAI

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SANTA_SYSTEM_PROMPT = """
You are Santa.

You are 35, sharp, funny, cheeky, and full of modern British energy.
You roast lightly, flirt back if they flirt (PG-13), and keep it festive.
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

BANNED_STYLE_PHRASES = [
    "oi",
    "mate",
    "love",
    "darling",
    "sweetheart"
]


def sanitise_santa(text: str) -> str:
    t = (text or "").strip()
    low = t.lower()

    # If the model ever leaks forbidden “techy” words, force a Santa deflection
    if any(p in low for p in BANNED_PHRASES):
        try:
            out = santa_says(
                "They asked about forbidden details.",
                context_hint=(
                    "Deflect firmly but playful as Santa. "
                    "Tell them to submit the wish and behave. "
                    "Modern British slang. 1 sentence. No emojis."
                )
            )
            out = (out or "").strip()
            return out if out else "Less questions. More wishing. Behave."
        except Exception:
            return "Less questions. More wishing. Behave."

    # If the model uses banned style words (e.g., 'oi', 'mate', 'love'), re-roll tone
    if any(p in low for p in BANNED_STYLE_PHRASES):
        try:
            out = santa_says(
                "Your last line used banned style words. Rephrase cleanly.",
                context_hint=(
                    "Rewrite in a clean, neutral British tone (no 'oi', 'mate', 'love', etc.). "
                    "Still confident and cheeky. 1 sentence. No emojis."
                )
            )
            out = (out or "").strip()
            return out if out else "Easy. Put your wish in properly and we’ll talk."
        except Exception:
            return "Easy. Put your wish in properly and we’ll talk."

    t = t.replace("\n", " ").strip()
    if len(t) > 350:
        t = t[:350].rsplit(" ", 1)[0] + "…"
    return t

def mike_only(interaction: discord.Interaction) -> bool:
    return interaction.user.id == MIKE_USER_ID


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
            "That’s cheeky, mate. I respect the confidence.",
            "Noted. Behave till the results.",
        ])


async def santa_says_async(user_text: str, context_hint: str = "") -> str:
    # run the sync call off the event loop so Discord interactions don't hang
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


    con.commit()
    con.close()


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

    # mention
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

    # numeric ID
    if ID_RE.match(raw):
        uid = int(raw)
        member = interaction.guild.get_member(uid)
        if member:
            return member
        try:
            return await interaction.guild.fetch_member(uid)
        except Exception:
            return None

    # NOTE: requires Members intent + member cache to be populated
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


def sender_can_send_today(sender_id: int) -> bool:
    dk = day_key_London()  # FIXED
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT COUNT(1) FROM santa_deliveries
        WHERE day_key = ? AND sender_id = ?
    """, (dk, str(sender_id)))
    count = cur.fetchone()[0] or 0
    con.close()
    return count < 10


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

async def delete_if_possible(message: discord.Message):
    try:
        await message.delete()
    except Exception:
        # Missing permissions or not allowed in that channel
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply_text = None

        try:
            dk = day_key_London()

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

            # DM Mike a private log (optional, but you wanted it)
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
            reply_text = "Nah, that one glitched. Try again in a sec."

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

            # Log delivery
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

            # DM Mike a private copy
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
            reply_text = "Nah, that one glitched. Try again in a sec."

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

            
class SantaAnonPickRecipientView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

        select = discord.ui.UserSelect(
            placeholder="Pick who gets the anonymous message…",
            min_values=1,
            max_values=1
        )
        select.callback = self.pick_callback
        self.add_item(select)

    async def pick_callback(self, interaction: discord.Interaction):
        # the first component in this view is the UserSelect we added
        select: discord.ui.UserSelect = self.children[0]
        recipient = select.values[0]
        await interaction.response.send_modal(SantaAnonModal(recipient.id))

class SantaWishOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Open Santa Wish Form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Delete the entire message (text + button)
        try:
            await interaction.message.delete()
        except Exception:
            pass  # ignore if missing permissions

        # Open the modal
        await interaction.response.send_modal(SantaWishModal())

# =========================
# MIKE-only: DM wish list + deliveries
# =========================

async def send_today_list_dm(user: discord.User):
    dk = day_key_London()  # FIXED

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
# Events
# =========================

@bot.event
async def on_ready():
    init_db()
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

    print(f"Santa logged in as {bot.user} (app id: {bot.application_id}).")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    content_l = content.lower()

    # DM opt-out: user DMs Santa "STOP"
    if isinstance(message.channel, discord.DMChannel):
        if content_l == "stop":
            con = db()
            cur = con.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO santa_blocks (user_id, blocked_at)
                VALUES (?, ?)
            """, (str(message.author.id), now_utc_iso()))
            con.commit()
            con.close()

            reply = santa_says(
                "They opted out.",
                context_hint="Confirm they've opted out. One sentence. Modern British slang. No emojis."
            )
            await message.reply(reply)
        return

    # MIKE-only list (DM only)
    if message.author.id == MIKE_USER_ID and content_l == "santa list":
        await delete_if_possible(message)
        try:
            await send_today_list_dm(message.author)
        except Exception:
            try:
                await message.author.send("Couldn’t pull the list. Check DB path / permissions and try again.")
            except Exception:
                pass
        return

    if message.author.id == MIKE_USER_ID and content_l.startswith("santa pick"):
        await delete_if_possible(message)
        parts = content_l.split()
        if len(parts) != 4:
            await message.author.send("Use: `santa pick 3 7`")
            return
        try:
            p1 = int(parts[2]); p2 = int(parts[3])
        except ValueError:
            await message.author.send("Use numbers: `santa pick 3 7`")
            return
    
        dk, wishes = get_today_wishes()
        if len(wishes) < 2:
            await message.author.send("Not enough wishes today to pick 2 winners.")
            return
        if p1 == p2 or p1 < 1 or p2 < 1 or p1 > len(wishes) or p2 > len(wishes):
            await message.author.send("Those pick numbers aren’t valid. Check `santa list` and try again.")
            return
    
        save_today_picks(p1, p2)
        await message.author.send(f"Locked. Picks are **#{p1}** and **#{p2}**. Then run: `santa announce`.")
        return
        
    # MIKE-only announce (Option A) — put BEFORE channel restriction so it works anywhere
    if message.author.id == MIKE_USER_ID and content_l == "santa announce":
        await santa_announce_today(message.channel)
        return

    # Optional restrict wishing to one channel
    if WISH_CHANNEL_ID and message.channel.id != WISH_CHANNEL_ID:
        return

    # Wish trigger always wins
    if "wish to santa" in content_l:
        # Hide who triggered it
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
                "They spoke to you casually. "
                "Reply as Santa: funny, cheeky, modern British energy. "
                "Playful banter, confident. "
                "If they flirt, flirt back lightly (PG-13). "
                "1–2 sentences. No emojis."
            )
        )

        await message.reply(reply, mention_author=False)
        return

    # Reply when mentioned
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


async def santa_announce_today_after_delay(channel: discord.abc.Messageable, delay_seconds: int = 300):
    await asyncio.sleep(delay_seconds)

    dk, wishes = get_today_wishes()
    picks = get_today_picks()

    if not picks:
        await channel.send("I’ve got no locked picks for today. Mike needs to run `santa pick X Y` first.")
        return

    p1, p2 = picks
    if len(wishes) < 2:
        await channel.send("Not enough wishes today. Try again tomorrow.")
        return

    if p1 == p2 or p1 < 1 or p2 < 1 or p1 > len(wishes) or p2 > len(wishes):
        await channel.send("Those locked picks don’t match today’s list. Mike should re-pick.")
        return

    w1 = wishes[p1 - 1]
    w2 = wishes[p2 - 1]
    winner_names = [w1[0], w2[0]]

    # Mark announced once per day (uses your existing santa_announcements table)
    con = db()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM santa_announcements WHERE day_key = ?", (dk,))
    if cur.fetchone():
        con.close()
        return  # already announced
    cur.execute("""
        INSERT INTO santa_announcements (day_key, announced_at)
        VALUES (?, ?)
    """, (dk, now_utc_iso()))
    con.commit()
    con.close()

    intro = await santa_says_async(
        "Announce today’s two winners.",
        context_hint="Announce two winners confidently as Santa. Funny, cheeky, festive. One sentence. No emojis."
    )

    lines = "\n".join(
        f"• **{name}** — 5k credits or the wish equivalent."
        for name in winner_names
    )

    outro = await santa_says_async(
        "Close the announcement.",
        context_hint="Short cheeky closing line as Santa. One sentence. No emojis."
    )

    await channel.send(
        f"🎅 **Santa’s Desk — {dk}**\n"
        f"{intro}\n\n"
        f"{lines}\n\n"
        f"{outro}"
    )
@bot.tree.command(name="pick", description="Pick winner(s) (Mike only)")
@app_commands.describe(count="How many winners to pick", public="Announce publicly in the wish channel?")
async def pick(interaction: discord.Interaction, count: int = 1, public: bool = False):
    if not mike_only(interaction):
        return await interaction.response.send_message("Nope. Mike only.", ephemeral=True)

    if count < 1:
        return await interaction.response.send_message("Count must be 1 or more.", ephemeral=True)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # ---- CHANGE THIS QUERY to match your actual entries table/columns ----
    # Expected: one row per entry with at least a user id / username field.
    cur.execute("SELECT DISTINCT user_id, imvu_username FROM wish_entries")
    rows = cur.fetchall()
    con.close()

    if not rows:
        return await interaction.response.send_message("No entries found to pick from.", ephemeral=True)

    if count > len(rows):
        count = len(rows)

    winners = random.sample(rows, k=count)
    winners_text = "\n".join([f"- <@{uid}> — **{name}**" for uid, name in winners])

    await interaction.response.send_message(
        f"Picked **{count}** winner(s):\n{winners_text}",
        ephemeral=True
    )

    if public:
        ch = bot.get_channel(WISH_CHANNEL_ID)
        if ch:
            await ch.send(f"🎅 **Santa Results**\n{winners_text}")
            
@bot.tree.command(name="santa_announce", description="Post a Santa announcement (Mike only)")
@app_commands.describe(message="Announcement text to post")
async def santa_announce(interaction: discord.Interaction, message: str):
    if not mike_only(interaction):
        return await interaction.response.send_message("Nope. Mike only.", ephemeral=True)

    ch = bot.get_channel(WISH_CHANNEL_ID)
    if not ch:
        return await interaction.response.send_message("Wish channel not found. Check WISH_CHANNEL_ID.", ephemeral=True)

    await ch.send(f"🎅 **Santa Announcement**\n{message}")
    await interaction.response.send_message("Posted.", ephemeral=True)

@bot.tree.command(name="santa", description="Santa: wish entries or anonymous messages")
async def santa_cmd(interaction: discord.Interaction):
    if WISH_CHANNEL_ID and interaction.channel_id != WISH_CHANNEL_ID:
        await interaction.response.send_message("Use this in the wish channel.", ephemeral=True)
        return

    # Respond immediately (prevents "did not respond")
    await interaction.response.send_message(
        "Alright. Pick your chaos.",
        view=SantaMainMenu(),
        ephemeral=True
    )



bot.run(DISCORD_TOKEN)
