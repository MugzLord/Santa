import os
import re
import json
import random
import sqlite3
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands

# =========================
# Config (ENV)
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var not set")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY env var not set")

# Channels (IDs)
WISH_CHANNEL_ID = int(os.getenv("SANTA_WISH_CHANNEL_ID", "0"))          # where players type "wish to santa"
PUBLIC_POLL_CHANNEL_ID = int(os.getenv("SANTA_POLL_CHANNEL_ID", "0"))   # where Santa posts the poll
PUBLIC_ANNOUNCE_CHANNEL_ID = int(os.getenv("SANTA_ANNOUNCE_CHANNEL_ID", "0"))  # where Santa announces winners
ADMIN_CHANNEL_ID = int(os.getenv("SANTA_ADMIN_CHANNEL_ID", "0"))        # private admin channel for logs & panel

# Prize logic
DAILY_PRIZE_CREDITS = int(os.getenv("SANTA_DAILY_PRIZE_CREDITS", "5000"))
SHORTLIST_SIZE = int(os.getenv("SANTA_SHORTLIST_SIZE", "6"))  # poll options A-F max recommended

DB_PATH = os.getenv("SANTA_DB_PATH", "santa.db")

QATAR_TZ = ZoneInfo("Asia/Qatar")

# Trigger phrases
TRIGGERS = {
    "wish to santa",
    "dear santa",
    "santa wish",
}

# Reaction options for poll
POLL_EMOJIS = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫"]

# =========================
# OpenAI (minimal wrapper)
# =========================

# Uses the new OpenAI python SDK style if installed.
# If you already use "from openai import OpenAI" in your other bots, this will match that.
from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

SANTA_SYSTEM_PROMPT = """
You are Santa.

You are about 35 years old with confident modern British energy.
You are playful, cheeky, and sharp.
You use British slang naturally and casually.

Rules:
- 1–3 sentences per reply.
- No emojis.
- No apologies.
- No explanations.
- No promises.
- Never mention being an AI, bot, system, or OpenAI.

You judge wishes, tease lightly, and stay in control.
You sound relaxed, confident, and amused.
If someone argues or begs, shut it down calmly.
""".strip()

def santa_says(user_text: str, context_hint: str = "") -> str:
    """Generate Santa reply via OpenAI. Keep output tight."""
    prompt = f"{context_hint}\nUser: {user_text}".strip()
    try:
        resp = openai_client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": SANTA_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=120,
        )
        out = resp.choices[0].message.content.strip()
        # Hard clamp: keep it short even if model gets chatty
        if len(out) > 400:
            out = out[:400].rsplit(" ", 1)[0] + "…"
        return out or "Alright. Noted."
    except Exception:
        # Fallback if OpenAI has a moment
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
      created_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending' -- pending/picked
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_polls (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      day_key TEXT NOT NULL,
      poll_message_id TEXT NOT NULL,
      poll_channel_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      is_closed INTEGER NOT NULL DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_poll_options (
      poll_id INTEGER NOT NULL,
      letter TEXT NOT NULL,             -- A..F
      emoji TEXT NOT NULL,              -- 🇦..🇫
      wish_id INTEGER NOT NULL,
      blurb TEXT NOT NULL,
      PRIMARY KEY (poll_id, letter)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS santa_daily (
      day_key TEXT PRIMARY KEY,
      winner1_wish_id INTEGER,
      winner2_wish_id INTEGER,
      announced INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
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
INTENTS.reactions = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)  # prefix exists but we do NOT use commands

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

    def __init__(self, botref: commands.Bot):
        super().__init__()
        self.botref = botref

    async def on_submit(self, interaction: discord.Interaction):
        dk = day_key_qatar()

        con = db()
        cur = con.cursor()

        # One wish per person per day (strict)
        cur.execute("""
            SELECT id FROM santa_wishes
            WHERE day_key = ? AND user_id = ?
            LIMIT 1
        """, (dk, str(interaction.user.id)))
        if cur.fetchone():
            con.close()
            msg = santa_says(
                "They tried to submit another wish today.",
                context_hint="They already submitted a wish today. Tell them no, playfully, in 1 sentence."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

        cur.execute("""
            INSERT INTO santa_wishes (day_key, user_id, discord_name, imvu_name, wish_text, note, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            dk,
            str(interaction.user.id),
            str(interaction.user),
            self.imvu_name.value.strip(),
            self.wish_text.value.strip(),
            self.note.value.strip() if self.note.value else None,
            now_utc_iso()
        ))
        wish_id = cur.lastrowid
        con.commit()
        con.close()

        # Santa reply (ephemeral, OpenAI)
        santa_reply = santa_says(
            f"IMVU: {self.imvu_name.value.strip()}\nWish: {self.wish_text.value.strip()}\nNote: {self.note.value.strip() if self.note.value else ''}",
            context_hint="They just submitted a wish. Reply as Santa in 1–2 sentences, cheeky, modern British slang, energetic."
        )
        await interaction.response.send_message(santa_reply, ephemeral=True)

        # Admin log embed
        if ADMIN_CHANNEL_ID:
            ch = self.botref.get_channel(ADMIN_CHANNEL_ID)
            if ch:
                e = discord.Embed(
                    title="🎅 New Wish (Today)",
                    description=f"Wish ID: `{wish_id}` | Day: `{dk}`"
                )
                e.add_field(name="Discord", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
                e.add_field(name="IMVU", value=self.imvu_name.value.strip(), inline=True)
                e.add_field(name="Wish", value=self.wish_text.value.strip()[:1024], inline=False)
                if self.note.value:
                    e.add_field(name="Note", value=self.note.value.strip()[:1024], inline=False)
                await ch.send(embed=e)

class SantaWishOpenView(discord.ui.View):
    def __init__(self, botref: commands.Bot):
        super().__init__(timeout=120)
        self.botref = botref

    @discord.ui.button(label="Open Santa Wish Form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SantaWishModal(self.botref))

# =========================
# UI: Admin Panel (buttons)
# =========================

def is_staff(member: discord.Member) -> bool:
    return bool(member.guild_permissions.administrator or member.guild_permissions.manage_guild)

class SantaAdminPanel(discord.ui.View):
    def __init__(self, botref: commands.Bot):
        super().__init__(timeout=None)
        self.botref = botref

    @discord.ui.button(label="Post Today's Poll", style=discord.ButtonStyle.success, custom_id="santa_post_poll")
    async def post_poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Nah mate. Staff only.", ephemeral=True)
            return

        dk = day_key_qatar()

        # Validate channels
        poll_ch = self.botref.get_channel(PUBLIC_POLL_CHANNEL_ID) if PUBLIC_POLL_CHANNEL_ID else None
        if not poll_ch:
            await interaction.response.send_message("Poll channel not set. Add SANTA_POLL_CHANNEL_ID.", ephemeral=True)
            return

        # Ensure no active poll today
        con = db()
        cur = con.cursor()
        cur.execute("SELECT id, is_closed FROM santa_polls WHERE day_key = ? ORDER BY id DESC LIMIT 1", (dk,))
        row = cur.fetchone()
        if row and row[1] == 0:
            con.close()
            await interaction.response.send_message("Today’s poll is already live.", ephemeral=True)
            return

        # Get today's pending wishes
        cur.execute("""
            SELECT id, imvu_name, wish_text
            FROM santa_wishes
            WHERE day_key = ? AND status = 'pending'
            ORDER BY id DESC
        """, (dk,))
        wishes = cur.fetchall()

        if len(wishes) < 2:
            con.close()
            await interaction.response.send_message("Not enough wishes to run a poll (need at least 2).", ephemeral=True)
            return

        # Shortlist (random sample up to SHORTLIST_SIZE)
        sample = wishes[:] if len(wishes) <= SHORTLIST_SIZE else random.sample(wishes, SHORTLIST_SIZE)
        sample = sample[:len(POLL_EMOJIS)]  # hard cap A-F

        # Build anonymised cases using OpenAI (short blurbs)
        blurbs: List[str] = []
        for (wid, imvu, wtxt) in sample:
            blurb = santa_says(
                f"Wish text: {wtxt}",
                context_hint="Rewrite this wish into a funny anonymised 'case' line for a poll. One short sentence. No names. Modern British slang. No emojis."
            )
            # Ensure it's one-liner
            blurb = blurb.replace("\n", " ").strip()
            blurbs.append(blurb)

        # Create poll message
        lines = []
        for idx, (wid, imvu, wtxt) in enumerate(sample):
            letter = chr(ord("A") + idx)
            emoji = POLL_EMOJIS[idx]
            lines.append(f"{emoji} **Case {letter}:** {blurbs[idx]}")

        poll_text = (
            f"**Santa’s Court — {dk}**\n"
            f"Vote for the case you rate. One vote. No campaigning.\n\n"
            + "\n".join(lines)
            + "\n\nVoting ends when Santa closes it."
        )

        poll_msg = await poll_ch.send(poll_text)

        # Add reactions
        for idx in range(len(sample)):
            try:
                await poll_msg.add_reaction(POLL_EMOJIS[idx])
            except Exception:
                pass

        # Store poll + options
        cur.execute("""
            INSERT INTO santa_polls (day_key, poll_message_id, poll_channel_id, created_at, is_closed)
            VALUES (?, ?, ?, ?, 0)
        """, (dk, str(poll_msg.id), str(poll_ch.id), now_utc_iso()))
        poll_id = cur.lastrowid

        for idx, (wid, imvu, wtxt) in enumerate(sample):
            letter = chr(ord("A") + idx)
            emoji = POLL_EMOJIS[idx]
            cur.execute("""
                INSERT INTO santa_poll_options (poll_id, letter, emoji, wish_id, blurb)
                VALUES (?, ?, ?, ?, ?)
            """, (poll_id, letter, emoji, int(wid), blurbs[idx]))

        con.commit()
        con.close()

        await interaction.response.send_message("Poll posted.", ephemeral=True)

    @discord.ui.button(label="Close Poll (Show Votes)", style=discord.ButtonStyle.primary, custom_id="santa_close_poll")
    async def close_poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Nah mate. Staff only.", ephemeral=True)
            return

        dk = day_key_qatar()

        poll_ch = self.botref.get_channel(PUBLIC_POLL_CHANNEL_ID) if PUBLIC_POLL_CHANNEL_ID else None
        if not poll_ch:
            await interaction.response.send_message("Poll channel not set.", ephemeral=True)
            return

        con = db()
        cur = con.cursor()

        cur.execute("SELECT id, poll_message_id, poll_channel_id, is_closed FROM santa_polls WHERE day_key = ? ORDER BY id DESC LIMIT 1", (dk,))
        poll_row = cur.fetchone()
        if not poll_row:
            con.close()
            await interaction.response.send_message("No poll found for today.", ephemeral=True)
            return

        poll_id, poll_msg_id, poll_channel_id, is_closed = poll_row
        if is_closed:
            con.close()
            await interaction.response.send_message("Today’s poll is already closed.", ephemeral=True)
            return

        # Fetch poll message
        try:
            msg = await poll_ch.fetch_message(int(poll_msg_id))
        except Exception:
            con.close()
            await interaction.response.send_message("Couldn’t fetch the poll message.", ephemeral=True)
            return

        # Load options
        cur.execute("""
            SELECT letter, emoji, wish_id, blurb
            FROM santa_poll_options
            WHERE poll_id = ?
            ORDER BY letter ASC
        """, (poll_id,))
        opts = cur.fetchall()

        # Count votes per emoji (exclude bot's own reaction)
        vote_counts: Dict[str, int] = {emoji: 0 for (_, emoji, _, _) in opts}
        for reaction in msg.reactions:
            if str(reaction.emoji) in vote_counts:
                # reaction.count includes the bot’s reaction, subtract 1 safely
                c = max(0, reaction.count - 1)
                vote_counts[str(reaction.emoji)] = c

        # Mark closed
        cur.execute("UPDATE santa_polls SET is_closed = 1 WHERE id = ?", (poll_id,))
        con.commit()

        # Build admin review list with pick buttons
        admin_ch = self.botref.get_channel(ADMIN_CHANNEL_ID) if ADMIN_CHANNEL_ID else None
        if not admin_ch:
            con.close()
            await interaction.response.send_message("Admin channel not set.", ephemeral=True)
            return

        # Create daily row if missing
        cur.execute("INSERT OR IGNORE INTO santa_daily (day_key, created_at) VALUES (?, ?)", (dk, now_utc_iso()))
        con.commit()

        header = santa_says(
            "Poll just closed. You are summarising results for staff.",
            context_hint="Write a short energetic line as Santa for the staff review message. One sentence. No emojis."
        )
        await admin_ch.send(f"**Santa Review — {dk}**\n{header}")

        for (letter, emoji, wish_id, blurb) in opts:
            # Pull wish info
            cur.execute("SELECT imvu_name, wish_text FROM santa_wishes WHERE id = ?", (wish_id,))
            w = cur.fetchone()
            imvu = w[0] if w else "Unknown"
            wtxt = w[1] if w else ""

            votes = vote_counts.get(emoji, 0)
            e = discord.Embed(
                title=f"Case {letter} ({emoji}) — Votes: {votes}",
                description=blurb[:2048]
            )
            e.add_field(name="IMVU", value=imvu, inline=True)
            e.add_field(name="Wish", value=(wtxt[:900] + "…") if len(wtxt) > 900 else wtxt, inline=False)

            await admin_ch.send(embed=e, view=SantaPickWinnerView(self.botref, int(wish_id), dk))

        con.close()
        await interaction.response.send_message("Poll closed. Review list posted to admin channel.", ephemeral=True)

class SantaPickWinnerView(discord.ui.View):
    """
    Per-case buttons in admin channel:
    - Pick as Winner #1
    - Pick as Winner #2
    Once both are set, Santa announces publicly.
    """
    def __init__(self, botref: commands.Bot, wish_id: int, dk: str):
        super().__init__(timeout=None)
        self.botref = botref
        self.wish_id = wish_id
        self.dk = dk

    async def _pick(self, interaction: discord.Interaction, slot: int):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Nah mate. Staff only.", ephemeral=True)
            return

        con = db()
        cur = con.cursor()

        cur.execute("SELECT winner1_wish_id, winner2_wish_id, announced FROM santa_daily WHERE day_key = ?", (self.dk,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO santa_daily (day_key, created_at) VALUES (?, ?)", (self.dk, now_utc_iso()))
            con.commit()
            row = (None, None, 0)

        w1, w2, announced = row
        if announced:
            con.close()
            await interaction.response.send_message("Already announced for today.", ephemeral=True)
            return

        if self.wish_id in (w1, w2):
            con.close()
            await interaction.response.send_message("That case is already selected.", ephemeral=True)
            return

        if slot == 1 and w1 is not None:
            con.close()
            await interaction.response.send_message("Winner #1 is already set.", ephemeral=True)
            return
        if slot == 2 and w2 is not None:
            con.close()
            await interaction.response.send_message("Winner #2 is already set.", ephemeral=True)
            return

        if slot == 1:
            cur.execute("UPDATE santa_daily SET winner1_wish_id = ? WHERE day_key = ?", (self.wish_id, self.dk))
        else:
            cur.execute("UPDATE santa_daily SET winner2_wish_id = ? WHERE day_key = ?", (self.wish_id, self.dk))

        # mark picked
        cur.execute("UPDATE santa_wishes SET status = 'picked' WHERE id = ?", (self.wish_id,))
        con.commit()

        # re-check
        cur.execute("SELECT winner1_wish_id, winner2_wish_id FROM santa_daily WHERE day_key = ?", (self.dk,))
        w1, w2 = cur.fetchone()

        # if both winners set -> announce
        if w1 and w2:
            # pull winner details
            cur.execute("""
                SELECT id, imvu_name, wish_text
                FROM santa_wishes
                WHERE id IN (?, ?)
            """, (w1, w2))
            winners = cur.fetchall()

            # mark announced
            cur.execute("UPDATE santa_daily SET announced = 1 WHERE day_key = ?", (self.dk,))
            con.commit()
            con.close()

            announce_ch = self.botref.get_channel(PUBLIC_ANNOUNCE_CHANNEL_ID) if PUBLIC_ANNOUNCE_CHANNEL_ID else None
            if announce_ch:
                # Santa announcement (OpenAI)
                # Option 3: "5k creds OR portion of wish" is Santa’s discretion; announcement stays high-level.
                w_lines = []
                for wid, imvu, wtxt in winners:
                    w_lines.append(f"**{imvu}** — **{DAILY_PRIZE_CREDITS:,} credits** or a portion of the wish (Santa’s call).")

                intro = santa_says(
                    "Announce today's two winners. Energetic modern British slang, confident. 1–2 sentences.",
                    context_hint="Write the announcement intro as Santa. No emojis."
                )
                outro = santa_says(
                    "End the announcement with a playful shut-down line.",
                    context_hint="Write a short outro line as Santa. One sentence. No emojis."
                )
                msg = (
                    f"**Santa’s Desk — {self.dk}**\n"
                    f"{intro}\n\n"
                    + "\n".join(w_lines)
                    + f"\n\n{outro}"
                )
                await announce_ch.send(msg)

            await interaction.response.send_message("Both winners set. Santa announced.", ephemeral=True)

            # disable buttons on THIS message to avoid double-picks
            for item in self.children:
                item.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

        else:
            con.close()
            await interaction.response.send_message(f"Winner #{slot} set. Pick the other winner to trigger the announcement.", ephemeral=True)

            # disable the clicked button on this case
            if slot == 1:
                self.children[0].disabled = True
            else:
                self.children[1].disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Pick Winner #1", style=discord.ButtonStyle.success)
    async def pick1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, slot=1)

    @discord.ui.button(label="Pick Winner #2", style=discord.ButtonStyle.primary)
    async def pick2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, slot=2)

# =========================
# Events
# =========================

@bot.event
async def on_ready():
    init_db()
    # Persistent admin panel view (so buttons work after restart)
    bot.add_view(SantaAdminPanel(bot))
    print(f"Santa logged in as {bot.user}.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    content_l = content.lower()

    # Admin panel trigger (NO slash command)
    # Admin types "santa panel" in admin channel
    if ADMIN_CHANNEL_ID and message.channel.id == ADMIN_CHANNEL_ID and content_l == "santa panel":
        if isinstance(message.author, discord.Member) and is_staff(message.author):
            await message.reply(
                "Santa Panel is live. Post the poll when you’re ready.",
                view=SantaAdminPanel(bot),
                mention_author=False
            )
        else:
            await message.reply("Nice try. Staff only.", mention_author=False)
        return

    # Wish trigger in wish channel (recommended to restrict)
    if WISH_CHANNEL_ID and message.channel.id != WISH_CHANNEL_ID:
        return

    # Trigger phrase match (exact or startswith)
    if content_l in TRIGGERS or content_l.startswith("wish to santa"):
        # Reply with a button (Discord requires an interaction to open modal)
        tease = santa_says(
            "They want to submit a wish. Prompt them to click the button.",
            context_hint="Tell them to click the button to submit their wish. One short energetic sentence. No emojis."
        )
        await message.reply(tease, view=SantaWishOpenView(bot), mention_author=False)
        return

# =========================
# Run
# =========================

bot.run(DISCORD_TOKEN)
