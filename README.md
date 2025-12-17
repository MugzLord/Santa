# 🎅 Santa — Discord Wish & Poll Bot

Santa is a modern British, cheeky, OpenAI-powered Discord bot.
Players submit wishes via modal.
Santa runs a daily poll.
Admins select winners.
Santa announces results with banter.

## Features
- No slash commands
- Modal-based wish submissions
- OpenAI-generated Santa personality
- Daily anonymised poll
- Vote-influenced winner selection
- 2 daily winners (5k credits or portion of wish)
- Admin-only control panel (buttons)

## Setup
1. Create a Discord bot (enable Message Content Intent)
2. Invite bot with:
   - bot
   - applications.commands
3. Set environment variables (see .env.example)
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
