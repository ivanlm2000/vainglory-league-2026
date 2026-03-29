# Leon Coach League - Discord Bot v15
# Vainglory ranked + scrims tracker
# Claude Vision + Google Sheets
# Bilingual EN/ES | Admin swap + delete + AFK + EDIT buttons
# v15: Batch writes to Sheets, 5v5 support, renamed channels

import os
import io
import re
import json
import base64
import asyncio
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput
import gspread
from google.oauth2.service_account import Credentials
import anthropic
from PIL import Image

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
google_creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])

RANKED_CHANNEL = "matches"
SCRIMS_3V3_CHANNEL = "3v3"
SCRIMS_5V5_CHANNEL = "5v5"
BOT_ADMIN_ROLE = "bot admin"

def is_bot_admin(user: discord.Member) -> bool:
    if user.guild_permissions.administrator:
        return True
    return any(role.name.lower() == BOT_ADMIN_ROLE.lower() for role in user.roles)

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

class SheetsManager:
    def __init__(self):
        self.creds = Credentials.from_service_account_info(google_creds_json, scopes=SCOPES)
        self.gc = gspread.authorize(self.creds)
        self.spreadsheet = self.gc.open_by_key(GOOGLE_SHEET_ID)
        self._cache_worksheets()
        self._last_auth = time.time()

    def _cache_worksheets(self):
        self.ws_players       = self.spreadsheet.worksheet("Players")
        self.ws_ranked_log    = self.spreadsheet.worksheet("RankedLog")
        self.ws_h2h           = self.spreadsheet.worksheet("H2H")
        self.ws_scrim_players = self.spreadsheet.worksheet("ScrimPlayers")
        self.ws_scrim_log     = self.spreadsheet.worksheet("ScrimLog")
        self.ws_scrim_h2h     = self.spreadsheet.worksheet("ScrimH2H")
        self.ws_scrim5_players = self.spreadsheet.worksheet("ScrimPlayers5v5")
        self.ws_scrim5_log     = self.spreadsheet.worksheet("ScrimLog5v5")
        self.ws_scrim5_h2h     = self.spreadsheet.worksheet("ScrimH2H5v5")

    def _re_auth_if_needed(self):
        if time.time() - self._last_auth > 2700:
            print("[Sheets] Re-authenticating credentials...")
            self.creds = Credentials.from_service_account_info(google_creds_json, scopes=SCOPES)
            self.gc = gspread.authorize(self.creds)
            self.spreadsheet = self.gc.open_by_key(GOOGLE_SHEET_ID)
            self._cache_worksheets()
            self._last_auth = time.time()

    def call(self, func, *args, retries=3, **kwargs):
        delays = [5, 10, 15]
        for attempt in range(retries):
            try:
                self._re_auth_if_needed()
                return func(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                code = e.response.status_code if hasattr(e, 'response') else 0
                if code == 429 and attempt < retries - 1:
                    wait = delays[min(attempt, len(delays) - 1)]
                    print(f"[Sheets] Rate limit hit, waiting {wait}s (attempt {attempt+1}/{retries})...")
                    time.sleep(wait)
                    continue
                elif code in (401, 403):
                    print(f"[Sheets] Auth error {code}, forcing re-auth...")
                    self._last_auth = 0
                    self._re_auth_if_needed()
                    if attempt < retries - 1:
                        continue
                raise
            except Exception as e:
                if "transport" in str(e).lower() or "credential" in str(e).lower():
                    print(f"[Sheets] Transport/credential error, forcing re-auth...")
                    self._last_auth = 0
                    self._re_auth_if_needed()
                    if attempt < retries - 1:
                        continue
                raise
        return None

    def batch_update_cells(self, ws, updates):
        """Batch update: updates = list of {"range": "A2:H2", "values": [[...]]}
        Hace UNA sola llamada a la API en vez de N."""
        if not updates:
            return
        self._re_auth_if_needed()
        body = []
        for u in updates:
            body.append({
                "range": f"{ws.title}!{u['range']}",
                "values": u["values"]
            })
        self.spreadsheet.values_batch_update(body={
            "valueInputOption": "RAW",
            "data": body
        })

    def batch_append_rows(self, ws, rows):
        """Append múltiples filas en UNA sola llamada."""
        if not rows:
            return
        self._re_auth_if_needed()
        ws.append_rows(rows, value_input_option="RAW")

sheets = SheetsManager()

# ── Cache ─────────────────────────────────────────────────────────────────────

class PlayerCache:
    def __init__(self):
        self.ranked = {}
        self.scrims = {}
        self.scrims5 = {}
        self.loaded_ranked = False
        self.loaded_scrims = False
        self.loaded_scrims5 = False

    def load_ranked(self):
        rows = sheets.call(sheets.ws_players.get_all_values)
        self.ranked.clear()
        for i, row in enumerate(rows[1:], start=2):
            if not row[0]: continue
            self.ranked[row[0].lower()] = {
                "row": i, "data": {
                    "name": row[0], "elo": int(row[1]) if row[1] else STARTING_ELO,
                    "rank": row[2], "wins": int(row[3]) if row[3] else 0,
                    "losses": int(row[4]) if row[4] else 0,
                    "streak": int(row[5]) if row[5] else 0,
                    "last_rival": row[6] if len(row) > 6 else "",
                    "last_match": row[7] if len(row) > 7 else "",
                }}
        self.loaded_ranked = True

    def load_scrims(self):
        rows = sheets.call(sheets.ws_scrim_players.get_all_values)
        self.scrims.clear()
        for i, row in enumerate(rows[1:], start=2):
            if not row[0]: continue
            self.scrims[row[0].lower()] = {
                "row": i, "data": {
                    "name": row[0], "wins": int(row[1]) if row[1] else 0,
                    "losses": int(row[2]) if row[2] else 0,
                    "winrate": row[3] if len(row) > 3 else "0%",
                    "streak": int(row[4]) if row[4] else 0,
                    "last_match": row[5] if len(row) > 5 else "",
                }}
        self.loaded_scrims = True

    def load_scrims5(self):
        rows = sheets.call(sheets.ws_scrim5_players.get_all_values)
        self.scrims5.clear()
        for i, row in enumerate(rows[1:], start=2):
            if not row[0]: continue
            self.scrims5[row[0].lower()] = {
                "row": i, "data": {
                    "name": row[0], "wins": int(row[1]) if row[1] else 0,
                    "losses": int(row[2]) if row[2] else 0,
                    "winrate": row[3] if len(row) > 3 else "0%",
                    "streak": int(row[4]) if row[4] else 0,
                    "last_match": row[5] if len(row) > 5 else "",
                }}
        self.loaded_scrims5 = True

    def invalidate(self):
        self.loaded_ranked = False
        self.loaded_scrims = False
        self.loaded_scrims5 = False

cache = PlayerCache()

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_player(name):
    if not cache.loaded_ranked: cache.load_ranked()
    entry = cache.ranked.get(name.lower())
    return (entry["row"], dict(entry["data"])) if entry else None

def create_player(name):
    sheets.call(sheets.ws_players.append_row, [name, STARTING_ELO, get_rank(STARTING_ELO), 0, 0, 0, "", ""])
    cache.loaded_ranked = False

def update_player(idx, d):
    sheets.call(sheets.ws_players.update, f"A{idx}:H{idx}", [[
        d["name"], d["elo"], d["rank"], d["wins"], d["losses"], d["streak"], d["last_rival"], d["last_match"]]])
    cache.ranked[d["name"].lower()] = {"row": idx, "data": dict(d)}

def get_scrim_player(name):
    if not cache.loaded_scrims: cache.load_scrims()
    entry = cache.scrims.get(name.lower())
    return (entry["row"], dict(entry["data"])) if entry else None

def create_scrim_player(name):
    sheets.call(sheets.ws_scrim_players.append_row, [name, 0, 0, "0%", 0, ""])
    cache.loaded_scrims = False

def update_scrim_player_cache(idx, d):
    """Solo actualiza cache, NO escribe a Sheets (para batch)."""
    total = d["wins"] + d["losses"]
    wr = f"{(d['wins']/total*100):.0f}%" if total > 0 else "0%"
    d_copy = dict(d); d_copy["winrate"] = wr
    cache.scrims[d["name"].lower()] = {"row": idx, "data": d_copy}
    return [d["name"], d["wins"], d["losses"], wr, d["streak"], d["last_match"]]

def get_scrim5_player(name):
    if not cache.loaded_scrims5: cache.load_scrims5()
    entry = cache.scrims5.get(name.lower())
    return (entry["row"], dict(entry["data"])) if entry else None

def create_scrim5_player(name):
    sheets.call(sheets.ws_scrim5_players.append_row, [name, 0, 0, "0%", 0, ""])
    cache.loaded_scrims5 = False

def update_scrim5_player_cache(idx, d):
    """Solo actualiza cache, NO escribe a Sheets (para batch)."""
    total = d["wins"] + d["losses"]
    wr = f"{(d['wins']/total*100):.0f}%" if total > 0 else "0%"
    d_copy = dict(d); d_copy["winrate"] = wr
    cache.scrims5[d["name"].lower()] = {"row": idx, "data": d_copy}
    return [d["name"], d["wins"], d["losses"], wr, d["streak"], d["last_match"]]

# ── H2H batch helpers ─────────────────────────────────────────────────────────

def update_h2h_batch(ws, pairs_winners):
    """Actualiza TODOS los H2H de una partida en batch.
    pairs_winners = list of (p1, p2, winner)
    Lee la hoja UNA vez, calcula cambios, escribe UNA vez."""
    recs = sheets.call(ws.get_all_values)
    existing = {}
    for i, row in enumerate(recs[1:], start=2):
        if len(row) >= 4:
            key = (row[0].lower(), row[1].lower())
            existing[key] = {"row": i, "w1": int(row[2] or 0), "w2": int(row[3] or 0)}

    updates = []
    new_rows = []
    for p1, p2, winner in pairs_winners:
        a, b = sorted([p1.lower(), p2.lower()])
        key = (a, b)
        if key in existing:
            e = existing[key]
            if winner.lower() == a:
                e["w1"] += 1
            else:
                e["w2"] += 1
            e["dirty"] = True
        else:
            w1 = 1 if winner.lower() == a else 0
            w2 = 1 if winner.lower() == b else 0
            existing[key] = {"row": None, "w1": w1, "w2": w2, "dirty": False}
            new_rows.append([a, b, w1, w2])

    for key, e in existing.items():
        if e.get("dirty") and e["row"]:
            updates.append({"range": f"C{e['row']}:D{e['row']}", "values": [[e["w1"], e["w2"]]]})

    if updates:
        sheets.call(sheets.batch_update_cells, ws, updates)
    if new_rows:
        sheets.call(sheets.batch_append_rows, ws, new_rows)

def revert_h2h_batch(ws, pairs_winners):
    """Revierte H2H en batch."""
    recs = sheets.call(ws.get_all_values)
    existing = {}
    for i, row in enumerate(recs[1:], start=2):
        if len(row) >= 4:
            key = (row[0].lower(), row[1].lower())
            existing[key] = {"row": i, "w1": int(row[2] or 0), "w2": int(row[3] or 0)}

    updates = []
    for p1, p2, winner in pairs_winners:
        a, b = sorted([p1.lower(), p2.lower()])
        key = (a, b)
        if key in existing:
            e = existing[key]
            if winner.lower() == a:
                e["w1"] = max(0, e["w1"] - 1)
            else:
                e["w2"] = max(0, e["w2"] - 1)
            e["dirty"] = True

    for key, e in existing.items():
        if e.get("dirty") and e["row"]:
            updates.append({"range": f"C{e['row']}:D{e['row']}", "values": [[e["w1"], e["w2"]]]})

    if updates:
        sheets.call(sheets.batch_update_cells, ws, updates)

# ── Single H2H read (para /vs command) ───────────────────────────────────────

def get_h2h(ws, p1, p2):
    a, b = sorted([p1.lower(), p2.lower()])
    recs = sheets.call(ws.get_all_values)
    for row in recs[1:]:
        if row[0].lower() == a and row[1].lower() == b:
            return int(row[2] or 0), int(row[3] or 0)
    return 0, 0

# ── Logging ───────────────────────────────────────────────────────────────────

def log_ranked(raw_w, raw_l, elo_changes, afk_players, url):
    sheets.call(sheets.ws_ranked_log.append_row, [
        datetime.now().strftime("%Y-%m-%d %H:%M"), ", ".join(raw_w), ", ".join(raw_l),
        json.dumps(elo_changes), ", ".join(afk_players) if afk_players else "No", url])

def log_scrim(raw_w, raw_l, afk_players, url):
    sheets.call(sheets.ws_scrim_log.append_row, [
        datetime.now().strftime("%Y-%m-%d %H:%M"), ", ".join(raw_w), ", ".join(raw_l),
        ", ".join(afk_players) if afk_players else "No", url])

def log_scrim5(raw_w, raw_l, afk_players, url):
    sheets.call(sheets.ws_scrim5_log.append_row, [
        datetime.now().strftime("%Y-%m-%d %H:%M"), ", ".join(raw_w), ", ".join(raw_l),
        ", ".join(afk_players) if afk_players else "No", url])

# ── Leaderboards ──────────────────────────────────────────────────────────────

def get_top_ranked(n=10):
    if not cache.loaded_ranked: cache.load_ranked()
    players = [{"name": e["data"]["name"], "elo": e["data"]["elo"], "rank": e["data"]["rank"],
                "wins": e["data"]["wins"], "losses": e["data"]["losses"]}
               for e in cache.ranked.values() if e["data"]["name"] and e["data"]["elo"]]
    return sorted(players, key=lambda x: x["elo"], reverse=True)[:n]

def get_top_scrims(n=10):
    if not cache.loaded_scrims: cache.load_scrims()
    players = [{"name": e["data"]["name"], "wins": e["data"]["wins"],
                "losses": e["data"]["losses"], "winrate": e["data"].get("winrate", "0%")}
               for e in cache.scrims.values() if e["data"]["name"]]
    return sorted(players, key=lambda x: x["wins"], reverse=True)[:n]

def get_top_scrims5(n=10):
    if not cache.loaded_scrims5: cache.load_scrims5()
    players = [{"name": e["data"]["name"], "wins": e["data"]["wins"],
                "losses": e["data"]["losses"], "winrate": e["data"].get("winrate", "0%")}
               for e in cache.scrims5.values() if e["data"]["name"]]
    return sorted(players, key=lambda x: x["wins"], reverse=True)[:n]

# ── Claude Vision ─────────────────────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def get_vision_prompt(team_size=3):
    total = team_size * 2
    player_slots = ',"name4","name5"' if team_size == 5 else ''
    return f"""Analyze this Vainglory match result screenshot.
Two teams: LEFT ({team_size} players on left side) and RIGHT ({team_size} players on right side).

STEP 1 - Determine winner:
Look at the word shown in the center of the screen between the two kill counts.
- "Victory" or "Victoria" -> LEFT team WON
- "Defeat" or "Derrota"   -> LEFT team LOST (right team won)
- "Surrender" or "Rendicion" -> compare kill counts: team with MORE kills WON

STEP 2 - Read ALL {total} player names exactly as displayed, including any numeric prefixes like "1600_", "1800-2_", "5656-1_".

STEP 3 - Detect AFK players:
Look carefully at each player on BOTH teams.
A player is AFK if they show ANY of these signs:
- Their name has a strikethrough (a horizontal line drawn through the text) — THIS IS THE STRONGEST SIGNAL, mark as AFK immediately
- Their character portrait/avatar appears grayscale or clearly faded/desaturated compared to the others
- There is a disconnect or AFK icon near their name or portrait

Add every AFK player to afk_players regardless of which team they are on.
If no AFK players are found, use [].

Respond with ONLY this JSON, no other text:
{{"left_team":["name1","name2","name3"{player_slots}],"right_team":["name1","name2","name3"{player_slots}],"left_kills":0,"right_kills":0,"winner":"left","center_word":"Victory","afk_players":[],"has_guests":false}}

Additional rules:
- Guests: Guest_1234, Guest0, GuestXXXX, etc -> set has_guests true
- winner must be exactly "left" or "right"
- If the image is unreadable respond ONLY: {{"error":"Could not read"}}
"""

async def analyze_screenshot(image_bytes, team_size=3):
    compressed, media_type = compress_image(image_bytes)
    b64 = base64.b64encode(compressed).decode("utf-8")
    prompt = get_vision_prompt(team_size)
    try:
        response = await asyncio.to_thread(
            claude_client.messages.create,
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text",  "text": prompt}
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

# ── Ranked processing ─────────────────────────────────────────────────────────

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

    # Preparar batch de updates para Players
    player_updates = []

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
        player_updates.append({"range": f"A{idx}:H{idx}", "values": [[
            d["name"], d["elo"], d["rank"], d["wins"], d["losses"], d["streak"], d["last_rival"], d["last_match"]]]})
        cache.ranked[d["name"].lower()] = {"row": idx, "data": dict(d)}

    hay_afk = any(n.lower() in afk_set for n in cl)
    for name in cl:
        idx, d = pd[name]
        old = d["elo"]
        d["losses"] += 1
        d["streak"]  = min(-1, d["streak"] - 1) if d["streak"] <= 0 else -1
        d["last_match"] = now
        d["last_rival"] = ", ".join(cw)
        if name.lower() in afk_set:
            d["elo"]  = max(MIN_ELO, old - el)
            d["rank"] = get_rank(d["elo"])
            changes[name] = {"old": old, "new": d["elo"], "diff": d["elo"] - old, "afk": True}
        elif hay_afk:
            changes[name] = {"old": old, "new": old, "diff": 0, "protected": True}
        else:
            d["elo"]  = max(MIN_ELO, old - el)
            d["rank"] = get_rank(d["elo"])
            changes[name] = {"old": old, "new": d["elo"], "diff": d["elo"] - old}
        player_updates.append({"range": f"A{idx}:H{idx}", "values": [[
            d["name"], d["elo"], d["rank"], d["wins"], d["losses"], d["streak"], d["last_rival"], d["last_match"]]]})
        cache.ranked[d["name"].lower()] = {"row": idx, "data": dict(d)}

    # 1 batch write para todos los jugadores
    await asyncio.to_thread(sheets.call, sheets.batch_update_cells, sheets.ws_players, player_updates)

    # 1 batch para H2H
    h2h_pairs = [(w, l, w) for w in cw for l in cl]
    await asyncio.to_thread(update_h2h_batch, sheets.ws_h2h, h2h_pairs)

    # 1 write para log
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

    player_updates = []
    for name in cw:
        if name not in pd: continue
        idx, d = pd[name]
        d["elo"]   = max(MIN_ELO, d["elo"] - eg)
        d["rank"]  = get_rank(d["elo"])
        d["wins"]  = max(0, d["wins"] - 1)
        d["streak"] = 0
        player_updates.append({"range": f"A{idx}:H{idx}", "values": [[
            d["name"], d["elo"], d["rank"], d["wins"], d["losses"], d["streak"], d.get("last_rival",""), d.get("last_match","")]]})
        cache.ranked[d["name"].lower()] = {"row": idx, "data": dict(d)}
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
        player_updates.append({"range": f"A{idx}:H{idx}", "values": [[
            d["name"], d["elo"], d["rank"], d["wins"], d["losses"], d["streak"], d.get("last_rival",""), d.get("last_match","")]]})
        cache.ranked[d["name"].lower()] = {"row": idx, "data": dict(d)}

    if player_updates:
        await asyncio.to_thread(sheets.call, sheets.batch_update_cells, sheets.ws_players, player_updates)

    h2h_pairs = [(w, l, w) for w in cw for l in cl if w in pd and l in pd]
    if h2h_pairs:
        await asyncio.to_thread(revert_h2h_batch, sheets.ws_h2h, h2h_pairs)

# ── Scrims processing (batch) ─────────────────────────────────────────────────

async def _ensure_player_scrim(name):
    result = await asyncio.to_thread(get_scrim_player, name)
    if not result:
        await asyncio.to_thread(create_scrim_player, name)
        result = await asyncio.to_thread(get_scrim_player, name)
    return result

async def _ensure_player_scrim5(name):
    result = await asyncio.to_thread(get_scrim5_player, name)
    if not result:
        await asyncio.to_thread(create_scrim5_player, name)
        result = await asyncio.to_thread(get_scrim5_player, name)
    return result

async def process_scrims(winner_team, loser_team, afk_players, url, mode="3v3"):
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    raw_w = [p for p in winner_team if "guest" not in p.lower()]
    raw_l = [p for p in loser_team  if "guest" not in p.lower()]
    cw    = [clean_name(p) for p in raw_w]
    cl    = [clean_name(p) for p in raw_l]
    is_5v5 = (mode == "5v5")
    ensure_func = _ensure_player_scrim5 if is_5v5 else _ensure_player_scrim
    cache_func  = update_scrim5_player_cache if is_5v5 else update_scrim_player_cache
    player_ws   = sheets.ws_scrim5_players if is_5v5 else sheets.ws_scrim_players
    h2h_ws      = sheets.ws_scrim5_h2h if is_5v5 else sheets.ws_scrim_h2h
    log_func    = log_scrim5 if is_5v5 else log_scrim

    # Asegurar que todos los jugadores existen (puede crear nuevos)
    player_data = {}
    for name in cw + cl:
        player_data[name] = await ensure_func(name)

    # Calcular cambios en memoria
    player_updates = []
    for name in cw:
        idx, d = player_data[name]
        d["wins"]  += 1
        d["streak"] = max(1, d["streak"] + 1) if d["streak"] >= 0 else 1
        d["last_match"] = now
        row_values = cache_func(idx, d)
        player_updates.append({"range": f"A{idx}:F{idx}", "values": [row_values]})

    for name in cl:
        idx, d = player_data[name]
        d["losses"] += 1
        d["streak"]  = min(-1, d["streak"] - 1) if d["streak"] <= 0 else -1
        d["last_match"] = now
        row_values = cache_func(idx, d)
        player_updates.append({"range": f"A{idx}:F{idx}", "values": [row_values]})

    # 1 batch write para todos los jugadores
    await asyncio.to_thread(sheets.call, sheets.batch_update_cells, player_ws, player_updates)

    # 1 batch para H2H
    h2h_pairs = [(w, l, w) for w in cw for l in cl]
    await asyncio.to_thread(update_h2h_batch, h2h_ws, h2h_pairs)

    # 1 write para log
    await asyncio.to_thread(log_func, raw_w, raw_l, afk_players, url)

async def revert_scrims(cw, cl, afk_players, mode="3v3"):
    is_5v5 = (mode == "5v5")
    get_func   = get_scrim5_player if is_5v5 else get_scrim_player
    cache_func = update_scrim5_player_cache if is_5v5 else update_scrim_player_cache
    player_ws  = sheets.ws_scrim5_players if is_5v5 else sheets.ws_scrim_players
    h2h_ws     = sheets.ws_scrim5_h2h if is_5v5 else sheets.ws_scrim_h2h

    player_updates = []
    valid_cw, valid_cl = [], []
    for name in cw:
        r = await asyncio.to_thread(get_func, name)
        if not r: continue
        idx, d = r
        d["wins"]   = max(0, d["wins"] - 1)
        d["streak"] = 0
        row_values = cache_func(idx, d)
        player_updates.append({"range": f"A{idx}:F{idx}", "values": [row_values]})
        valid_cw.append(name)
    for name in cl:
        r = await asyncio.to_thread(get_func, name)
        if not r: continue
        idx, d = r
        d["losses"] = max(0, d["losses"] - 1)
        d["streak"] = 0
        row_values = cache_func(idx, d)
        player_updates.append({"range": f"A{idx}:F{idx}", "values": [row_values]})
        valid_cl.append(name)

    if player_updates:
        await asyncio.to_thread(sheets.call, sheets.batch_update_cells, player_ws, player_updates)

    h2h_pairs = [(w, l, w) for w in valid_cw for l in valid_cl]
    if h2h_pairs:
        await asyncio.to_thread(revert_h2h_batch, h2h_ws, h2h_pairs)

# ── Embeds ────────────────────────────────────────────────────────────────────

def build_ranked_embed(winner_team, loser_team, changes, afk_players, url, footer):
    embed = discord.Embed(title="🏆 Ranked match registered / Partida ranked registrada", color=0x00FF88)
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

def build_scrim_embed(winner_team, loser_team, afk_players, url, footer, mode="3v3"):
    label = "3v3" if mode == "3v3" else "5v5"
    color = 0xFFD700 if mode == "3v3" else 0x8844FF

    if mode == "5v5":
        cw = [clean_name(p) for p in winner_team if "guest" not in p.lower()]
        cl = [clean_name(p) for p in loser_team  if "guest" not in p.lower()]
        lines = []
        lines.append("**Winners / Ganadores**")
        lines.append(", ".join(cw) if cw else "-")
        lines.append("")
        lines.append("**Losers / Perdedores**")
        lines.append(", ".join(cl) if cl else "-")
        if afk_players:
            lines.append("")
            lines.append(f"**AFK:** {', '.join(clean_name(p) for p in afk_players)}")
        embed = discord.Embed(
            title=f"⚔️ Scrim {label} registered / Scrim {label} registrado",
            description="\n".join(lines), color=color)
        embed.set_thumbnail(url=url)
        embed.set_footer(text=footer)
        return embed

    embed = discord.Embed(
        title=f"⚔️ Scrim {label} registered / Scrim {label} registrado", color=color)
    wn = "\n".join(f"🟢 **{clean_name(p)}**" for p in winner_team if "guest" not in p.lower())
    ln = "\n".join(f"🔴 **{clean_name(p)}**" for p in loser_team  if "guest" not in p.lower())
    embed.add_field(name="🏅 Winners / Ganadores", value=wn or "-", inline=True)
    embed.add_field(name="💀 Losers / Perdedores", value=ln or "-", inline=True)
    if afk_players:
        embed.add_field(name="💤 AFK", value=", ".join(clean_name(p) for p in afk_players), inline=False)
    embed.set_thumbnail(url=url)
    embed.set_footer(text=footer)
    return embed

# ── Edit Names Modal ──────────────────────────────────────────────────────────

class EditNamesModal(Modal):
    def __init__(self, match_view: 'MatchView'):
        super().__init__(title="✏️ Edit Names / Editar Nombres")
        self.match_view = match_view
        current_winners = [p for p in match_view.winner_team if "guest" not in p.lower()]
        current_losers  = [p for p in match_view.loser_team  if "guest" not in p.lower()]
        self.winners_input = TextInput(label="Winners / Ganadores (one per line)", style=discord.TextStyle.paragraph,
            default="\n".join(current_winners), placeholder="Player1\nPlayer2\nPlayer3", required=True, max_length=500)
        self.losers_input = TextInput(label="Losers / Perdedores (one per line)", style=discord.TextStyle.paragraph,
            default="\n".join(current_losers), placeholder="Player1\nPlayer2\nPlayer3", required=True, max_length=500)
        self.add_item(self.winners_input)
        self.add_item(self.losers_input)

    async def on_submit(self, interaction: discord.Interaction):
        mv = self.match_view
        if mv.deleted:
            await interaction.response.send_message("⚠️ Match deleted / Partida eliminada.", ephemeral=True); return
        if mv.processing:
            await interaction.response.send_message("⏳ Processing... / Procesando...", ephemeral=True); return
        new_winners = [n.strip() for n in self.winners_input.value.strip().split("\n") if n.strip()]
        new_losers  = [n.strip() for n in self.losers_input.value.strip().split("\n") if n.strip()]
        if not new_winners or not new_losers:
            await interaction.response.send_message("❌ Both teams need at least 1 player / Ambos equipos necesitan al menos 1 jugador.", ephemeral=True); return

        mv.processing = True
        await interaction.response.defer()
        try:
            cw_old, cl_old, ca_old = mv._clean()
            if mv.mode == "ranked": await revert_ranked(cw_old, cl_old, ca_old)
            else: await revert_scrims(cw_old, cl_old, ca_old, mode=mv.scrim_mode)

            old_winner_guests = [p for p in mv.winner_team if "guest" in p.lower()]
            old_loser_guests  = [p for p in mv.loser_team  if "guest" in p.lower()]
            mv.winner_team = new_winners + old_winner_guests
            mv.loser_team  = new_losers  + old_loser_guests
            new_loser_lower = {clean_name(p).lower() for p in new_losers}
            mv.manual_afk = {n for n in mv.manual_afk if n in new_loser_lower}
            mv.afk_players = [p for p in mv.afk_players
                              if clean_name(p).lower() in new_loser_lower or
                                 clean_name(p).lower() in {clean_name(w).lower() for w in new_winners}]
            effective_afk = mv._get_effective_afk_names()

            if mv.mode == "ranked":
                cache.invalidate()
                mv.changes, err = await process_ranked(mv.winner_team, mv.loser_team, effective_afk, mv.url)
                if err: await interaction.followup.send(f"❌ {err}", ephemeral=True); return
                embed = build_ranked_embed(mv.winner_team, mv.loser_team, mv.changes, effective_afk, mv.url,
                    f"By / Por {mv.submitter} | ✏️ Edited by / Editado por {interaction.user.display_name}")
            else:
                cache.invalidate()
                await process_scrims(mv.winner_team, mv.loser_team, effective_afk, mv.url, mode=mv.scrim_mode)
                embed = build_scrim_embed(mv.winner_team, mv.loser_team, effective_afk, mv.url,
                    f"By / Por {mv.submitter} | ✏️ Edited by / Editado por {interaction.user.display_name}", mode=mv.scrim_mode)

            mv._rebuild_buttons()
            await interaction.edit_original_response(embed=embed, view=mv)
        except Exception as e:
            await interaction.followup.send(f"❌ Edit error: {str(e)}", ephemeral=True)
        finally:
            mv.processing = False

# ── Persistent View ───────────────────────────────────────────────────────────

class MatchView(View):
    def __init__(self, winner_team, loser_team, afk_players, url, mode, submitter,
                 changes=None, view_id=None, scrim_mode="3v3"):
        super().__init__(timeout=None)
        self.winner_team  = winner_team
        self.loser_team   = loser_team
        self.afk_players  = list(afk_players)
        self.manual_afk   = set()
        self.url          = url
        self.mode         = mode
        self.scrim_mode   = scrim_mode
        self.submitter    = submitter
        self.changes      = changes or {}
        self.deleted      = False
        self.processing   = False
        self.view_id      = view_id or f"match_{int(time.time()*1000)}"
        self._loser_names = [clean_name(p) for p in self.loser_team if "guest" not in p.lower()]
        self.clear_items()
        self._add_buttons()

    def _add_buttons(self):
        swap = Button(label="🔄 Swap", style=discord.ButtonStyle.secondary, custom_id=f"{self.view_id}_swap")
        swap.callback = self.swap_callback
        self.add_item(swap)
        edit = Button(label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id=f"{self.view_id}_edit")
        edit.callback = self.edit_callback
        self.add_item(edit)
        if self.mode == "ranked":
            for i, name in enumerate(self._loser_names[:3]):
                is_active = name.lower() in self.manual_afk
                label = f"💤 {name}" if not is_active else f"✅ {name} AFK"
                style = discord.ButtonStyle.primary if not is_active else discord.ButtonStyle.success
                btn = Button(label=label, style=style, custom_id=f"{self.view_id}_afk_{i}")
                btn.callback = self._make_afk_callback(i, name)
                self.add_item(btn)
        delete = Button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id=f"{self.view_id}_delete")
        delete.callback = self.delete_callback
        self.add_item(delete)

    def _rebuild_buttons(self):
        self.clear_items()
        self._loser_names = [clean_name(p) for p in self.loser_team if "guest" not in p.lower()]
        self._add_buttons()

    def _get_effective_afk_names(self):
        afk_set = set(clean_name(p).lower() for p in self.afk_players)
        afk_set.update(self.manual_afk)
        names, seen = [], set()
        for p in self.afk_players:
            cn = clean_name(p)
            if cn.lower() in afk_set and cn.lower() not in seen:
                names.append(cn); seen.add(cn.lower())
        for name_lower in self.manual_afk:
            if name_lower not in seen:
                for ln in self._loser_names:
                    if ln.lower() == name_lower: names.append(ln); seen.add(name_lower); break
                else: names.append(name_lower); seen.add(name_lower)
        return names

    def _clean(self):
        cw = [clean_name(p) for p in self.winner_team if "guest" not in p.lower()]
        cl = [clean_name(p) for p in self.loser_team  if "guest" not in p.lower()]
        return cw, cl, self._get_effective_afk_names()

    def _make_afk_callback(self, index, player_name):
        async def callback(interaction: discord.Interaction):
            if not is_bot_admin(interaction.user):
                await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True); return
            if self.deleted:
                await interaction.response.send_message("⚠️ Match deleted / Partida eliminada.", ephemeral=True); return
            if self.processing:
                await interaction.response.send_message("⏳ Processing... / Procesando...", ephemeral=True); return
            self.processing = True
            await interaction.response.defer()
            try:
                cw, cl, ca = self._clean()
                if self.mode == "ranked": await revert_ranked(cw, cl, ca)
                else: await revert_scrims(cw, cl, ca, mode=self.scrim_mode)
                pname_lower = player_name.lower()
                if pname_lower in self.manual_afk: self.manual_afk.remove(pname_lower)
                else: self.manual_afk.add(pname_lower)
                effective_afk = self._get_effective_afk_names()
                if self.mode == "ranked":
                    self.changes, _ = await process_ranked(self.winner_team, self.loser_team, effective_afk, self.url)
                    embed = build_ranked_embed(self.winner_team, self.loser_team, self.changes, effective_afk, self.url, f"By / Por {self.submitter}")
                else:
                    await process_scrims(self.winner_team, self.loser_team, effective_afk, self.url, mode=self.scrim_mode)
                    embed = build_scrim_embed(self.winner_team, self.loser_team, effective_afk, self.url, f"By / Por {self.submitter}", mode=self.scrim_mode)
                self._rebuild_buttons()
                await interaction.edit_original_response(embed=embed, view=self)
            finally:
                self.processing = False
        return callback

    async def swap_callback(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user):
            await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True); return
        if self.deleted:
            await interaction.response.send_message("⚠️ Match deleted / Partida eliminada.", ephemeral=True); return
        if self.processing:
            await interaction.response.send_message("⏳ Processing... / Procesando...", ephemeral=True); return
        self.processing = True
        await interaction.response.defer()
        try:
            cw, cl, ca = self._clean()
            if self.mode == "ranked": await revert_ranked(cw, cl, ca)
            else: await revert_scrims(cw, cl, ca, mode=self.scrim_mode)
            self.winner_team, self.loser_team = self.loser_team, self.winner_team
            self.manual_afk.clear()
            effective_afk = self._get_effective_afk_names()
            if self.mode == "ranked":
                self.changes, _ = await process_ranked(self.winner_team, self.loser_team, effective_afk, self.url)
                embed = build_ranked_embed(self.winner_team, self.loser_team, self.changes, effective_afk, self.url, f"By / Por {self.submitter}")
            else:
                await process_scrims(self.winner_team, self.loser_team, effective_afk, self.url, mode=self.scrim_mode)
                embed = build_scrim_embed(self.winner_team, self.loser_team, effective_afk, self.url, f"By / Por {self.submitter}", mode=self.scrim_mode)
            self._rebuild_buttons()
            await interaction.edit_original_response(embed=embed, view=self)
        finally:
            self.processing = False

    async def edit_callback(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user):
            await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True); return
        if self.deleted:
            await interaction.response.send_message("⚠️ Match deleted / Partida eliminada.", ephemeral=True); return
        if self.processing:
            await interaction.response.send_message("⏳ Processing... / Procesando...", ephemeral=True); return
        await interaction.response.send_modal(EditNamesModal(self))

    async def delete_callback(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user):
            await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True); return
        if self.deleted:
            await interaction.response.send_message("⚠️ Already deleted / Ya eliminada.", ephemeral=True); return
        if self.processing:
            await interaction.response.send_message("⏳ Processing... / Procesando...", ephemeral=True); return
        self.processing = True
        self.deleted = True
        await interaction.response.defer()
        try:
            cw, cl, ca = self._clean()
            if self.mode == "ranked": await revert_ranked(cw, cl, ca)
            else: await revert_scrims(cw, cl, ca, mode=self.scrim_mode)
            for c in self.children: c.disabled = True
            embed = discord.Embed(title="🗑️ Match removed / Partida eliminada", color=0x666666)
            embed.set_thumbnail(url=self.url)
            await interaction.edit_original_response(embed=embed, view=self)
        finally:
            self.processing = False

# ── Bot ───────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
processing_channels = set()

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commands synced")
    except Exception as e:
        print(f"Sync error: {e}")
    try:
        await asyncio.to_thread(cache.load_ranked)
        await asyncio.to_thread(cache.load_scrims)
        await asyncio.to_thread(cache.load_scrims5)
        print(f"[Cache] Loaded {len(cache.ranked)} ranked, {len(cache.scrims)} scrim 3v3, {len(cache.scrims5)} scrim 5v5 players")
    except Exception as e:
        print(f"[Cache] Initial load error: {e}")

@bot.event
async def on_message(message):
    if message.author.bot: return
    ch = message.channel.name
    if ch not in [RANKED_CHANNEL, SCRIMS_3V3_CHANNEL, SCRIMS_5V5_CHANNEL]:
        await bot.process_commands(message); return
    img_att = next((a for a in message.attachments if a.content_type and a.content_type.startswith("image/")), None)
    if not img_att:
        await bot.process_commands(message); return
    if ch == RANKED_CHANNEL:
        mode, scrim_mode, team_size = "ranked", "3v3", 3
    elif ch == SCRIMS_3V3_CHANNEL:
        mode, scrim_mode, team_size = "scrim", "3v3", 3
    else:
        mode, scrim_mode, team_size = "scrim", "5v5", 5

    submitter = message.author.display_name
    if ch in processing_channels:
        await message.reply("⏳ Wait, processing previous match... / Espera, procesando partida anterior...", delete_after=10); return

    processing_channels.add(ch)
    proc = await message.reply("🔍 Analyzing screenshot... Don't send another yet / Analizando... No envíes otra aún")

    try:
        result = await analyze_screenshot(await img_att.read(), team_size=team_size)
        if "error" in result:
            await proc.edit(content=f"❌ {result['error']}"); return
        wt, lt = result["winner_team"], result["loser_team"]
        afk    = result.get("afk_players", [])
        guests = result.get("has_guests", False)

        if mode == "scrim":
            if guests:
                await proc.edit(content="❌ Invalid scrim: Guests not allowed / Sin Guests."); return
            await process_scrims(wt, lt, afk, img_att.url, mode=scrim_mode)
            embed = build_scrim_embed(wt, lt, afk, img_att.url, f"By / Por {submitter}", mode=scrim_mode)
            view  = MatchView(wt, lt, afk, img_att.url, mode, submitter, scrim_mode=scrim_mode)
            await proc.edit(content=None, embed=embed, view=view)
            return

        changes, error = await process_ranked(wt, lt, afk, img_att.url)
        if error:
            await proc.edit(content=f"❌ {error}"); return
        embed = build_ranked_embed(wt, lt, changes, afk, img_att.url, f"By / Por {submitter}")
        view  = MatchView(wt, lt, afk, img_att.url, mode, submitter, changes)
        await proc.edit(content=None, embed=embed, view=view)

    except Exception as e:
        import traceback
        traceback.print_exc()
        await proc.edit(content=f"❌ Error: {str(e)}")
    finally:
        processing_channels.discard(ch)

# ── Slash commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="ranking", description="Top 10 ranked ELO")
async def ranking_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_ranked, 10)
    if not players:
        await interaction.response.send_message("No players yet / No hay jugadores aún."); return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'{i+1}.'} **{p['name']}** — {p['elo']} ELO | {p['rank']} | {p['wins']}W-{p['losses']}L"
             for i, p in enumerate(players)]
    embed = discord.Embed(title="🏆 Top 10 Ranked ELO", description="\n".join(lines), color=0xFFD700)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ranking_scrims", description="Top 10 scrims 3v3")
async def ranking_scrims_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_scrims, 10)
    if not players:
        await interaction.response.send_message("No scrim players yet / No hay jugadores aún."); return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'{i+1}.'} **{p['name']}** — {p['wins']}W-{p['losses']}L ({p['winrate']})"
             for i, p in enumerate(players)]
    embed = discord.Embed(title="⚔️ Top 10 Scrims 3v3", description="\n".join(lines), color=0xFF4444)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ranking_5v5", description="Top 10 scrims 5v5")
async def ranking_5v5_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_scrims5, 10)
    if not players:
        await interaction.response.send_message("No 5v5 scrim players yet / No hay jugadores aún."); return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'{i+1}.'} **{p['name']}** — {p['wins']}W-{p['losses']}L ({p['winrate']})"
             for i, p in enumerate(players)]
    embed = discord.Embed(title="🟣 Top 10 Scrims 5v5", description="\n".join(lines), color=0x8844FF)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="perfil", description="Player profile / Perfil del jugador")
@app_commands.describe(jugador="Player name / Nombre del jugador")
async def perfil_cmd(interaction: discord.Interaction, jugador: str):
    result = await asyncio.to_thread(get_player, jugador)
    sr     = await asyncio.to_thread(get_scrim_player, jugador)
    sr5    = await asyncio.to_thread(get_scrim5_player, jugador)
    if not result and not sr and not sr5:
        await interaction.response.send_message(f"❌ Not found / No encontré a **{jugador}**."); return
    embed = discord.Embed(title=f"👤 Perfil: {jugador}", color=0x00BFFF)
    if result:
        _, d  = result
        total = d["wins"] + d["losses"]
        wr    = f"{(d['wins']/total*100):.1f}%" if total else "N/A"
        streak = (f"🔥 {d['streak']}W racha" if d["streak"] > 0 else
                  f"❄️ {abs(d['streak'])}L racha" if d["streak"] < 0 else "-")
        elo, prog = d["elo"], 0
        for ts, te, *_ in TIERS:
            if ts <= elo <= te: prog = (elo - ts) / (te - ts + 1) * 100; break
        bar = "█" * round(prog / 10) + "░" * (10 - round(prog / 10))
        embed.add_field(name="🏆 Ranked",
            value=f"**{elo}** ELO | {d['rank']}\n{d['wins']}W-{d['losses']}L ({wr})\nStreak: {streak}\n`{bar}` {prog:.0f}%", inline=False)
    if sr:
        _, s = sr
        st = s["wins"] + s["losses"]
        swr = f"{(s['wins']/st*100):.1f}%" if st else "N/A"
        ss = (f"🔥 {s['streak']}W racha" if s["streak"] > 0 else f"❄️ {abs(s['streak'])}L racha" if s["streak"] < 0 else "-")
        embed.add_field(name="⚔️ Scrims 3v3", value=f"{s['wins']}W-{s['losses']}L ({swr})\nStreak: {ss}", inline=False)
    if sr5:
        _, s5 = sr5
        st5 = s5["wins"] + s5["losses"]
        swr5 = f"{(s5['wins']/st5*100):.1f}%" if st5 else "N/A"
        ss5 = (f"🔥 {s5['streak']}W racha" if s5["streak"] > 0 else f"❄️ {abs(s5['streak'])}L racha" if s5["streak"] < 0 else "-")
        embed.add_field(name="🟣 Scrims 5v5", value=f"{s5['wins']}W-{s5['losses']}L ({swr5})\nStreak: {ss5}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="vs", description="Head-to-head / Enfrentamiento directo")
@app_commands.describe(jugador1="Player 1", jugador2="Player 2")
async def vs_cmd(interaction: discord.Interaction, jugador1: str, jugador2: str):
    rw1, rw2 = await asyncio.to_thread(get_h2h, sheets.ws_h2h, jugador1, jugador2)
    sw1, sw2 = await asyncio.to_thread(get_h2h, sheets.ws_scrim_h2h, jugador1, jugador2)
    s5w1, s5w2 = await asyncio.to_thread(get_h2h, sheets.ws_scrim5_h2h, jugador1, jugador2)
    p1, p2 = sorted([jugador1.lower(), jugador2.lower()])
    if not any([rw1, rw2, sw1, sw2, s5w1, s5w2]):
        await interaction.response.send_message(f"❌ No matches between / Sin partidas entre **{jugador1}** y **{jugador2}**."); return
    embed = discord.Embed(title=f"⚔️ {p1} vs {p2}", color=0xFF6600)
    if rw1 or rw2:
        embed.add_field(name="🏆 Ranked", value=f"**{p1}**: {rw1}W\n**{p2}**: {rw2}W\n{rw1+rw2} partidas", inline=True)
    if sw1 or sw2:
        embed.add_field(name="⚔️ Scrims 3v3", value=f"**{p1}**: {sw1}W\n**{p2}**: {sw2}W\n{sw1+sw2} partidas", inline=True)
    if s5w1 or s5w2:
        embed.add_field(name="🟣 Scrims 5v5", value=f"**{p1}**: {s5w1}W\n**{p2}**: {s5w2}W\n{s5w1+s5w2} partidas", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="anular", description="[Admin] Info sobre controles de partida")
async def anular_cmd(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True); return
    await interaction.response.send_message(
        "🔄 **Swap** = invertir ganadores/perdedores (reutilizable)\n"
        "✏️ **Edit** = corregir nombres mal leídos por la IA (abre formulario)\n"
        "💤 **AFK [nombre]** = marcar/desmarcar jugador como AFK (toggle, protege ELO) — solo ranked\n"
        "🗑️ **Delete** = eliminar partida permanentemente\n\n"
        "Swap, Edit y AFK se pueden usar varias veces. Delete es final.", ephemeral=True)

@bot.tree.command(name="cache_reload", description="[Admin] Recargar cache de jugadores")
async def cache_reload_cmd(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user):
        await interaction.response.send_message("⛔ Admins only / Solo admins.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try:
        await asyncio.to_thread(cache.load_ranked)
        await asyncio.to_thread(cache.load_scrims)
        await asyncio.to_thread(cache.load_scrims5)
        await interaction.followup.send(f"✅ Cache recargado: {len(cache.ranked)} ranked, {len(cache.scrims)} scrims 3v3, {len(cache.scrims5)} scrims 5v5.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
