"""
León Coach League — Discord Bot v8
Vainglory ranked + scrims tracker
Claude Vision + Google Sheets
Bilingual EN/ES | Admin swap button
"""

import os
import io
import re
import json
import base64
import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
import gspread
from google.oauth2.service_account import Credentials
import anthropic
from PIL import Image

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
google_creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])

RANKED_CHANNEL = "ranked"
SCRIMS_CHANNEL = "scrims"

K_FACTOR = 32
STARTING_ELO = 1680
MIN_ELO = 0
MAX_ELO = 2800

TIERS = [
    (2400, 2800, 10, "1900"),
    (2160, 2399, 9, "1800"),
    (1920, 2159, 8, "1700"),
    (1680, 1919, 7, "1600"),
]


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def clean_name(raw_name):
    cleaned = re.sub(r'^\d+(-\d+)?_', '', raw_name)
    return cleaned if cleaned else raw_name


def get_rank(elo):
    elo = max(MIN_ELO, min(MAX_ELO, elo))
    for tier_start, tier_end, tier_num, _ in TIERS:
        if tier_start <= elo <= tier_end:
            sub_size = (tier_end - tier_start + 1) / 3
            offset = elo - tier_start
            if offset < sub_size:
                sub = "Bronze"
            elif offset < sub_size * 2:
                sub = "Silver"
            else:
                sub = "Gold"
            return f"T{tier_num} {sub}"
    if elo < 1680:
        return "T7 Bronze"
    return "T10 Gold"


def calc_elo(winner_elo, loser_elo):
    expected_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    elo_gain = round(K_FACTOR * (1 - expected_w))
    elo_loss = round(K_FACTOR * expected_w)
    return elo_gain, elo_loss


def get_tier_code(elo):
    for tier_start, _, _, code in TIERS:
        if elo >= tier_start:
            return code
    return "1600"


def compress_image(image_bytes, max_size_mb=4.5):
    max_bytes = int(max_size_mb * 1024 * 1024)
    if len(image_bytes) <= max_bytes:
        return image_bytes, "image/png"
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")
    if max(img.size) > 2000:
        ratio = 2000 / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    for quality in [85, 70, 55, 40]:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        if len(buffer.getvalue()) <= max_bytes:
            return buffer.getvalue(), "image/jpeg"
    img = img.resize((int(img.width * 0.5), int(img.height * 0.5)), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=50)
    return buffer.getvalue(), "image/jpeg"


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(google_creds_json, scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)

ws_players = spreadsheet.worksheet("Players")
ws_ranked_log = spreadsheet.worksheet("RankedLog")
ws_h2h = spreadsheet.worksheet("H2H")
ws_scrim_players = spreadsheet.worksheet("ScrimPlayers")
ws_scrim_log = spreadsheet.worksheet("ScrimLog")
ws_scrim_h2h = spreadsheet.worksheet("ScrimH2H")


def get_player(name):
    records = ws_players.get_all_values()
    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == name.lower():
            return i, {
                "name": row[0], "elo": int(row[1]) if row[1] else STARTING_ELO,
                "rank": row[2], "wins": int(row[3]) if row[3] else 0,
                "losses": int(row[4]) if row[4] else 0, "streak": int(row[5]) if row[5] else 0,
                "last_rival": row[6], "last_match": row[7],
            }
    return None


def create_player(name):
    ws_players.append_row([name, STARTING_ELO, get_rank(STARTING_ELO), 0, 0, 0, "", ""])


def update_player(row_idx, data):
    ws_players.update(f"A{row_idx}:H{row_idx}", [[
        data["name"], data["elo"], data["rank"], data["wins"],
        data["losses"], data["streak"], data["last_rival"], data["last_match"]
    ]])


def get_scrim_player(name):
    records = ws_scrim_players.get_all_values()
    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == name.lower():
            return i, {
                "name": row[0], "wins": int(row[1]) if row[1] else 0,
                "losses": int(row[2]) if row[2] else 0, "winrate": row[3],
                "streak": int(row[4]) if row[4] else 0, "last_match": row[5],
            }
    return None


def create_scrim_player(name):
    ws_scrim_players.append_row([name, 0, 0, "0%", 0, ""])


def update_scrim_player(row_idx, data):
    total = data["wins"] + data["losses"]
    winrate = f"{(data['wins']/total*100):.0f}%" if total > 0 else "0%"
    ws_scrim_players.update(f"A{row_idx}:F{row_idx}", [[
        data["name"], data["wins"], data["losses"], winrate, data["streak"], data["last_match"]
    ]])


def update_h2h_sheet(ws, player1, player2, winner_name):
    records = ws.get_all_values()
    p1, p2 = sorted([player1.lower(), player2.lower()])
    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == p1 and row[1].lower() == p2:
            w1 = int(row[2]) if row[2] else 0
            w2 = int(row[3]) if row[3] else 0
            if winner_name.lower() == p1:
                w1 += 1
            else:
                w2 += 1
            ws.update(f"C{i}:D{i}", [[w1, w2]])
            return
    w1 = 1 if winner_name.lower() == p1 else 0
    w2 = 1 if winner_name.lower() == p2 else 0
    ws.append_row([p1, p2, w1, w2])


def revert_h2h_sheet(ws, player1, player2, winner_name):
    records = ws.get_all_values()
    p1, p2 = sorted([player1.lower(), player2.lower()])
    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == p1 and row[1].lower() == p2:
            w1 = int(row[2]) if row[2] else 0
            w2 = int(row[3]) if row[3] else 0
            if winner_name.lower() == p1:
                w1 = max(0, w1 - 1)
            else:
                w2 = max(0, w2 - 1)
            ws.update(f"C{i}:D{i}", [[w1, w2]])
            return


def get_h2h_record(ws, player1, player2):
    records = ws.get_all_values()
    p1, p2 = sorted([player1.lower(), player2.lower()])
    for row in records[1:]:
        if row[0].lower() == p1 and row[1].lower() == p2:
            return int(row[2]) if row[2] else 0, int(row[3]) if row[3] else 0
    return 0, 0


def log_ranked(raw_winners, raw_losers, elo_changes, afk_players, capture_url):
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws_ranked_log.append_row([fecha, ", ".join(raw_winners), ", ".join(raw_losers),
        json.dumps(elo_changes), ", ".join(afk_players) if afk_players else "No", capture_url])


def log_scrim(raw_winners, raw_losers, afk_players, capture_url):
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws_scrim_log.append_row([fecha, ", ".join(raw_winners), ", ".join(raw_losers),
        ", ".join(afk_players) if afk_players else "No", capture_url])


def get_top_ranked(n=10):
    records = ws_players.get_all_values()
    players = []
    for row in records[1:]:
        if row[0] and row[1]:
            try:
                players.append({"name": row[0], "elo": int(row[1]), "rank": row[2],
                    "wins": int(row[3]) if row[3] else 0, "losses": int(row[4]) if row[4] else 0})
            except ValueError:
                continue
    players.sort(key=lambda x: x["elo"], reverse=True)
    return players[:n]


def get_top_scrims(n=10):
    records = ws_scrim_players.get_all_values()
    players = []
    for row in records[1:]:
        if row[0]:
            try:
                players.append({"name": row[0], "wins": int(row[1]) if row[1] else 0,
                    "losses": int(row[2]) if row[2] else 0, "winrate": row[3] if row[3] else "0%"})
            except ValueError:
                continue
    players.sort(key=lambda x: x["wins"], reverse=True)
    return players[:n]


# ─── CLAUDE VISION ────────────────────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

VISION_PROMPT = """Analyze this Vainglory match result screenshot. There are two teams: LEFT (3 players on the left) and RIGHT (3 players on the right).

STEP 1 — Determine the winner:
In the center there is a word between two kill counts.
- "Victory" / "Victoria" → LEFT team WON
- "Defeat" / "Derrota" → LEFT team LOST (right won)
- "Surrender" / "Rendición" → Use kill counts: team with MORE kills WON

STEP 2 — Read ALL 6 player names exactly as shown.

STEP 3 — Check for AFK (name crossed out, character faded).

Respond ONLY in JSON:
{
    "left_team": ["name1", "name2", "name3"],
    "right_team": ["name1", "name2", "name3"],
    "left_kills": 25,
    "right_kills": 5,
    "winner": "left" or "right",
    "center_word": "the word shown",
    "afk_players": [],
    "has_guests": false
}

Names must be EXACT including prefixes like "1600_", "1800-2_", "5656-1_".
Guest includes Guest_1234, Guest0, etc.
If unreadable: {"error": "Could not read / No pude leer"}
"""


async def analyze_screenshot(image_bytes):
    compressed_bytes, media_type = compress_image(image_bytes)
    b64 = base64.b64encode(compressed_bytes).decode("utf-8")
    try:
        response = await asyncio.to_thread(
            claude_client.messages.create, model="claude-sonnet-4-20250514", max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": VISION_PROMPT},
            ]}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        if "error" in data:
            return data

        winner_side = data.get("winner", "left")
        left_team = data.get("left_team", [])
        right_team = data.get("right_team", [])
        left_kills = data.get("left_kills", 0)
        right_kills = data.get("right_kills", 0)
        center = data.get("center_word", "").lower()

        if "surr" in center or "rend" in center:
            if left_kills > right_kills:
                winner_side = "left"
            elif right_kills > left_kills:
                winner_side = "right"

        if winner_side == "left":
            return {"winner_team": left_team, "loser_team": right_team,
                    "afk_players": data.get("afk_players", []), "has_guests": data.get("has_guests", False)}
        else:
            return {"winner_team": right_team, "loser_team": left_team,
                    "afk_players": data.get("afk_players", []), "has_guests": data.get("has_guests", False)}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}


# ─── PROCESS RANKED ──────────────────────────────────────────────────────────

async def process_ranked(winner_team, loser_team, afk_players, capture_url):
    afk_set = {clean_name(p).lower() for p in afk_players}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    elo_changes = {}

    raw_winners = [p for p in winner_team if "guest" not in p.lower()]
    raw_losers = [p for p in loser_team if "guest" not in p.lower()]
    clean_winners = [clean_name(p) for p in raw_winners]
    clean_losers = [clean_name(p) for p in raw_losers]

    if not clean_winners or not clean_losers:
        return None, "No valid players / No se encontraron jugadores válidos."

    player_data = {}
    for name in clean_winners + clean_losers:
        result = await asyncio.to_thread(get_player, name)
        if not result:
            await asyncio.to_thread(create_player, name)
            result = await asyncio.to_thread(get_player, name)
        player_data[name] = result

    avg_w = sum(player_data[p][1]["elo"] for p in clean_winners) / len(clean_winners)
    avg_l = sum(player_data[p][1]["elo"] for p in clean_losers) / len(clean_losers)
    elo_gain, elo_loss = calc_elo(avg_w, avg_l)

    for name in clean_winners:
        idx, data = player_data[name]
        old_elo = data["elo"]
        data["elo"] = min(MAX_ELO, old_elo + elo_gain)
        data["rank"] = get_rank(data["elo"])
        data["wins"] += 1
        data["streak"] = max(1, data["streak"] + 1) if data["streak"] >= 0 else 1
        data["last_match"] = now
        elo_changes[name] = {"old": old_elo, "new": data["elo"], "diff": data["elo"] - old_elo}
        await asyncio.to_thread(update_player, idx, data)

    for name in clean_losers:
        idx, data = player_data[name]
        old_elo = data["elo"]
        if name.lower() in afk_set:
            elo_changes[name] = {"old": old_elo, "new": old_elo, "diff": 0, "afk": True}
        else:
            data["elo"] = max(MIN_ELO, old_elo - elo_loss)
            data["rank"] = get_rank(data["elo"])
            data["losses"] += 1
            data["streak"] = min(-1, data["streak"] - 1) if data["streak"] <= 0 else -1
            data["last_match"] = now
            elo_changes[name] = {"old": old_elo, "new": data["elo"], "diff": data["elo"] - old_elo}
            await asyncio.to_thread(update_player, idx, data)

    for w in clean_winners:
        for l in clean_losers:
            if l.lower() not in afk_set:
                await asyncio.to_thread(update_h2h_sheet, ws_h2h, w, l, w)

    await asyncio.to_thread(log_ranked, raw_winners, raw_losers, elo_changes, afk_players, capture_url)
    return elo_changes, None


async def revert_ranked(clean_winners, clean_losers, afk_players):
    """Revert a ranked match — undo ELO, wins/losses, H2H."""
    afk_set = {p.lower() for p in afk_players}

    player_data = {}
    for name in clean_winners + clean_losers:
        result = await asyncio.to_thread(get_player, name)
        if result:
            player_data[name] = result

    if not player_data:
        return

    avg_w = sum(player_data[p][1]["elo"] for p in clean_winners if p in player_data) / max(len(clean_winners), 1)
    avg_l = sum(player_data[p][1]["elo"] for p in clean_losers if p in player_data) / max(len(clean_losers), 1)
    elo_gain, elo_loss = calc_elo(avg_w, avg_l)

    for name in clean_winners:
        if name not in player_data:
            continue
        idx, data = player_data[name]
        data["elo"] = max(MIN_ELO, data["elo"] - elo_gain)
        data["rank"] = get_rank(data["elo"])
        data["wins"] = max(0, data["wins"] - 1)
        data["streak"] = 0
        await asyncio.to_thread(update_player, idx, data)

    for name in clean_losers:
        if name not in player_data or name.lower() in afk_set:
            continue
        idx, data = player_data[name]
        data["elo"] = min(MAX_ELO, data["elo"] + elo_loss)
        data["rank"] = get_rank(data["elo"])
        data["losses"] = max(0, data["losses"] - 1)
        data["streak"] = 0
        await asyncio.to_thread(update_player, idx, data)

    for w in clean_winners:
        for l in clean_losers:
            if l.lower() not in afk_set:
                await asyncio.to_thread(revert_h2h_sheet, ws_h2h, w, l, w)


# ─── PROCESS SCRIMS ──────────────────────────────────────────────────────────

async def process_scrims(winner_team, loser_team, afk_players, capture_url):
    afk_set = {clean_name(p).lower() for p in afk_players}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    raw_winners = [p for p in winner_team if "guest" not in p.lower()]
    raw_losers = [p for p in loser_team if "guest" not in p.lower()]
    clean_winners = [clean_name(p) for p in raw_winners]
    clean_losers = [clean_name(p) for p in raw_losers]

    for name in clean_winners:
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result:
            await asyncio.to_thread(create_scrim_player, name)
            result = await asyncio.to_thread(get_scrim_player, name)
        idx, data = result
        data["wins"] += 1
        data["streak"] = max(1, data["streak"] + 1) if data["streak"] >= 0 else 1
        data["last_match"] = now
        await asyncio.to_thread(update_scrim_player, idx, data)

    for name in clean_losers:
        if name.lower() in afk_set:
            continue
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result:
            await asyncio.to_thread(create_scrim_player, name)
            result = await asyncio.to_thread(get_scrim_player, name)
        idx, data = result
        data["losses"] += 1
        data["streak"] = min(-1, data["streak"] - 1) if data["streak"] <= 0 else -1
        data["last_match"] = now
        await asyncio.to_thread(update_scrim_player, idx, data)

    for w in clean_winners:
        for l in clean_losers:
            if l.lower() not in afk_set:
                await asyncio.to_thread(update_h2h_sheet, ws_scrim_h2h, w, l, w)

    await asyncio.to_thread(log_scrim, raw_winners, raw_losers, afk_players, capture_url)


async def revert_scrims(clean_winners, clean_losers, afk_players):
    afk_set = {p.lower() for p in afk_players}

    for name in clean_winners:
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result:
            continue
        idx, data = result
        data["wins"] = max(0, data["wins"] - 1)
        data["streak"] = 0
        await asyncio.to_thread(update_scrim_player, idx, data)

    for name in clean_losers:
        if name.lower() in afk_set:
            continue
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result:
            continue
        idx, data = result
        data["losses"] = max(0, data["losses"] - 1)
        data["streak"] = 0
        await asyncio.to_thread(update_scrim_player, idx, data)

    for w in clean_winners:
        for l in clean_losers:
            if l.lower() not in afk_set:
                await asyncio.to_thread(revert_h2h_sheet, ws_scrim_h2h, w, l, w)


# ─── SWAP BUTTON ──────────────────────────────────────────────────────────────

class SwapView(View):
    def __init__(self, winner_team, loser_team, afk_players, capture_url, mode, submitter):
        super().__init__(timeout=300)
        self.winner_team = winner_team
        self.loser_team = loser_team
        self.afk_players = afk_players
        self.capture_url = capture_url
        self.mode = mode
        self.submitter = submitter
        self.swapped = False

    @discord.ui.button(label="🔄 Swap / Invertir", style=discord.ButtonStyle.secondary)
    async def swap_button(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only / Solo admins.", ephemeral=True)
            return

        if self.swapped:
            await interaction.response.send_message("⚠️ Already swapped / Ya se invirtió.", ephemeral=True)
            return

        self.swapped = True
        button.disabled = True
        button.label = "✅ Swapped / Invertido"

        await interaction.response.defer()

        # Get clean names
        clean_old_winners = [clean_name(p) for p in self.winner_team if "guest" not in p.lower()]
        clean_old_losers = [clean_name(p) for p in self.loser_team if "guest" not in p.lower()]
        clean_afk = [clean_name(p) for p in self.afk_players]

        # Revert original
        if self.mode == "ranked":
            await revert_ranked(clean_old_winners, clean_old_losers, clean_afk)
        else:
            await revert_scrims(clean_old_winners, clean_old_losers, clean_afk)

        # Swap teams
        self.winner_team, self.loser_team = self.loser_team, self.winner_team

        # Process with swapped teams
        if self.mode == "ranked":
            elo_changes, error = await process_ranked(self.winner_team, self.loser_team, self.afk_players, self.capture_url)
            if error:
                await interaction.followup.send(f"❌ {error}")
                return

            embed = discord.Embed(title="🏆 Ranked match registered (CORRECTED) / Partida ranked registrada (CORREGIDA)", color=0x00FF88)

            winner_lines = []
            for raw in self.winner_team:
                if "guest" in raw.lower():
                    continue
                name = clean_name(raw)
                ch = elo_changes.get(name, {})
                winner_lines.append(f"**{name}**\n{ch.get('old', 0)} → {ch.get('new', 0)} (+{ch.get('diff', 0)}) | {get_rank(ch.get('new', STARTING_ELO))}")

            loser_lines = []
            for raw in self.loser_team:
                if "guest" in raw.lower():
                    continue
                name = clean_name(raw)
                ch = elo_changes.get(name, {})
                if ch.get("afk"):
                    loser_lines.append(f"**{name}** ⚠️ AFK\n{ch.get('old', 0)} (no change / sin cambio)")
                else:
                    loser_lines.append(f"**{name}**\n{ch.get('old', 0)} → {ch.get('new', 0)} ({ch.get('diff', 0)}) | {get_rank(ch.get('new', STARTING_ELO))}")

            embed.add_field(name="👑 Winners / Ganadores", value="\n\n".join(winner_lines) if winner_lines else "—", inline=True)
            embed.add_field(name="💀 Losers / Perdedores", value="\n\n".join(loser_lines) if loser_lines else "—", inline=True)

        else:
            await process_scrims(self.winner_team, self.loser_team, self.afk_players, self.capture_url)
            embed = discord.Embed(title="⚔️ Scrim registered (CORRECTED) / Scrim registrado (CORREGIDO)", color=0xFFD700)
            w_names = "\n".join([f"**{clean_name(p)}**" for p in self.winner_team if "guest" not in p.lower()])
            l_names = "\n".join([f"**{clean_name(p)}**" for p in self.loser_team if "guest" not in p.lower()])
            embed.add_field(name="🏆 Winners / Ganadores", value=w_names or "—", inline=True)
            embed.add_field(name="💀 Losers / Perdedores", value=l_names or "—", inline=True)

        embed.set_thumbnail(url=self.capture_url)
        embed.set_footer(text=f"Corrected by / Corregido por {interaction.user.display_name}")
        await interaction.edit_original_response(embed=embed, view=self)


# ─── DISCORD BOT ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ {bot.user} online")
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} commands synced")
    except Exception as e:
        print(f"❌ Sync error: {e}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    channel_name = message.channel.name
    if channel_name not in [RANKED_CHANNEL, SCRIMS_CHANNEL]:
        await bot.process_commands(message)
        return

    image_attachment = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_attachment = att
            break
    if not image_attachment:
        await bot.process_commands(message)
        return

    mode = "ranked" if channel_name == RANKED_CHANNEL else "scrim"
    submitter = message.author.display_name
    processing_msg = await message.reply("🔍 Analyzing / Analizando...")

    try:
        img_bytes = await image_attachment.read()
        result = await analyze_screenshot(img_bytes)

        if "error" in result:
            await processing_msg.edit(content=f"❌ {result['error']}")
            return

        winner_team = result["winner_team"]
        loser_team = result["loser_team"]
        afk_players = result.get("afk_players", [])
        has_guests = result.get("has_guests", False)

        swap_view = SwapView(winner_team, loser_team, afk_players, image_attachment.url, mode, submitter)

        if mode == "scrim":
            if has_guests:
                await processing_msg.edit(content="❌ Invalid scrim: no Guests.\nScrim inválido: sin Guests.")
                return
            await process_scrims(winner_team, loser_team, afk_players, image_attachment.url)
            embed = discord.Embed(title="⚔️ Scrim registered / Scrim registrado", color=0xFFD700)
            w_names = "\n".join([f"**{clean_name(p)}**" for p in winner_team if "guest" not in p.lower()])
            l_names = "\n".join([f"**{clean_name(p)}**" for p in loser_team if "guest" not in p.lower()])
            embed.add_field(name="🏆 Winners / Ganadores", value=w_names or "—", inline=True)
            embed.add_field(name="💀 Losers / Perdedores", value=l_names or "—", inline=True)
            if afk_players:
                embed.add_field(name="⚠️ AFK", value=", ".join([clean_name(p) for p in afk_players]), inline=False)
            embed.set_footer(text=f"By / Por {submitter}")
            embed.set_thumbnail(url=image_attachment.url)
            await processing_msg.edit(content=None, embed=embed, view=swap_view)
            return

        elo_changes, error = await process_ranked(winner_team, loser_team, afk_players, image_attachment.url)
        if error:
            await processing_msg.edit(content=f"❌ {error}")
            return

        embed = discord.Embed(title="🏆 Ranked match registered / Partida ranked registrada", color=0x00FF88)

        winner_lines = []
        for raw in winner_team:
            if "guest" in raw.lower():
                continue
            name = clean_name(raw)
            ch = elo_changes.get(name, {})
            winner_lines.append(f"**{name}**\n{ch.get('old', 0)} → {ch.get('new', 0)} (+{ch.get('diff', 0)}) | {get_rank(ch.get('new', STARTING_ELO))}")

        loser_lines = []
        for raw in loser_team:
            if "guest" in raw.lower():
                continue
            name = clean_name(raw)
            ch = elo_changes.get(name, {})
            if ch.get("afk"):
                loser_lines.append(f"**{name}** ⚠️ AFK\n{ch.get('old', 0)} (no change / sin cambio)")
            else:
                loser_lines.append(f"**{name}**\n{ch.get('old', 0)} → {ch.get('new', 0)} ({ch.get('diff', 0)}) | {get_rank(ch.get('new', STARTING_ELO))}")

        embed.add_field(name="👑 Winners / Ganadores", value="\n\n".join(winner_lines) if winner_lines else "—", inline=True)
        embed.add_field(name="💀 Losers / Perdedores", value="\n\n".join(loser_lines) if loser_lines else "—", inline=True)

        guests = [p for p in winner_team + loser_team if "guest" in p.lower()]
        if guests:
            embed.add_field(name="👤 Guests (ignored / ignorados)", value=", ".join(guests), inline=False)

        embed.set_thumbnail(url=image_attachment.url)
        embed.set_footer(text=f"By / Por {submitter}")
        await processing_msg.edit(content=None, embed=embed, view=swap_view)

    except Exception as e:
        await processing_msg.edit(content=f"❌ Error: {str(e)}")

    await bot.process_commands(message)


# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

@bot.tree.command(name="ranking", description="Top 10 ranked players / Top 10 jugadores ranked")
async def ranking_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_ranked, 10)
    if not players:
        await interaction.response.send_message("No players yet / No hay jugadores aún.")
        return
    embed = discord.Embed(title="🏆 Top 10 — Ranked ELO", color=0xFFD700)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players):
        prefix = medals[i] if i < 3 else f"**{i+1}.**"
        lines.append(f"{prefix} **{p['name']}** — {p['elo']} ELO | {p['rank']} | {p['wins']}W-{p['losses']}L")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ranking_scrims", description="Top 10 scrim players / Top 10 jugadores scrims")
async def ranking_scrims_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_scrims, 10)
    if not players:
        await interaction.response.send_message("No scrim players yet / No hay jugadores de scrims aún.")
        return
    embed = discord.Embed(title="⚔️ Top 10 — Scrims", color=0xFF4444)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players):
        prefix = medals[i] if i < 3 else f"**{i+1}.**"
        lines.append(f"{prefix} **{p['name']}** — {p['wins']}W-{p['losses']}L ({p['winrate']})")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="perfil", description="Player profile / Perfil de jugador")
@app_commands.describe(jugador="Player name / Nombre del jugador")
async def perfil_cmd(interaction: discord.Interaction, jugador: str):
    result = await asyncio.to_thread(get_player, jugador)
    scrim_result = await asyncio.to_thread(get_scrim_player, jugador)
    if not result and not scrim_result:
        await interaction.response.send_message(f"❌ Not found / No encontré a **{jugador}**.")
        return
    embed = discord.Embed(title=f"📊 {jugador}", color=0x00BFFF)
    if result:
        _, d = result
        total = d["wins"] + d["losses"]
        wr = f"{(d['wins']/total*100):.1f}%" if total > 0 else "N/A"
        streak = f"🔥 {d['streak']}W" if d["streak"] > 0 else (f"❄️ {abs(d['streak'])}L" if d["streak"] < 0 else "—")
        elo = d["elo"]
        progress = 0
        for ts, te, tn, tc in TIERS:
            if ts <= elo <= te:
                progress = ((elo - ts) / (te - ts + 1)) * 100
                break
        bar = "█" * round(progress / 10) + "░" * (10 - round(progress / 10))
        embed.add_field(name="🎮 Ranked", value=f"**{elo}** ELO | {d['rank']}\n{d['wins']}W-{d['losses']}L ({wr})\nStreak: {streak}\n`{bar}` {progress:.0f}%", inline=False)
    if scrim_result:
        _, s = scrim_result
        st = s["wins"] + s["losses"]
        swr = f"{(s['wins']/st*100):.1f}%" if st > 0 else "N/A"
        sstreak = f"🔥 {s['streak']}W" if s["streak"] > 0 else (f"❄️ {abs(s['streak'])}L" if s["streak"] < 0 else "—")
        embed.add_field(name="⚔️ Scrims", value=f"{s['wins']}W-{s['losses']}L ({swr})\nStreak: {sstreak}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="vs", description="Head-to-head / Enfrentamiento directo")
@app_commands.describe(jugador1="Player 1", jugador2="Player 2")
async def vs_cmd(interaction: discord.Interaction, jugador1: str, jugador2: str):
    rw1, rw2 = await asyncio.to_thread(get_h2h_record, ws_h2h, jugador1, jugador2)
    sw1, sw2 = await asyncio.to_thread(get_h2h_record, ws_scrim_h2h, jugador1, jugador2)
    p1, p2 = sorted([jugador1.lower(), jugador2.lower()])
    if rw1 == 0 and rw2 == 0 and sw1 == 0 and sw2 == 0:
        await interaction.response.send_message(f"No matches between / Sin partidas entre **{jugador1}** y **{jugador2}**.")
        return
    embed = discord.Embed(title=f"⚔️ {p1} vs {p2}", color=0xFF6600)
    if rw1 > 0 or rw2 > 0:
        embed.add_field(name="🎮 Ranked", value=f"**{p1}**: {rw1}W\n**{p2}**: {rw2}W\n{rw1+rw2} matches", inline=True)
    if sw1 > 0 or sw2 > 0:
        embed.add_field(name="⚔️ Scrims", value=f"**{p1}**: {sw1}W\n**{p2}**: {sw2}W\n{sw1+sw2} matches", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="anular", description="[Admin] Revert / Anular partida")
@app_commands.describe(jugador="Player / Jugador", razon="Reason / Razón")
async def anular_cmd(interaction: discord.Interaction, jugador: str, razon: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only / Solo admins.", ephemeral=True)
        return
    await interaction.response.send_message(f"⚠️ Use the 🔄 Swap button on the match, or fix manually in Sheets.\nUsa el botón 🔄 Swap en la partida, o ajusta manualmente en Sheets.\nPlayer / Jugador: **{jugador}** | Reason / Razón: {razon}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
