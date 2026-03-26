"""
León Coach League — Discord Bot
Vainglory ranked + scrims tracker powered by Claude Vision + Google Sheets
"""

import os
import io
import json
import math
import base64
import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
import anthropic

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

# Google credentials from env var (JSON string)
google_creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])

# Channel names
RANKED_CHANNEL = "ranked"
SCRIMS_CHANNEL = "scrims"
RECLAMOS_CHANNEL = "reclamos"

# ELO config
K_FACTOR = 32
STARTING_ELO = 1680
MIN_ELO = 0
MAX_ELO = 2800

# Tier table
TIERS = [
    (2400, 2800, "Vainglorious", "1900"),
    (2160, 2399, "Pinnacle of Awesome", "1800"),
    (1920, 2159, "Simply Amazing", "1700"),
    (1680, 1919, "The Hotness", "1600"),
]

SUBDIVISIONS = ["Bronze", "Silver", "Gold"]


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_rank(elo):
    """Return rank string from ELO."""
    elo = max(MIN_ELO, min(MAX_ELO, elo))
    for tier_start, tier_end, tier_name, _ in TIERS:
        if tier_start <= elo <= tier_end:
            tier_range = tier_end - tier_start + 1
            sub_size = tier_range / 3
            offset = elo - tier_start
            if offset < sub_size:
                sub = "Bronze"
            elif offset < sub_size * 2:
                sub = "Silver"
            else:
                sub = "Gold"
            return f"{tier_name} {sub}"
    if elo < 1680:
        return "The Hotness Bronze"
    return "Vainglorious Gold"


def calc_elo(winner_elo, loser_elo):
    """Calculate new ELO ratings after a match."""
    expected_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_l = 1 - expected_w
    new_winner = round(winner_elo + K_FACTOR * (1 - expected_w))
    new_loser = round(loser_elo + K_FACTOR * (0 - expected_l))
    new_winner = max(MIN_ELO, min(MAX_ELO, new_winner))
    new_loser = max(MIN_ELO, min(MAX_ELO, new_loser))
    return new_winner, new_loser


def get_tier_code(elo):
    """Get tier match code from ELO."""
    for tier_start, _, _, code in TIERS:
        if elo >= tier_start:
            return code
    return "1600"


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_info(google_creds_json, scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)

ws_jugadores = spreadsheet.worksheet("Jugadores")
ws_partidas = spreadsheet.worksheet("Partidas")
ws_h2h = spreadsheet.worksheet("HeadToHead")
ws_scrims = spreadsheet.worksheet("Scrims")


def get_player(name):
    """Find player row by name (case-insensitive). Returns (row_index, data) or None."""
    records = ws_jugadores.get_all_values()
    for i, row in enumerate(records[1:], start=2):  # skip header
        if row[0].lower() == name.lower():
            return i, {
                "name": row[0],
                "elo": int(row[1]) if row[1] else STARTING_ELO,
                "rank": row[2],
                "wins": int(row[3]) if row[3] else 0,
                "losses": int(row[4]) if row[4] else 0,
                "streak": int(row[5]) if row[5] else 0,
                "last_rival": row[6],
                "last_match": row[7],
            }
    return None


def create_player(name):
    """Create a new player with starting ELO."""
    rank = get_rank(STARTING_ELO)
    ws_jugadores.append_row([name, STARTING_ELO, rank, 0, 0, 0, "", ""])
    return {
        "name": name,
        "elo": STARTING_ELO,
        "rank": rank,
        "wins": 0,
        "losses": 0,
        "streak": 0,
        "last_rival": "",
        "last_match": "",
    }


def update_player(row_idx, data):
    """Update a player row."""
    ws_jugadores.update(f"A{row_idx}:H{row_idx}", [[
        data["name"], data["elo"], data["rank"],
        data["wins"], data["losses"], data["streak"],
        data["last_rival"], data["last_match"]
    ]])


def update_h2h(player1, player2, winner_name):
    """Update head-to-head record."""
    records = ws_h2h.get_all_values()
    p1, p2 = sorted([player1.lower(), player2.lower()])

    for i, row in enumerate(records[1:], start=2):
        if row[0].lower() == p1 and row[1].lower() == p2:
            w1 = int(row[2]) if row[2] else 0
            w2 = int(row[3]) if row[3] else 0
            if winner_name.lower() == p1:
                w1 += 1
            else:
                w2 += 1
            ws_h2h.update(f"C{i}:D{i}", [[w1, w2]])
            return w1, w2

    # New H2H record
    w1 = 1 if winner_name.lower() == p1 else 0
    w2 = 1 if winner_name.lower() == p2 else 0
    ws_h2h.append_row([p1, p2, w1, w2])
    return w1, w2


def log_match(winner, loser, w_elo, l_elo, w_rank, l_rank, afk, capture_url):
    """Log a match to the Partidas sheet."""
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws_partidas.append_row([fecha, winner, loser, w_elo, l_elo, w_rank, l_rank, afk, capture_url])


def log_scrim(team_a, team_b, winner, players_a, players_b, capture_url):
    """Log a scrim to the Scrims sheet."""
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws_scrims.append_row([fecha, team_a, team_b, winner, players_a, players_b, capture_url])


def get_h2h(player1, player2):
    """Get H2H record between two players."""
    records = ws_h2h.get_all_values()
    p1, p2 = sorted([player1.lower(), player2.lower()])
    for row in records[1:]:
        if row[0].lower() == p1 and row[1].lower() == p2:
            return int(row[2]) if row[2] else 0, int(row[3]) if row[3] else 0
    return 0, 0


def get_top_players(n=10):
    """Get top N players by ELO."""
    records = ws_jugadores.get_all_values()
    players = []
    for row in records[1:]:
        if row[0] and row[1]:
            players.append({
                "name": row[0],
                "elo": int(row[1]),
                "rank": row[2],
                "wins": int(row[3]) if row[3] else 0,
                "losses": int(row[4]) if row[4] else 0,
            })
    players.sort(key=lambda x: x["elo"], reverse=True)
    return players[:n]


def get_all_scrims():
    """Get all scrim results."""
    records = ws_scrims.get_all_values()
    return records[1:]


def get_last_match(player_name):
    """Get the last match for a player to enable /anular."""
    records = ws_partidas.get_all_values()
    for row in reversed(records[1:]):
        if row[1].lower() == player_name.lower() or row[2].lower() == player_name.lower():
            return row
    return None


def revert_last_match(player_name):
    """Revert the last match involving a player."""
    records = ws_partidas.get_all_values()
    for i in range(len(records) - 1, 0, -1):
        row = records[i]
        if row[1].lower() == player_name.lower() or row[2].lower() == player_name.lower():
            winner_name = row[1]
            loser_name = row[2]
            w_elo_after = int(row[3]) if row[3] else 0
            l_elo_after = int(row[4]) if row[4] else 0
            afk = row[7].lower() == "sí" if row[7] else False

            # Get players
            w_result = get_player(winner_name)
            l_result = get_player(loser_name)

            if w_result and l_result:
                w_idx, w_data = w_result
                l_idx, l_data = l_result

                # Reverse ELO: recalculate what the old ELOs were
                old_w_expected = 1 / (1 + 10 ** ((l_elo_after - w_elo_after) / 400))
                elo_gained = round(K_FACTOR * (1 - old_w_expected))
                reversed_w_elo = w_data["elo"] - elo_gained
                reversed_l_elo = l_data["elo"] + elo_gained

                w_data["elo"] = max(MIN_ELO, min(MAX_ELO, reversed_w_elo))
                l_data["elo"] = max(MIN_ELO, min(MAX_ELO, reversed_l_elo))
                w_data["rank"] = get_rank(w_data["elo"])
                l_data["rank"] = get_rank(l_data["elo"])
                w_data["wins"] = max(0, w_data["wins"] - 1)
                l_data["losses"] = max(0, l_data["losses"] - 1)

                update_player(w_idx, w_data)
                update_player(l_idx, l_data)

                # Remove H2H if not AFK
                if not afk:
                    h2h_records = ws_h2h.get_all_values()
                    p1, p2 = sorted([winner_name.lower(), loser_name.lower()])
                    for j, h_row in enumerate(h2h_records[1:], start=2):
                        if h_row[0].lower() == p1 and h_row[1].lower() == p2:
                            hw = int(h_row[2]) if h_row[2] else 0
                            hl = int(h_row[3]) if h_row[3] else 0
                            if winner_name.lower() == p1:
                                hw = max(0, hw - 1)
                            else:
                                hl = max(0, hl - 1)
                            ws_h2h.update(f"C{j}:D{j}", [[hw, hl]])
                            break

            # Delete the match row
            ws_partidas.delete_rows(i + 1)
            return winner_name, loser_name
    return None


# ─── CLAUDE VISION ────────────────────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

RANKED_PROMPT = """Analiza esta captura de pantalla de resultado de Vainglory.

Identifica:
1. El equipo ganador (izquierda o derecha) — el equipo ganador tiene el indicador de victoria (crown/trophy/✓)
2. Los nombres de TODOS los jugadores en ambos equipos (exactamente como aparecen)
3. Si algún jugador tiene "Guest" en su nombre

Responde SOLO en este formato JSON exacto, sin texto extra:
{
    "winner_team": ["nombre1", "nombre2", "nombre3"],
    "loser_team": ["nombre1", "nombre2", "nombre3"],
    "has_guests": true/false
}

IMPORTANTE:
- Los nombres deben ser EXACTOS como aparecen en la captura
- "Guest" incluye cualquier variación como Guest_1234, Guest, etc.
- Si no puedes identificar el resultado claramente, responde: {"error": "No pude leer la captura"}
"""

SCRIM_PROMPT = """Analiza esta captura de pantalla de resultado de Vainglory (scrim de equipos).

Identifica:
1. El equipo ganador (izquierda o derecha) — el equipo ganador tiene el indicador de victoria
2. Los nombres de TODOS los jugadores en ambos equipos (exactamente como aparecen)
3. Si algún jugador tiene "Guest" en su nombre

Responde SOLO en este formato JSON exacto, sin texto extra:
{
    "winner_team": ["nombre1", "nombre2", "nombre3"],
    "loser_team": ["nombre1", "nombre2", "nombre3"],
    "has_guests": true/false
}

IMPORTANTE:
- Los nombres deben ser EXACTOS como aparecen
- En scrims, los 6 jugadores DEBEN tener IGN real (sin Guests)
- Si hay algún Guest, responde: {"error": "Scrim inválido: hay jugadores Guest"}
- Si no puedes leer la captura, responde: {"error": "No pude leer la captura"}
"""


async def analyze_screenshot(image_bytes, mode="ranked"):
    """Send screenshot to Claude Vision and get match result."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = RANKED_PROMPT if mode == "ranked" else SCRIM_PROMPT

    try:
        response = claude_client.messages.create(
            model="claude-opus-4-5-20250514",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = response.content[0].text.strip()
        # Clean JSON if wrapped in code block
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        return {"error": f"Error al analizar: {str(e)}"}


# ─── DISCORD BOT ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ {bot.user} está online")
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} comandos sincronizados")
    except Exception as e:
        print(f"❌ Error sync: {e}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    channel_name = message.channel.name

    # Only process in ranked/scrims channels
    if channel_name not in [RANKED_CHANNEL, SCRIMS_CHANNEL]:
        await bot.process_commands(message)
        return

    # Must have an image attachment
    image_attachment = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_attachment = att
            break

    if not image_attachment:
        await bot.process_commands(message)
        return

    # Check for AFK flag
    is_afk = "afk" in message.content.lower()
    mode = "ranked" if channel_name == RANKED_CHANNEL else "scrim"
    submitter = message.author.display_name

    # Processing message
    processing_msg = await message.reply("🔍 Analizando captura...")

    try:
        # Download image
        img_bytes = await image_attachment.read()

        # Analyze with Claude Vision
        result = await asyncio.to_thread(analyze_screenshot, img_bytes, mode)

        if "error" in result:
            await processing_msg.edit(content=f"❌ {result['error']}")
            return

        winner_team = result["winner_team"]
        loser_team = result["loser_team"]
        has_guests = result.get("has_guests", False)

        # ── SCRIM MODE ──
        if mode == "scrim":
            if has_guests:
                await processing_msg.edit(content="❌ Scrim inválido: todos los jugadores deben tener IGN (sin Guests).")
                return

            # Determine teams by submitter
            team_a_names = ", ".join(winner_team)
            team_b_names = ", ".join(loser_team)

            # Log scrim
            log_scrim(
                "Equipo A", "Equipo B", "Equipo A",
                team_a_names, team_b_names,
                image_attachment.url
            )

            embed = discord.Embed(
                title="⚔️ Scrim Registrado",
                color=0xFFD700,
            )
            embed.add_field(name="🏆 Equipo Ganador", value=team_a_names, inline=False)
            embed.add_field(name="💀 Equipo Perdedor", value=team_b_names, inline=False)
            embed.set_footer(text=f"Registrado por {submitter}")
            embed.set_thumbnail(url=image_attachment.url)

            await processing_msg.edit(content=None, embed=embed)
            return

        # ── RANKED MODE ──
        # Find the submitter in the match
        all_players = winner_team + loser_team
        submitter_in_match = None

        for p in all_players:
            if p.lower() == submitter.lower():
                submitter_in_match = p
                break

        # Determine winner/loser from submitter perspective
        if submitter_in_match:
            if submitter_in_match in winner_team:
                winner_name = submitter_in_match
                # Find opponent (non-guest, non-submitter)
                loser_name = None
                for p in loser_team:
                    if "guest" not in p.lower():
                        loser_name = p
                        break
            else:
                loser_name = submitter_in_match
                winner_name = None
                for p in winner_team:
                    if "guest" not in p.lower():
                        winner_name = p
                        break
        else:
            # Submitter not found by display name — use first non-guest from each team
            winner_name = None
            loser_name = None
            for p in winner_team:
                if "guest" not in p.lower():
                    winner_name = p
                    break
            for p in loser_team:
                if "guest" not in p.lower():
                    loser_name = p
                    break

        if not winner_name or not loser_name:
            await processing_msg.edit(content="❌ No pude identificar a los jugadores. Verifica que los nombres sean correctos.")
            return

        # Get or create players
        w_result = get_player(winner_name)
        if w_result:
            w_idx, w_data = w_result
        else:
            create_player(winner_name)
            w_result = get_player(winner_name)
            w_idx, w_data = w_result

        l_result = get_player(loser_name)
        if l_result:
            l_idx, l_data = l_result
        else:
            create_player(loser_name)
            l_result = get_player(loser_name)
            l_idx, l_data = l_result

        # Calculate new ELO
        old_w_elo = w_data["elo"]
        old_l_elo = l_data["elo"]
        new_w_elo, new_l_elo = calc_elo(old_w_elo, old_l_elo)

        # Update winner
        w_data["elo"] = new_w_elo
        w_data["rank"] = get_rank(new_w_elo)
        w_data["wins"] += 1
        w_data["streak"] = max(1, w_data["streak"] + 1) if w_data["streak"] >= 0 else 1
        w_data["last_rival"] = loser_name
        w_data["last_match"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        update_player(w_idx, w_data)

        # Update loser
        l_data["elo"] = new_l_elo
        l_data["rank"] = get_rank(new_l_elo)
        l_data["losses"] += 1
        l_data["streak"] = min(-1, l_data["streak"] - 1) if l_data["streak"] <= 0 else -1
        l_data["last_rival"] = winner_name
        l_data["last_match"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        update_player(l_idx, l_data)

        # Update H2H (skip if AFK)
        if not is_afk:
            update_h2h(winner_name, loser_name, winner_name)

        # Log match
        log_match(
            winner_name, loser_name,
            new_w_elo, new_l_elo,
            w_data["rank"], l_data["rank"],
            "Sí" if is_afk else "No",
            image_attachment.url
        )

        # Build response embed
        w_diff = new_w_elo - old_w_elo
        l_diff = new_l_elo - old_l_elo

        embed = discord.Embed(
            title="🏆 Partida Ranked Registrada",
            color=0x00FF88,
        )
        embed.add_field(
            name=f"👑 {winner_name}",
            value=f"**{old_w_elo}** → **{new_w_elo}** (+{w_diff})\n{w_data['rank']}",
            inline=True,
        )
        embed.add_field(
            name=f"💀 {loser_name}",
            value=f"**{old_l_elo}** → **{new_l_elo}** ({l_diff})\n{l_data['rank']}",
            inline=True,
        )

        if is_afk:
            embed.add_field(name="⚠️ AFK", value="No cuenta para H2H", inline=False)

        embed.set_thumbnail(url=image_attachment.url)
        embed.set_footer(text=f"Subido por {submitter}")

        await processing_msg.edit(content=None, embed=embed)

    except Exception as e:
        await processing_msg.edit(content=f"❌ Error: {str(e)}")

    await bot.process_commands(message)


# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

@bot.tree.command(name="ranking", description="Top 10 jugadores por ELO")
async def ranking_cmd(interaction: discord.Interaction):
    players = get_top_players(10)
    if not players:
        await interaction.response.send_message("No hay jugadores registrados aún.")
        return

    embed = discord.Embed(title="🏆 Top 10 — Ranking ELO", color=0xFFD700)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(players):
        prefix = medals[i] if i < 3 else f"**{i+1}.**"
        record = f"{p['wins']}W - {p['losses']}L"
        lines.append(f"{prefix} **{p['name']}** — {p['elo']} ELO | {p['rank']} | {record}")

    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="perfil", description="Ver perfil de un jugador")
@app_commands.describe(jugador="Nombre del jugador")
async def perfil_cmd(interaction: discord.Interaction, jugador: str):
    result = get_player(jugador)
    if not result:
        await interaction.response.send_message(f"❌ No encontré a **{jugador}**. Verifica el nombre.")
        return

    _, data = result

    total = data["wins"] + data["losses"]
    winrate = f"{(data['wins']/total*100):.1f}%" if total > 0 else "N/A"

    streak_text = ""
    if data["streak"] > 0:
        streak_text = f"🔥 {data['streak']}W"
    elif data["streak"] < 0:
        streak_text = f"❄️ {abs(data['streak'])}L"
    else:
        streak_text = "—"

    # ELO bar
    elo = data["elo"]
    tier_code = get_tier_code(elo)
    progress_in_tier = 0
    for tier_start, tier_end, tier_name, code in TIERS:
        if tier_start <= elo <= tier_end:
            progress_in_tier = ((elo - tier_start) / (tier_end - tier_start + 1)) * 100
            break

    bar_filled = round(progress_in_tier / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    embed = discord.Embed(
        title=f"📊 Perfil de {data['name']}",
        color=0x00BFFF,
    )
    embed.add_field(name="ELO", value=f"**{elo}**", inline=True)
    embed.add_field(name="Rango", value=data["rank"], inline=True)
    embed.add_field(name="Match Code", value=f"`{tier_code}_{data['name']}`", inline=True)
    embed.add_field(name="Récord", value=f"{data['wins']}W - {data['losses']}L ({winrate})", inline=True)
    embed.add_field(name="Racha", value=streak_text, inline=True)
    embed.add_field(name="Último rival", value=data["last_rival"] or "—", inline=True)
    embed.add_field(name="Progreso en tier", value=f"`{bar}` {progress_in_tier:.0f}%", inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="vs", description="Récord entre dos jugadores")
@app_commands.describe(jugador1="Primer jugador", jugador2="Segundo jugador")
async def vs_cmd(interaction: discord.Interaction, jugador1: str, jugador2: str):
    w1, w2 = get_h2h(jugador1, jugador2)
    p1, p2 = sorted([jugador1.lower(), jugador2.lower()])

    if w1 == 0 and w2 == 0:
        await interaction.response.send_message(f"No hay partidas registradas entre **{jugador1}** y **{jugador2}**.")
        return

    total = w1 + w2

    embed = discord.Embed(
        title=f"⚔️ {p1} vs {p2}",
        color=0xFF6600,
    )
    embed.add_field(name=p1, value=f"**{w1}** victorias", inline=True)
    embed.add_field(name="vs", value=f"{total} partidas", inline=True)
    embed.add_field(name=p2, value=f"**{w2}** victorias", inline=True)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="scrims", description="Tabla de resultados de scrims")
async def scrims_cmd(interaction: discord.Interaction):
    records = get_all_scrims()
    if not records:
        await interaction.response.send_message("No hay scrims registrados aún.")
        return

    embed = discord.Embed(title="⚔️ Historial de Scrims", color=0xFF4444)

    # Show last 10 scrims
    recent = records[-10:]
    lines = []
    for row in recent:
        fecha = row[0] if row[0] else "?"
        ganador = row[3] if row[3] else "?"
        jugadores_a = row[4] if row[4] else "?"
        jugadores_b = row[5] if row[5] else "?"
        lines.append(f"**{fecha}** — 🏆 {ganador}\n┗ {jugadores_a} vs {jugadores_b}")

    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Mostrando últimos {len(recent)} scrims")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="anular", description="[Admin] Anular última partida de un jugador")
@app_commands.describe(jugador="Nombre del jugador", razon="Razón de la anulación")
async def anular_cmd(interaction: discord.Interaction, jugador: str, razon: str):
    # Check admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores pueden usar este comando.", ephemeral=True)
        return

    result = revert_last_match(jugador)
    if result:
        winner, loser = result
        embed = discord.Embed(
            title="🔄 Partida Anulada",
            color=0xFF0000,
        )
        embed.add_field(name="Partida", value=f"{winner} vs {loser}", inline=False)
        embed.add_field(name="Razón", value=razon, inline=False)
        embed.set_footer(text=f"Anulada por {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message(f"❌ No encontré partidas recientes de **{jugador}**.")


# ─── RUN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
