"""
León Coach League — Discord Bot v10
Vainglory ranked + scrims tracker
Claude Vision + Google Sheets
Bilingual EN/ES | Admin swap + delete buttons (permanent, silent)
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
    for ts, te, tn, _ in TIERS:
        if ts <= elo <= te:
            sub_size = (te - ts + 1) / 3
            offset = elo - ts
            if offset < sub_size: sub = "Bronze"
            elif offset < sub_size * 2: sub = "Silver"
            else: sub = "Gold"
            return f"T{tn} {sub}"
    return "T7 Bronze" if elo < 1680 else "T10 Gold"

def calc_elo(winner_elo, loser_elo):
    expected_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    return round(K_FACTOR * (1 - expected_w)), round(K_FACTOR * expected_w)

def get_tier_code(elo):
    for ts, _, _, code in TIERS:
        if elo >= ts: return code
    return "1600"

def compress_image(image_bytes, max_size_mb=4.5):
    max_bytes = int(max_size_mb * 1024 * 1024)
    if len(image_bytes) <= max_bytes:
        return image_bytes, "image/png"
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA": img = img.convert("RGB")
    if max(img.size) > 2000:
        r = 2000 / max(img.size)
        img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
    for q in [85, 70, 55, 40]:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q)
        if len(buf.getvalue()) <= max_bytes:
            return buf.getvalue(), "image/jpeg"
    img = img.resize((int(img.width * 0.5), int(img.height * 0.5)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=50)
    return buf.getvalue(), "image/jpeg"

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
            return i, {"name": row[0], "elo": int(row[1]) if row[1] else STARTING_ELO,
                "rank": row[2], "wins": int(row[3]) if row[3] else 0,
                "losses": int(row[4]) if row[4] else 0, "streak": int(row[5]) if row[5] else 0,
                "last_rival": row[6], "last_match": row[7]}
    return None

def create_player(name):
    ws_players.append_row([name, STARTING_ELO, get_rank(STARTING_ELO), 0, 0, 0, "", ""])

def update_player(row_idx, data):
    ws_players.update(f"A{row_idx}:H{row_idx}", [[data["name"], data["elo"], data["rank"],
        data["wins"], data["losses"], data["streak"], data["last_rival"], data["last_match"]]])

def get_scrim_player(name):
    records = ws_scrim_players.get_all_values()
    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == name.lower():
            return i, {"name": row[0], "wins": int(row[1]) if row[1] else 0,
                "losses": int(row[2]) if row[2] else 0, "winrate": row[3],
                "streak": int(row[4]) if row[4] else 0, "last_match": row[5]}
    return None

def create_scrim_player(name):
    ws_scrim_players.append_row([name, 0, 0, "0%", 0, ""])

def update_scrim_player(row_idx, data):
    total = data["wins"] + data["losses"]
    wr = f"{(data['wins']/total*100):.0f}%" if total > 0 else "0%"
    ws_scrim_players.update(f"A{row_idx}:F{row_idx}", [[data["name"], data["wins"],
        data["losses"], wr, data["streak"], data["last_match"]]])

def update_h2h_sheet(ws, p1_name, p2_name, winner_name):
    records = ws.get_all_values()
    p1, p2 = sorted([p1_name.lower(), p2_name.lower()])
    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == p1 and row[1].lower() == p2:
            w1 = int(row[2]) if row[2] else 0
            w2 = int(row[3]) if row[3] else 0
            if winner_name.lower() == p1: w1 += 1
            else: w2 += 1
            ws.update(f"C{i}:D{i}", [[w1, w2]])
            return
    w1 = 1 if winner_name.lower() == p1 else 0
    w2 = 1 if winner_name.lower() == p2 else 0
    ws.append_row([p1, p2, w1, w2])

def revert_h2h_sheet(ws, p1_name, p2_name, winner_name):
    records = ws.get_all_values()
    p1, p2 = sorted([p1_name.lower(), p2_name.lower()])
    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == p1 and row[1].lower() == p2:
            w1 = int(row[2]) if row[2] else 0
            w2 = int(row[3]) if row[3] else 0
            if winner_name.lower() == p1: w1 = max(0, w1 - 1)
            else: w2 = max(0, w2 - 1)
            ws.update(f"C{i}:D{i}", [[w1, w2]])
            return

def get_h2h_record(ws, player1, player2):
    records = ws.get_all_values()
    p1, p2 = sorted([player1.lower(), player2.lower()])
    for row in records[1:]:
        if row[0].lower() == p1 and row[1].lower() == p2:
            return int(row[2]) if row[2] else 0, int(row[3]) if row[3] else 0
    return 0, 0

def log_ranked(raw_w, raw_l, elo_changes, afk_players, url):
    ws_ranked_log.append_row([datetime.now().strftime("%Y-%m-%d %H:%M"), ", ".join(raw_w),
        ", ".join(raw_l), json.dumps(elo_changes),
        ", ".join(afk_players) if afk_players else "No", url])

def log_scrim(raw_w, raw_l, afk_players, url):
    ws_scrim_log.append_row([datetime.now().strftime("%Y-%m-%d %H:%M"), ", ".join(raw_w),
        ", ".join(raw_l), ", ".join(afk_players) if afk_players else "No", url])

def get_top_ranked(n=10):
    records = ws_players.get_all_values()
    players = []
    for row in records[1:]:
        if row[0] and row[1]:
            try: players.append({"name": row[0], "elo": int(row[1]), "rank": row[2],
                    "wins": int(row[3]) if row[3] else 0, "losses": int(row[4]) if row[4] else 0})
            except ValueError: continue
    players.sort(key=lambda x: x["elo"], reverse=True)
    return players[:n]

def get_top_scrims(n=10):
    records = ws_scrim_players.get_all_values()
    players = []
    for row in records[1:]:
        if row[0]:
            try: players.append({"name": row[0], "wins": int(row[1]) if row[1] else 0,
                    "losses": int(row[2]) if row[2] else 0, "winrate": row[3] if row[3] else "0%"})
            except ValueError: continue
    players.sort(key=lambda x: x["wins"], reverse=True)
    return players[:n]

# ─── CLAUDE VISION ────────────────────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

VISION_PROMPT = """Analyze this Vainglory match result screenshot. Two teams: LEFT (3 players on left) and RIGHT (3 players on right).

STEP 1 — Determine winner:
Center of screen shows a word between two kill counts.
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
        response = await asyncio.to_thread(claude_client.messages.create,
            model="claude-sonnet-4-20250514", max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": VISION_PROMPT}]}])
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
            text = text.strip()
        data = json.loads(text)
        if "error" in data: return data
        winner_side = data.get("winner", "left")
        left = data.get("left_team", [])
        right = data.get("right_team", [])
        center = data.get("center_word", "").lower()
        if "surr" in center or "rend" in center:
            lk, rk = data.get("left_kills", 0), data.get("right_kills", 0)
            if lk > rk: winner_side = "left"
            elif rk > lk: winner_side = "right"
        w = left if winner_side == "left" else right
        l = right if winner_side == "left" else left
        return {"winner_team": w, "loser_team": l,
                "afk_players": data.get("afk_players", []), "has_guests": data.get("has_guests", False)}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

# ─── PROCESS RANKED ──────────────────────────────────────────────────────────

async def process_ranked(winner_team, loser_team, afk_players, capture_url):
    afk_set = {clean_name(p).lower() for p in afk_players}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    elo_changes = {}
    raw_w = [p for p in winner_team if "guest" not in p.lower()]
    raw_l = [p for p in loser_team if "guest" not in p.lower()]
    cw = [clean_name(p) for p in raw_w]
    cl = [clean_name(p) for p in raw_l]
    if not cw or not cl:
        return None, "No valid players / Sin jugadores válidos."
    pd = {}
    for name in cw + cl:
        result = await asyncio.to_thread(get_player, name)
        if not result:
            await asyncio.to_thread(create_player, name)
            result = await asyncio.to_thread(get_player, name)
        pd[name] = result
    avg_w = sum(pd[p][1]["elo"] for p in cw) / len(cw)
    avg_l = sum(pd[p][1]["elo"] for p in cl) / len(cl)
    eg, el = calc_elo(avg_w, avg_l)
    for name in cw:
        idx, data = pd[name]
        old = data["elo"]
        data["elo"] = min(MAX_ELO, old + eg)
        data["rank"] = get_rank(data["elo"])
        data["wins"] += 1
        data["streak"] = max(1, data["streak"] + 1) if data["streak"] >= 0 else 1
        data["last_match"] = now
        elo_changes[name] = {"old": old, "new": data["elo"], "diff": data["elo"] - old}
        await asyncio.to_thread(update_player, idx, data)
    for name in cl:
        idx, data = pd[name]
        old = data["elo"]
        if name.lower() in afk_set:
            elo_changes[name] = {"old": old, "new": old, "diff": 0, "afk": True}
        else:
            data["elo"] = max(MIN_ELO, old - el)
            data["rank"] = get_rank(data["elo"])
            data["losses"] += 1
            data["streak"] = min(-1, data["streak"] - 1) if data["streak"] <= 0 else -1
            data["last_match"] = now
            elo_changes[name] = {"old": old, "new": data["elo"], "diff": data["elo"] - old}
            await asyncio.to_thread(update_player, idx, data)
    for w in cw:
        for l in cl:
            if l.lower() not in afk_set:
                await asyncio.to_thread(update_h2h_sheet, ws_h2h, w, l, w)
    await asyncio.to_thread(log_ranked, raw_w, raw_l, elo_changes, afk_players, capture_url)
    return elo_changes, None

async def revert_ranked(clean_winners, clean_losers, afk_players):
    afk_set = {p.lower() for p in afk_players}
    pd = {}
    for name in clean_winners + clean_losers:
        result = await asyncio.to_thread(get_player, name)
        if result: pd[name] = result
    if not pd: return
    avg_w = sum(pd[p][1]["elo"] for p in clean_winners if p in pd) / max(len([p for p in clean_winners if p in pd]), 1)
    avg_l = sum(pd[p][1]["elo"] for p in clean_losers if p in pd) / max(len([p for p in clean_losers if p in pd]), 1)
    eg, el = calc_elo(avg_w, avg_l)
    for name in clean_winners:
        if name not in pd: continue
        idx, data = pd[name]
        data["elo"] = max(MIN_ELO, data["elo"] - eg)
        data["rank"] = get_rank(data["elo"])
        data["wins"] = max(0, data["wins"] - 1)
        data["streak"] = 0
        await asyncio.to_thread(update_player, idx, data)
    for name in clean_losers:
        if name not in pd or name.lower() in afk_set: continue
        idx, data = pd[name]
        data["elo"] = min(MAX_ELO, data["elo"] + el)
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
    raw_w = [p for p in winner_team if "guest" not in p.lower()]
    raw_l = [p for p in loser_team if "guest" not in p.lower()]
    cw = [clean_name(p) for p in raw_w]
    cl = [clean_name(p) for p in raw_l]
    for name in cw:
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result:
            await asyncio.to_thread(create_scrim_player, name)
            result = await asyncio.to_thread(get_scrim_player, name)
        idx, data = result
        data["wins"] += 1
        data["streak"] = max(1, data["streak"] + 1) if data["streak"] >= 0 else 1
        data["last_match"] = now
        await asyncio.to_thread(update_scrim_player, idx, data)
    for name in cl:
        if name.lower() in afk_set: continue
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result:
            await asyncio.to_thread(create_scrim_player, name)
            result = await asyncio.to_thread(get_scrim_player, name)
        idx, data = result
        data["losses"] += 1
        data["streak"] = min(-1, data["streak"] - 1) if data["streak"] <= 0 else -1
        data["last_match"] = now
        await asyncio.to_thread(update_scrim_player, idx, data)
    for w in cw:
        for l in cl:
            if l.lower() not in afk_set:
                await asyncio.to_thread(update_h2h_sheet, ws_scrim_h2h, w, l, w)
    await asyncio.to_thread(log_scrim, raw_w, raw_l, afk_players, capture_url)

async def revert_scrims(clean_winners, clean_losers, afk_players):
    afk_set = {p.lower() for p in afk_players}
    for name in clean_winners:
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result: continue
        idx, data = result
        data["wins"] = max(0, data["wins"] - 1)
        data["streak"] = 0
        await asyncio.to_thread(update_scrim_player, idx, data)
    for name in clean_losers:
        if name.lower() in afk_set: continue
        result = await asyncio.to_thread(get_scrim_player, name)
        if not result: continue
        idx, data = result
        data["losses"] = max(0, data["losses"] - 1)
        data["streak"] = 0
        await asyncio.to_thread(update_scrim_player, idx, data)
    for w in clean_winners:
        for l in clean_losers:
            if l.lower() not in afk_set:
                await asyncio.to_thread(revert_h2h_sheet, ws_scrim_h2h, w, l, w)

# ─── EMBEDS ───────────────────────────────────────────────────────────────────

def build_ranked_embed(winner_team, loser_team, elo_changes, capture_url, footer):
    embed = discord.Embed(title="🏆 Ranked match registered / Partida ranked registrada", color=0x00FF88)
    wl, ll = [], []
    for raw in winner_team:
        if "guest" in raw.lower(): continue
        n = clean_name(raw)
        ch = elo_changes.get(n, {})
        wl.append(f"**{n}**\n{ch.get('old',0)} → {ch.get('new',0)} (+{ch.get('diff',0)}) | {get_rank(ch.get('new', STARTING_ELO))}")
    for raw in loser_team:
        if "guest" in raw.lower(): continue
        n = clean_name(raw)
        ch = elo_changes.get(n, {})
        if ch.get("afk"): ll.append(f"**{n}** ⚠️ AFK\n{ch.get('old',0)} (no change / sin cambio)")
        else: ll.append(f"**{n}**\n{ch.get('old',0)} → {ch.get('new',0)} ({ch.get('diff',0)}) | {get_rank(ch.get('new', STARTING_ELO))}")
    embed.add_field(name="👑 Winners / Ganadores", value="\n\n".join(wl) if wl else "—", inline=True)
    embed.add_field(name="💀 Losers / Perdedores", value="\n\n".join(ll) if ll else "—", inline=True)
    guests = [p for p in winner_team + loser_team if "guest" in p.lower()]
    if guests: embed.add_field(name="👤 Guests", value=", ".join(guests), inline=False)
    embed.set_thumbnail(url=capture_url)
    embed.set_footer(text=footer)
    return embed

def build_scrim_embed(winner_team, loser_team, afk_players, capture_url, footer):
    embed = discord.Embed(title="⚔️ Scrim registered / Scrim registrado", color=0xFFD700)
    wn = "\n".join([f"**{clean_name(p)}**" for p in winner_team if "guest" not in p.lower()])
    ln = "\n".join([f"**{clean_name(p)}**" for p in loser_team if "guest" not in p.lower()])
    embed.add_field(name="🏆 Winners / Ganadores", value=wn or "—", inline=True)
    embed.add_field(name="💀 Losers / Perdedores", value=ln or "—", inline=True)
    if afk_players: embed.add_field(name="⚠️ AFK", value=", ".join([clean_name(p) for p in afk_players]), inline=False)
    embed.set_thumbnail(url=capture_url)
    embed.set_footer(text=footer)
    return embed

# ─── MATCH BUTTONS ────────────────────────────────────────────────────────────

class MatchView(View):
    def __init__(self, winner_team, loser_team, afk_players, capture_url, mode, submitter, elo_changes=None):
        super().__init__(timeout=None)
        self.winner_team = winner_team
        self.loser_team = loser_team
        self.afk_players = afk_players
        self.capture_url = capture_url
        self.mode = mode
        self.submitter = submitter
        self.elo_changes = elo_changes or {}
        self.acted = False

    def get_clean(self):
        cw = [clean_name(p) for p in self.winner_team if "guest" not in p.lower()]
        cl = [clean_name(p) for p in self.loser_team if "guest" not in p.lower()]
        ca = [clean_name(p) for p in self.afk_players]
        return cw, cl, ca

    @discord.ui.button(label="🔄 Swap", style=discord.ButtonStyle.secondary)
    async def swap_btn(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only / Solo admins.", ephemeral=True)
            return
        if self.acted:
            await interaction.response.send_message("⚠️ Already modified / Ya modificado.", ephemeral=True)
            return
        self.acted = True
        for c in self.children: c.disabled = True
        await interaction.response.defer()

        cw, cl, ca = self.get_clean()
        if self.mode == "ranked": await revert_ranked(cw, cl, ca)
        else: await revert_scrims(cw, cl, ca)

        self.winner_team, self.loser_team = self.loser_team, self.winner_team

        if self.mode == "ranked":
            self.elo_changes, _ = await process_ranked(self.winner_team, self.loser_team, self.afk_players, self.capture_url)
            embed = build_ranked_embed(self.winner_team, self.loser_team, self.elo_changes,
                self.capture_url, f"By / Por {self.submitter}")
        else:
            await process_scrims(self.winner_team, self.loser_team, self.afk_players, self.capture_url)
            embed = build_scrim_embed(self.winner_team, self.loser_team, self.afk_players,
                self.capture_url, f"By / Por {self.submitter}")
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only / Solo admins.", ephemeral=True)
            return
        if self.acted:
            await interaction.response.send_message("⚠️ Already modified / Ya modificado.", ephemeral=True)
            return
        self.acted = True
        for c in self.children: c.disabled = True
        await interaction.response.defer()

        cw, cl, ca = self.get_clean()
        if self.mode == "ranked": await revert_ranked(cw, cl, ca)
        else: await revert_scrims(cw, cl, ca)

        embed = discord.Embed(title="🗑️ Match removed / Partida eliminada", color=0x666666)
        embed.set_thumbnail(url=self.capture_url)
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
    if message.author.bot: return
    ch_name = message.channel.name
    if ch_name not in [RANKED_CHANNEL, SCRIMS_CHANNEL]:
        await bot.process_commands(message)
        return
    img_att = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            img_att = att
            break
    if not img_att:
        await bot.process_commands(message)
        return

    mode = "ranked" if ch_name == RANKED_CHANNEL else "scrim"
    submitter = message.author.display_name
    proc = await message.reply("🔍 Analyzing / Analizando...")

    try:
        img_bytes = await img_att.read()
        result = await analyze_screenshot(img_bytes)
        if "error" in result:
            await proc.edit(content=f"❌ {result['error']}")
            return

        wt = result["winner_team"]
        lt = result["loser_team"]
        afk = result.get("afk_players", [])
        guests = result.get("has_guests", False)

        if mode == "scrim":
            if guests:
                await proc.edit(content="❌ Invalid scrim: no Guests / Sin Guests.")
                return
            await process_scrims(wt, lt, afk, img_att.url)
            embed = build_scrim_embed(wt, lt, afk, img_att.url, f"By / Por {submitter}")
            view = MatchView(wt, lt, afk, img_att.url, mode, submitter)
            await proc.edit(content=None, embed=embed, view=view)
            return

        elo_changes, error = await process_ranked(wt, lt, afk, img_att.url)
        if error:
            await proc.edit(content=f"❌ {error}")
            return
        embed = build_ranked_embed(wt, lt, elo_changes, img_att.url, f"By / Por {submitter}")
        view = MatchView(wt, lt, afk, img_att.url, mode, submitter, elo_changes)
        await proc.edit(content=None, embed=embed, view=view)

    except Exception as e:
        await proc.edit(content=f"❌ Error: {str(e)}")
    await bot.process_commands(message)

# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

@bot.tree.command(name="ranking", description="Top 10 ranked ELO")
async def ranking_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_ranked, 10)
    if not players:
        await interaction.response.send_message("No players yet / No hay jugadores aún.")
        return
    embed = discord.Embed(title="🏆 Top 10 — Ranked ELO", color=0xFFD700)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players):
        pfx = medals[i] if i < 3 else f"**{i+1}.**"
        lines.append(f"{pfx} **{p['name']}** — {p['elo']} ELO | {p['rank']} | {p['wins']}W-{p['losses']}L")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ranking_scrims", description="Top 10 scrims")
async def ranking_scrims_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_scrims, 10)
    if not players:
        await interaction.response.send_message("No scrim players yet / No hay jugadores de scrims aún.")
        return
    embed = discord.Embed(title="⚔️ Top 10 — Scrims", color=0xFF4444)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players):
        pfx = medals[i] if i < 3 else f"**{i+1}.**"
        lines.append(f"{pfx} **{p['name']}** — {p['wins']}W-{p['losses']}L ({p['winrate']})")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="perfil", description="Player profile / Perfil")
@app_commands.describe(jugador="Player name / Nombre")
async def perfil_cmd(interaction: discord.Interaction, jugador: str):
    result = await asyncio.to_thread(get_player, jugador)
    sr = await asyncio.to_thread(get_scrim_player, jugador)
    if not result and not sr:
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
    if sr:
        _, s = sr
        st = s["wins"] + s["losses"]
        swr = f"{(s['wins']/st*100):.1f}%" if st > 0 else "N/A"
        ss = f"🔥 {s['streak']}W" if s["streak"] > 0 else (f"❄️ {abs(s['streak'])}L" if s["streak"] < 0 else "—")
        embed.add_field(name="⚔️ Scrims", value=f"{s['wins']}W-{s['losses']}L ({swr})\nStreak: {ss}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="vs", description="Head-to-head / Enfrentamiento")
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

@bot.tree.command(name="anular", description="[Admin] Info about match controls")
async def anular_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only / Solo admins.", ephemeral=True)
        return
    await interaction.response.send_message("Use the buttons on each match result:\n🔄 **Swap** = invert winners/losers\n🗑️ **Delete** = remove match completely\n\nUsa los botones en cada resultado:\n🔄 **Swap** = invertir ganadores/perdedores\n🗑️ **Delete** = eliminar partida", ephemeral=True)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
