# Leon Coach League - Discord Bot v11
# Vainglory ranked + scrims tracker
# Claude Vision + Google Sheets
# Bilingual EN/ES | Admin swap + delete buttons (permanent, silent)

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
    (2160, 2399, 9,  "1800"),
    (1920, 2159, 8,  "1700"),
    (1680, 1919, 7,  "1600"),
]

# ── utilidades ────────────────────────────────────────────────────────────────

def clean_name(raw):
    cleaned = re.sub(r'^\d+(-\d+)?_', '', raw)
    return cleaned if cleaned else raw

def get_rank(elo):
    elo = max(MIN_ELO, min(MAX_ELO, elo))
    for ts, te, tn, _ in TIERS:
        if ts <= elo <= te:
            size = (te - ts + 1) / 3
            offset = elo - ts
            sub = "Bronze" if offset < size else ("Silver" if offset < size * 2 else "Gold")
            return f"T{tn} {sub}"
    return "T7 Bronze" if elo < 1680 else "T10 Gold"

def calc_elo(winner_elo, loser_elo):
    exp = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    return round(K_FACTOR * (1 - exp)), round(K_FACTOR * exp)

def compress_image(image_bytes, max_mb=3.5):
    max_bytes = int(max_mb * 1024 * 1024)
    if len(image_bytes) <= max_bytes:
        fmt = Image.open(io.BytesIO(image_bytes)).format or "PNG"
        return image_bytes, "image/jpeg" if fmt.upper() == "JPEG" else "image/png"
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")
    if max(img.size) > 1800:
        r = 1800 / max(img.size)
        img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
    for q in [80, 65, 50, 35]:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q)
        if len(buf.getvalue()) <= max_bytes:
            return buf.getvalue(), "image/jpeg"
    img = img.resize((int(img.width * 0.4), int(img.height * 0.4)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=40)
    return buf.getvalue(), "image/jpeg"

def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

# ── Google Sheets ─────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
creds       = Credentials.from_service_account_info(google_creds_json, scopes=SCOPES)
gc          = gspread.authorize(creds)
spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)

ws_players      = spreadsheet.worksheet("Players")
ws_ranked_log   = spreadsheet.worksheet("RankedLog")
ws_h2h          = spreadsheet.worksheet("H2H")
ws_scrim_players= spreadsheet.worksheet("ScrimPlayers")
ws_scrim_log    = spreadsheet.worksheet("ScrimLog")
ws_scrim_h2h    = spreadsheet.worksheet("ScrimH2H")

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_player(name):
    for i, row in enumerate(ws_players.get_all_values()[1:], start=2):
        if row[0].lower() == name.lower():
            return i, {
                "name": row[0],
                "elo":  int(row[1]) if row[1] else STARTING_ELO,
                "rank": row[2],
                "wins": int(row[3]) if row[3] else 0,
                "losses": int(row[4]) if row[4] else 0,
                "streak": int(row[5]) if row[5] else 0,
                "last_rival": row[6],
                "last_match": row[7],
            }
    return None

def create_player(name):
    ws_players.append_row([name, STARTING_ELO, get_rank(STARTING_ELO), 0, 0, 0, "", ""])

def update_player(idx, d):
    ws_players.update(f"A{idx}:H{idx}", [[
        d["name"], d["elo"], d["rank"],
        d["wins"], d["losses"], d["streak"],
        d["last_rival"], d["last_match"]
    ]])

def get_scrim_player(name):
    for i, row in enumerate(ws_scrim_players.get_all_values()[1:], start=2):
        if row[0].lower() == name.lower():
            return i, {
                "name": row[0],
                "wins": int(row[1]) if row[1] else 0,
                "losses": int(row[2]) if row[2] else 0,
                "winrate": row[3],
                "streak": int(row[4]) if row[4] else 0,
                "last_match": row[5],
            }
    return None

def create_scrim_player(name):
    ws_scrim_players.append_row([name, 0, 0, "0%", 0, ""])

def update_scrim_player(idx, d):
    total = d["wins"] + d["losses"]
    wr = f"{(d['wins']/total*100):.0f}%" if total > 0 else "0%"
    ws_scrim_players.update(f"A{idx}:F{idx}", [[
        d["name"], d["wins"], d["losses"], wr, d["streak"], d["last_match"]
    ]])

def update_h2h(ws, p1, p2, winner):
    recs = ws.get_all_values()
    a, b = sorted([p1.lower(), p2.lower()])
    for i, row in enumerate(recs[1:], start=2):
        if row[0].lower() == a and row[1].lower() == b:
            w1, w2 = int(row[2] or 0), int(row[3] or 0)
            if winner.lower() == a: w1 += 1
            else: w2 += 1
            ws.update(f"C{i}:D{i}", [[w1, w2]])
            return
    ws.append_row([a, b,
                   1 if winner.lower() == a else 0,
                   1 if winner.lower() == b else 0])

def revert_h2h(ws, p1, p2, winner):
    recs = ws.get_all_values()
    a, b = sorted([p1.lower(), p2.lower()])
    for i, row in enumerate(recs[1:], start=2):
        if row[0].lower() == a and row[1].lower() == b:
            w1, w2 = int(row[2] or 0), int(row[3] or 0)
            if winner.lower() == a: w1 = max(0, w1 - 1)
            else: w2 = max(0, w2 - 1)
            ws.update(f"C{i}:D{i}", [[w1, w2]])
            return

def get_h2h(ws, p1, p2):
    a, b = sorted([p1.lower(), p2.lower()])
    for row in ws.get_all_values()[1:]:
        if row[0].lower() == a and row[1].lower() == b:
            return int(row[2] or 0), int(row[3] or 0)
    return 0, 0

def log_ranked(raw_w, raw_l, elo_changes, afk_players, url):
    ws_ranked_log.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        ", ".join(raw_w), ", ".join(raw_l),
        json.dumps(elo_changes),
        ", ".join(afk_players) if afk_players else "No",
        url
    ])

def log_scrim(raw_w, raw_l, afk_players, url):
    ws_scrim_log.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        ", ".join(raw_w), ", ".join(raw_l),
        ", ".join(afk_players) if afk_players else "No",
        url
    ])

def get_top_ranked(n=10):
    players = []
    for row in ws_players.get_all_values()[1:]:
        if row[0] and row[1]:
            try:
                players.append({"name": row[0], "elo": int(row[1]), "rank": row[2],
                                 "wins": int(row[3] or 0), "losses": int(row[4] or 0)})
            except ValueError:
                continue
    return sorted(players, key=lambda x: x["elo"], reverse=True)[:n]

def get_top_scrims(n=10):
    players = []
    for row in ws_scrim_players.get_all_values()[1:]:
        if row[0]:
            try:
                players.append({"name": row[0], "wins": int(row[1] or 0),
                                 "losses": int(row[2] or 0), "winrate": row[3] or "0%"})
            except ValueError:
                continue
    return sorted(players, key=lambda x: x["wins"], reverse=True)[:n]

# ── Claude Vision ─────────────────────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

VISION_PROMPT = """Analyze this Vainglory match result screenshot.
Two teams: LEFT (3 players on left side) and RIGHT (3 players on right side).

STEP 1 - Determine winner:
Look at the word shown in the center of the screen between the two kill counts.
- "Victory" or "Victoria" -> LEFT team WON
- "Defeat" or "Derrota"   -> LEFT team LOST (right team won)
- "Surrender" or "Rendicion" -> compare kill counts: team with MORE kills WON

STEP 2 - Read ALL 6 player names exactly as displayed, including any numeric prefixes like "1600_", "1800-2_", "5656-1_".

STEP 3 - Detect AFK players:
Look carefully at each player on BOTH teams.
A player is AFK if they show ANY of these signs:
- Their name has a strikethrough (a horizontal line drawn through the text) — THIS IS THE STRONGEST SIGNAL, mark as AFK immediately
- Their character portrait/avatar appears grayscale or clearly faded/desaturated compared to the others
- There is a disconnect or AFK icon near their name or portrait

Add every AFK player to afk_players regardless of which team they are on.
If no AFK players are found, use [].

Respond with ONLY this JSON, no other text:
{"left_team":["name1","name2","name3"],"right_team":["name1","name2","name3"],"left_kills":0,"right_kills":0,"winner":"left","center_word":"Victory","afk_players":[],"has_guests":false}

Additional rules:
- Guests: Guest_1234, Guest0, GuestXXXX, etc -> set has_guests true
- winner must be exactly "left" or "right"
- If the image is unreadable respond ONLY: {"error":"Could not read"}
"""

async def analyze_screenshot(image_bytes):
    compressed, media_type = compress_image(image_bytes)
    b64 = base64.b64encode(compressed).decode("utf-8")
    try:
        response = await asyncio.to_thread(
            claude_client.messages.create,
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text",  "text": VISION_PROMPT}
            ]}]
        )
        data = extract_json(response.content[0].text.strip())
        if data is None:
            return {"error": "Could not parse response / No pude interpretar la respuesta"}
        if "error" in data:
            return data

        winner_side = data.get("winner", "left")
        center      = data.get("center_word", "").lower()
        left, right = data.get("left_team", []), data.get("right_team", [])

        if "surr" in center or "rend" in center:
            lk, rk = data.get("left_kills", 0), data.get("right_kills", 0)
            if lk > rk:   winner_side = "left"
            elif rk > lk: winner_side = "right"

        w = left  if winner_side == "left" else right
        l = right if winner_side == "left" else left
        return {"winner_team": w, "loser_team": l,
                "afk_players": data.get("afk_players", []),
                "has_guests":  data.get("has_guests", False)}
    except Exception as e:
        return {"error": str(e)}

# ── ELO logic ─────────────────────────────────────────────────────────────────

async def _ensure_player(name):
    result = await asyncio.to_thread(get_player, name)
    if not result:
        await asyncio.to_thread(create_player, name)
        result = await asyncio.to_thread(get_player, name)
    return result

async def process_ranked(winner_team, loser_team, afk_players, url):
    afk_set = {clean_name(p).lower() for p in afk_players}
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    changes = {}

    raw_w = [p for p in winner_team if "guest" not in p.lower()]
    raw_l = [p for p in loser_team  if "guest" not in p.lower()]
    cw    = [clean_name(p) for p in raw_w]
    cl    = [clean_name(p) for p in raw_l]
    if not cw or not cl:
        return None, "No valid players / Sin jugadores válidos."

    pd = {}
    for name in cw + cl:
        pd[name] = await _ensure_player(name)

    avg_w = sum(pd[p][1]["elo"] for p in cw) / len(cw)
    avg_l = sum(pd[p][1]["elo"] for p in cl) / len(cl)
    eg, el = calc_elo(avg_w, avg_l)

    # Ganadores — siempre ganan ELO
    for name in cw:
        idx, d = pd[name]
        old = d["elo"]
        d["elo"]    = min(MAX_ELO, old + eg)
        d["rank"]   = get_rank(d["elo"])
        d["wins"]  += 1
        d["streak"] = max(1, d["streak"] + 1) if d["streak"] >= 0 else 1
        d["last_match"]  = now
        d["last_rival"]  = ", ".join(cl)
        changes[name] = {"old": old, "new": d["elo"], "diff": d["elo"] - old}
        await asyncio.to_thread(update_player, idx, d)

    # ¿Hay AFK en el equipo perdedor?
    hay_afk = any(n.lower() in afk_set for n in cl)

    # Perdedores
    for name in cl:
        idx, d = pd[name]
        old = d["elo"]
        d["losses"] += 1
        d["streak"]  = min(-1, d["streak"] - 1) if d["streak"] <= 0 else -1
        d["last_match"] = now
        d["last_rival"] = ", ".join(cw)

        if name.lower() in afk_set:
            # Se fue AFK → pierde ELO (penalización extra)
            d["elo"]  = max(MIN_ELO, old - el)
            d["rank"] = get_rank(d["elo"])
            changes[name] = {"old": old, "new": d["elo"], "diff": d["elo"] - old, "afk": True}
        elif hay_afk:
            # Se quedó jugando con compañero AFK → protegido, no pierde ELO
            changes[name] = {"old": old, "new": old, "diff": 0, "protected": True}
        else:
            # Derrota normal → pierde ELO
            d["elo"]  = max(MIN_ELO, old - el)
            d["rank"] = get_rank(d["elo"])
            changes[name] = {"old": old, "new": d["elo"], "diff": d["elo"] - old}

        await asyncio.to_thread(update_player, idx, d)

    for w in cw:
        for l in cl:
            await asyncio.to_thread(update_h2h, ws_h2h, w, l, w)
    await asyncio.to_thread(log_ranked, raw_w, raw_l, changes, afk_players, url)
    return changes, None

async def revert_ranked(cw, cl, afk_players):
    afk_set = {p.lower() for p in afk_players}
    pd = {}
    for name in cw + cl:
        r = await asyncio.to_thread(get_player, name)
        if r: pd[name] = r
    if not pd: return

    valid_w = [p for p in cw if p in pd]
    valid_l = [p for p in cl if p in pd]
    avg_w = sum(pd[p][1]["elo"] for p in valid_w) / max(len(valid_w), 1)
    avg_l = sum(pd[p][1]["elo"] for p in valid_l) / max(len(valid_l), 1)
    eg, el = calc_elo(avg_w, avg_l)
    hay_afk = any(n.lower() in afk_set for n in cl)

    for name in cw:
        if name not in pd: continue
        idx, d = pd[name]
        d["elo"]   = max(MIN_ELO, d["elo"] - eg)
        d["rank"]  = get_rank(d["elo"])
        d["wins"]  = max(0, d["wins"] - 1)
        d["streak"] = 0
        await asyncio.to_thread(update_player, idx, d)

    for name in cl:
        if name not in pd: continue
        idx, d = pd[name]
        d["losses"] = max(0, d["losses"] - 1)
        d["streak"] = 0
        if name.lower() in afk_set:
            d["elo"]  = min(MAX_ELO, d["elo"] + el)
            d["rank"] = get_rank(d["elo"])
        elif not hay_afk:
            d["elo"]  = min(MAX_ELO, d["elo"] + el)
            d["rank"] = get_rank(d["elo"])
        # protegido → no se toca el ELO
        await asyncio.to_thread(update_player, idx, d)

    for w in cw:
        for l in cl:
            await asyncio.to_thread(revert_h2h, ws_h2h, w, l, w)

async def process_scrims(winner_team, loser_team, afk_players, url):
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    raw_w = [p for p in winner_team if "guest" not in p.lower()]
    raw_l = [p for p in loser_team  if "guest" not in p.lower()]
    cw    = [clean_name(p) for p in raw_w]
    cl    = [clean_name(p) for p in raw_l]

    for name in cw:
        idx, d = await _ensure_player_scrim(name)
        d["wins"]  += 1
        d["streak"] = max(1, d["streak"] + 1) if d["streak"] >= 0 else 1
        d["last_match"] = now
        await asyncio.to_thread(update_scrim_player, idx, d)

    for name in cl:
        idx, d = await _ensure_player_scrim(name)
        d["losses"] += 1
        d["streak"]  = min(-1, d["streak"] - 1) if d["streak"] <= 0 else -1
        d["last_match"] = now
        await asyncio.to_thread(update_scrim_player, idx, d)

    for w in cw:
        for l in cl:
            await asyncio.to_thread(update_h2h, ws_scrim_h2h, w, l, w)
    await asyncio.to_thread(log_scrim, raw_w, raw_l, afk_players, url)

async def _ensure_player_scrim(name):
    result = await asyncio.to_thread(get_scrim_player, name)
    if not result:
        await asyncio.to_thread(create_scrim_player, name)
        result = await asyncio.to_thread(get_scrim_player, name)
    return result

async def revert_scrims(cw, cl, afk_players):
    for name in cw:
        r = await asyncio.to_thread(get_scrim_player, name)
        if not r: continue
        idx, d = r
        d["wins"]   = max(0, d["wins"] - 1)
        d["streak"] = 0
        await asyncio.to_thread(update_scrim_player, idx, d)
    for name in cl:
        r = await asyncio.to_thread(get_scrim_player, name)
        if not r: continue
        idx, d = r
        d["losses"] = max(0, d["losses"] - 1)
        d["streak"] = 0
        await asyncio.to_thread(update_scrim_player, idx, d)
    for w in cw:
        for l in cl:
            await asyncio.to_thread(revert_h2h, ws_scrim_h2h, w, l, w)

# ── Embeds ────────────────────────────────────────────────────────────────────

def build_ranked_embed(winner_team, loser_team, changes, url, footer):
    embed = discord.Embed(
        title="🏆 Ranked match registered / Partida ranked registrada",
        color=0x00FF88)
    wl, ll = [], []

    for raw in winner_team:
        if "guest" in raw.lower(): continue
        n  = clean_name(raw)
        ch = changes.get(n, {})
        wl.append(f"🟢 **{n}**\n{ch.get('old',0)} → {ch.get('new',0)} (+{ch.get('diff',0)}) | {get_rank(ch.get('new', STARTING_ELO))}")

    for raw in loser_team:
        if "guest" in raw.lower(): continue
        n  = clean_name(raw)
        ch = changes.get(n, {})
        if ch.get("afk"):
            ll.append(f"💤 **{n}** AFK\n{ch.get('old',0)} → {ch.get('new',0)} ({ch.get('diff',0)}) | {get_rank(ch.get('new', STARTING_ELO))}")
        elif ch.get("protected"):
            ll.append(f"🛡️ **{n}** Protegido / Protected\n{ch.get('old',0)} (sin cambio / no change) | {get_rank(ch.get('old', STARTING_ELO))}")
        else:
            ll.append(f"🔴 **{n}**\n{ch.get('old',0)} → {ch.get('new',0)} ({ch.get('diff',0)}) | {get_rank(ch.get('new', STARTING_ELO))}")

    embed.add_field(name="🏅 Winners / Ganadores", value="\n\n".join(wl) or "-", inline=True)
    embed.add_field(name="💀 Losers / Perdedores", value="\n\n".join(ll) or "-", inline=True)

    guests = [p for p in winner_team + loser_team if "guest" in p.lower()]
    if guests:
        embed.add_field(name="👤 Guests", value=", ".join(guests), inline=False)

    embed.set_thumbnail(url=url)
    embed.set_footer(text=footer)
    return embed

def build_scrim_embed(winner_team, loser_team, afk_players, url, footer):
    embed = discord.Embed(
        title="⚔️ Scrim registered / Scrim registrado",
        color=0xFFD700)
    wn = "\n".join(f"🟢 **{clean_name(p)}**" for p in winner_team if "guest" not in p.lower())
    ln = "\n".join(f"🔴 **{clean_name(p)}**" for p in loser_team  if "guest" not in p.lower())
    embed.add_field(name="🏅 Winners / Ganadores", value=wn or "-", inline=True)
    embed.add_field(name="💀 Losers / Perdedores", value=ln or "-", inline=True)
    if afk_players:
        embed.add_field(name="💤 AFK", value=", ".join(clean_name(p) for p in afk_players), inline=False)
    embed.set_thumbnail(url=url)
    embed.set_footer(text=footer)
    return embed

# ── View (botones) ────────────────────────────────────────────────────────────

class MatchView(View):
    def __init__(self, winner_team, loser_team, afk_players, url, mode, submitter, changes=None):
        super().__init__(timeout=None)
        self.winner_team = winner_team
        self.loser_team  = loser_team
        self.afk_players = afk_players
        self.url         = url
        self.mode        = mode
        self.submitter   = submitter
        self.changes     = changes or {}
        self.acted       = False

    def _clean(self):
        cw = [clean_name(p) for p in self.winner_team if "guest" not in p.lower()]
        cl = [clean_name(p) for p in self.loser_team  if "guest" not in p.lower()]
        ca = [clean_name(p) for p in self.afk_players]
        return cw, cl, ca

    @discord.ui.button(label="🔄 Swap", style=discord.ButtonStyle.secondary)
    async def swap_btn(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True)
            return
        if self.acted:
            await interaction.response.send_message("⚠️ Already modified / Ya modificado.", ephemeral=True)
            return
        self.acted = True
        for c in self.children: c.disabled = True
        await interaction.response.defer()

        cw, cl, ca = self._clean()
        if self.mode == "ranked": await revert_ranked(cw, cl, ca)
        else:                     await revert_scrims(cw, cl, ca)

        self.winner_team, self.loser_team = self.loser_team, self.winner_team

        if self.mode == "ranked":
            self.changes, _ = await process_ranked(self.winner_team, self.loser_team, self.afk_players, self.url)
            embed = build_ranked_embed(self.winner_team, self.loser_team, self.changes, self.url, f"By / Por {self.submitter}")
        else:
            await process_scrims(self.winner_team, self.loser_team, self.afk_players, self.url)
            embed = build_scrim_embed(self.winner_team, self.loser_team, self.afk_players, self.url, f"By / Por {self.submitter}")

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True)
            return
        if self.acted:
            await interaction.response.send_message("⚠️ Already modified / Ya modificado.", ephemeral=True)
            return
        self.acted = True
        for c in self.children: c.disabled = True
        await interaction.response.defer()

        cw, cl, ca = self._clean()
        if self.mode == "ranked": await revert_ranked(cw, cl, ca)
        else:                     await revert_scrims(cw, cl, ca)

        embed = discord.Embed(title="🗑️ Match removed / Partida eliminada", color=0x666666)
        embed.set_thumbnail(url=self.url)
        await interaction.edit_original_response(embed=embed, view=self)

# ── Bot ───────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commands synced")
    except Exception as e:
        print(f"Sync error: {e}")

@bot.event
async def on_message(message):
    if message.author.bot: return
    ch = message.channel.name

    # Solo actuar en canales ranked/scrims con imagen
    if ch not in [RANKED_CHANNEL, SCRIMS_CHANNEL]:
        await bot.process_commands(message)
        return

    img_att = next((a for a in message.attachments
                    if a.content_type and a.content_type.startswith("image/")), None)
    if not img_att:
        await bot.process_commands(message)
        return

    # A partir de aquí manejamos la imagen — NO llamar process_commands después
    mode      = "ranked" if ch == RANKED_CHANNEL else "scrim"
    submitter = message.author.display_name
    proc      = await message.reply("🔍 Analyzing / Analizando...")

    try:
        result = await analyze_screenshot(await img_att.read())

        if "error" in result:
            await proc.edit(content=f"❌ {result['error']}")
            return

        wt     = result["winner_team"]
        lt     = result["loser_team"]
        afk    = result.get("afk_players", [])
        guests = result.get("has_guests", False)

        if mode == "scrim":
            if guests:
                await proc.edit(content="❌ Invalid scrim: Guests not allowed / Sin Guests.")
                return
            await process_scrims(wt, lt, afk, img_att.url)
            embed = build_scrim_embed(wt, lt, afk, img_att.url, f"By / Por {submitter}")
            view  = MatchView(wt, lt, afk, img_att.url, mode, submitter)
            await proc.edit(content=None, embed=embed, view=view)
            return

        changes, error = await process_ranked(wt, lt, afk, img_att.url)
        if error:
            await proc.edit(content=f"❌ {error}")
            return
        embed = build_ranked_embed(wt, lt, changes, img_att.url, f"By / Por {submitter}")
        view  = MatchView(wt, lt, afk, img_att.url, mode, submitter, changes)
        await proc.edit(content=None, embed=embed, view=view)

    except Exception as e:
        await proc.edit(content=f"❌ Error: {str(e)}")

# ── Slash commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="ranking", description="Top 10 ranked ELO")
async def ranking_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_ranked, 10)
    if not players:
        await interaction.response.send_message("No players yet / No hay jugadores aún.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines  = [
        f"{medals[i] if i < 3 else f'{i+1}.'} **{p['name']}** — {p['elo']} ELO | {p['rank']} | {p['wins']}W-{p['losses']}L"
        for i, p in enumerate(players)
    ]
    embed = discord.Embed(title="🏆 Top 10 Ranked ELO", description="\n".join(lines), color=0xFFD700)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ranking_scrims", description="Top 10 scrims")
async def ranking_scrims_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_scrims, 10)
    if not players:
        await interaction.response.send_message("No scrim players yet / No hay jugadores aún.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines  = [
        f"{medals[i] if i < 3 else f'{i+1}.'} **{p['name']}** — {p['wins']}W-{p['losses']}L ({p['winrate']})"
        for i, p in enumerate(players)
    ]
    embed = discord.Embed(title="⚔️ Top 10 Scrims", description="\n".join(lines), color=0xFF4444)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="perfil", description="Player profile / Perfil del jugador")
@app_commands.describe(jugador="Player name / Nombre del jugador")
async def perfil_cmd(interaction: discord.Interaction, jugador: str):
    result = await asyncio.to_thread(get_player, jugador)
    sr     = await asyncio.to_thread(get_scrim_player, jugador)
    if not result and not sr:
        await interaction.response.send_message(f"❌ Not found / No encontré a **{jugador}**.")
        return

    embed = discord.Embed(title=f"👤 Perfil: {jugador}", color=0x00BFFF)

    if result:
        _, d  = result
        total = d["wins"] + d["losses"]
        wr    = f"{(d['wins']/total*100):.1f}%" if total else "N/A"
        streak = (f"🔥 {d['streak']}W racha"      if d["streak"] > 0 else
                  f"❄️ {abs(d['streak'])}L racha" if d["streak"] < 0 else "-")
        elo  = d["elo"]
        prog = 0
        for ts, te, *_ in TIERS:
            if ts <= elo <= te:
                prog = (elo - ts) / (te - ts + 1) * 100
                break
        bar = "█" * round(prog / 10) + "░" * (10 - round(prog / 10))
        embed.add_field(
            name="🏆 Ranked",
            value=f"**{elo}** ELO | {d['rank']}\n{d['wins']}W-{d['losses']}L ({wr})\nStreak: {streak}\n`{bar}` {prog:.0f}%",
            inline=False)

    if sr:
        _, s  = sr
        st    = s["wins"] + s["losses"]
        swr   = f"{(s['wins']/st*100):.1f}%" if st else "N/A"
        ss    = (f"🔥 {s['streak']}W racha"      if s["streak"] > 0 else
                 f"❄️ {abs(s['streak'])}L racha" if s["streak"] < 0 else "-")
        embed.add_field(
            name="⚔️ Scrims",
            value=f"{s['wins']}W-{s['losses']}L ({swr})\nStreak: {ss}",
            inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="vs", description="Head-to-head / Enfrentamiento directo")
@app_commands.describe(jugador1="Player 1", jugador2="Player 2")
async def vs_cmd(interaction: discord.Interaction, jugador1: str, jugador2: str):
    rw1, rw2 = await asyncio.to_thread(get_h2h, ws_h2h,       jugador1, jugador2)
    sw1, sw2 = await asyncio.to_thread(get_h2h, ws_scrim_h2h, jugador1, jugador2)
    p1, p2   = sorted([jugador1.lower(), jugador2.lower()])

    if not any([rw1, rw2, sw1, sw2]):
        await interaction.response.send_message(
            f"❌ No matches between / Sin partidas entre **{jugador1}** y **{jugador2}**.")
        return

    embed = discord.Embed(title=f"⚔️ {p1} vs {p2}", color=0xFF6600)
    if rw1 or rw2:
        embed.add_field(name="🏆 Ranked",
                        value=f"**{p1}**: {rw1}W\n**{p2}**: {rw2}W\n{rw1+rw2} partidas", inline=True)
    if sw1 or sw2:
        embed.add_field(name="⚔️ Scrims",
                        value=f"**{p1}**: {sw1}W\n**{p2}**: {sw2}W\n{sw1+sw2} partidas", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="anular", description="[Admin] Info sobre controles de partida")
async def anular_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True)
        return
    await interaction.response.send_message(
        "🔄 **Swap** = invertir ganadores/perdedores | invert winners/losers\n"
        "🗑️ **Delete** = eliminar partida | remove match\n\n"
        "Usa los botones en cada partida / Use the buttons on each match.",
        ephemeral=True)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
