"""
León Coach League — Discord Bot v4
Vainglory ranked + scrims tracker powered by Claude Vision + Google Sheets
"""

import os
import io
import json
import base64
import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
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

# Tier table: (elo_start, elo_end, tier_number, match_code)
TIERS = [
    (2400, 2800, 10, "1900"),
    (2160, 2399, 9, "1800"),
    (1920, 2159, 8, "1700"),
    (1680, 1919, 7, "1600"),
]


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_rank(elo):
    elo = max(MIN_ELO, min(MAX_ELO, elo))
    for tier_start, tier_end, tier_num, _ in TIERS:
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
            return f"T{tier_num} {sub}"
    if elo < 1680:
        return "T7 Bronze"
    return "T10 Gold"


def calc_elo(winner_elo, loser_elo):
    expected_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_l = 1 - expected_w
    new_winner = round(winner_elo + K_FACTOR * (1 - expected_w))
    new_loser = round(loser_elo + K_FACTOR * (0 - expected_l))
    return max(MIN_ELO, min(MAX_ELO, new_winner)), max(MIN_ELO, min(MAX_ELO, new_loser))


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
    records = ws_jugadores.get_all_values()
    for i, row in enumerate(records[1:], start=2):
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
    rank = get_rank(STARTING_ELO)
    ws_jugadores.append_row([name, STARTING_ELO, rank, 0, 0, 0, "", ""])


def update_player(row_idx, data):
    ws_jugadores.update(f"A{row_idx}:H{row_idx}", [[
        data["name"], data["elo"], data["rank"],
        data["wins"], data["losses"], data["streak"],
        data["last_rival"], data["last_match"]
    ]])


def update_h2h(player1, player2, winner_name):
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
            return
    w1 = 1 if winner_name.lower() == p1 else 0
    w2 = 1 if winner_name.lower() == p2 else 0
    ws_h2h.append_row([p1, p2, w1, w2])


def log_match(winners, losers, elo_changes, afk_players, capture_url):
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    winners_str = ", ".join(winners)
    losers_str = ", ".join(losers)
    afk_str = ", ".join(afk_players) if afk_players else "No"
    elo_str = json.dumps(elo_changes)
    ws_partidas.append_row([fecha, winners_str, losers_str, elo_str, "", "", "", afk_str, capture_url])


def log_scrim(winner_team, loser_team, capture_url):
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    winners_str = ", ".join(winner_team)
    losers_str = ", ".join(loser_team)
    ws_scrims.append_row([fecha, "Ganador", "Perdedor", "Ganador", winners_str, losers_str, capture_url])


def get_h2h(player1, player2):
    records = ws_h2h.get_all_values()
    p1, p2 = sorted([player1.lower(), player2.lower()])
    for row in records[1:]:
        if row[0].lower() == p1 and row[1].lower() == p2:
            return int(row[2]) if row[2] else 0, int(row[3]) if row[3] else 0
    return 0, 0


def get_top_players(n=10):
    records = ws_jugadores.get_all_values()
    players = []
    for row in records[1:]:
        if row[0] and row[1]:
            try:
                players.append({
                    "name": row[0],
                    "elo": int(row[1]),
                    "rank": row[2],
                    "wins": int(row[3]) if row[3] else 0,
                    "losses": int(row[4]) if row[4] else 0,
                })
            except ValueError:
                continue
    players.sort(key=lambda x: x["elo"], reverse=True)
    return players[:n]


def get_all_scrims():
    records = ws_scrims.get_all_values()
    return records[1:]


# ─── CLAUDE VISION ────────────────────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

VISION_PROMPT = """Analiza esta captura de pantalla de resultado de Vainglory.

Identifica:
1. El equipo ganador y el equipo perdedor:
   - "Victory" (azul) o "Victoria" (azul) = el equipo de la izquierda GANÓ
   - "Defeat" (rojo) o "Derrota" (rojo) = el equipo de la izquierda PERDIÓ
   - "Rendición" / "Surrender": si aparece en AZUL = el equipo izquierda ganó, si en ROJO = perdió
2. Los nombres de TODOS los jugadores (3 por equipo, exactamente como aparecen)
3. Jugadores AFK: tienen su nombre tachado y su personaje aparece más opaco/oscuro

Responde SOLO en este formato JSON exacto, sin texto extra:
{
    "winner_team": ["nombre1", "nombre2", "nombre3"],
    "loser_team": ["nombre1", "nombre2", "nombre3"],
    "afk_players": ["nombre_afk1"],
    "has_guests": true/false
}

IMPORTANTE:
- Los nombres deben ser EXACTOS como aparecen en la captura
- Los nombres pueden tener prefijos como "1600_", "1600-1_", "1800_", "1800-2_" — inclúyelos tal cual
- "Guest" incluye Guest_1234, Guest0, Guest0-Top25, etc.
- afk_players es un array con los nombres de jugadores AFK (puede estar vacío [])
- Si no puedes identificar el resultado, responde: {"error": "No pude leer la captura"}
"""


async def analyze_screenshot(image_bytes):
    compressed_bytes, media_type = compress_image(image_bytes)
    b64 = base64.b64encode(compressed_bytes).decode("utf-8")
    try:
        response = await asyncio.to_thread(
            claude_client.messages.create,
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        return {"error": f"Error al analizar: {str(e)}"}


# ─── PROCESS RANKED ──────────────────────────────────────────────────────────

async def process_ranked(winner_team, loser_team, afk_players, capture_url):
    """Process a ranked match: update ELO for all 6 players."""
    afk_set = {p.lower() for p in afk_players}
    elo_changes = {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Filter out guests
    winners = [p for p in winner_team if "guest" not in p.lower()]
    losers = [p for p in loser_team if "guest" not in p.lower()]

    if not winners or not losers:
        return None, "No se encontraron jugadores válidos (sin Guest)."

    # Get or create all players
    all_names = winners + losers
    player_data = {}

    for name in all_names:
        result = await asyncio.to_thread(get_player, name)
        if not result:
            await asyncio.to_thread(create_player, name)
            result = await asyncio.to_thread(get_player, name)
        player_data[name] = result  # (row_idx, data)

    # Calculate average ELO per team for ELO calculation
    avg_winner_elo = sum(player_data[p][1]["elo"] for p in winners) / len(winners)
    avg_loser_elo = sum(player_data[p][1]["elo"] for p in losers) / len(losers)

    # Calculate ELO change based on team averages
    expected_w = 1 / (1 + 10 ** ((avg_loser_elo - avg_winner_elo) / 400))
    elo_gain = round(K_FACTOR * (1 - expected_w))
    elo_loss = round(K_FACTOR * expected_w)

    # Update winners
    for name in winners:
        idx, data = player_data[name]
        old_elo = data["elo"]
        data["elo"] = min(MAX_ELO, old_elo + elo_gain)
        data["rank"] = get_rank(data["elo"])
        data["wins"] += 1
        data["streak"] = max(1, data["streak"] + 1) if data["streak"] >= 0 else 1
        data["last_match"] = now
        elo_changes[name] = {"old": old_elo, "new": data["elo"], "diff": data["elo"] - old_elo}
        await asyncio.to_thread(update_player, idx, data)

    # Update losers
    for name in losers:
        idx, data = player_data[name]
        old_elo = data["elo"]
        is_afk = name.lower() in afk_set

        if is_afk:
            # AFK: no ELO loss, no defeat counted
            elo_changes[name] = {"old": old_elo, "new": old_elo, "diff": 0, "afk": True}
        else:
            data["elo"] = max(MIN_ELO, old_elo - elo_loss)
            data["rank"] = get_rank(data["elo"])
            data["losses"] += 1
            data["streak"] = min(-1, data["streak"] - 1) if data["streak"] <= 0 else -1
            data["last_match"] = now
            elo_changes[name] = {"old": old_elo, "new": data["elo"], "diff": data["elo"] - old_elo}
            await asyncio.to_thread(update_player, idx, data)

    # Update H2H: each winner vs each non-AFK loser
    for w in winners:
        for l in losers:
            if l.lower() not in afk_set:
                await asyncio.to_thread(update_h2h, w, l, w)

    # Log match
    await asyncio.to_thread(log_match, winners, losers, elo_changes, afk_players, capture_url)

    return elo_changes, None


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
    processing_msg = await message.reply("🔍 Analizando captura...")

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

        # ── SCRIM MODE (no ELO, just record) ──
        if mode == "scrim":
            if has_guests:
                await processing_msg.edit(content="❌ Scrim inválido: todos los jugadores deben tener IGN (sin Guests).")
                return

            await asyncio.to_thread(log_scrim, winner_team, loser_team, image_attachment.url)

            embed = discord.Embed(title="⚔️ Scrim Registrado", color=0xFFD700)
            embed.add_field(name="🏆 Equipo Ganador", value="\n".join(winner_team), inline=True)
            embed.add_field(name="💀 Equipo Perdedor", value="\n".join(loser_team), inline=True)
            if afk_players:
                embed.add_field(name="⚠️ AFK", value=", ".join(afk_players), inline=False)
            embed.set_footer(text=f"Registrado por {submitter}")
            embed.set_thumbnail(url=image_attachment.url)
            await processing_msg.edit(content=None, embed=embed)
            return

        # ── RANKED MODE (ELO for all 6) ──
        elo_changes, error = await process_ranked(winner_team, loser_team, afk_players, image_attachment.url)

        if error:
            await processing_msg.edit(content=f"❌ {error}")
            return

        # Build embed
        embed = discord.Embed(title="🏆 Partida Ranked Registrada", color=0x00FF88)

        # Winners column
        winner_lines = []
        for name in winner_team:
            if "guest" in name.lower():
                continue
            ch = elo_changes.get(name, {})
            diff = ch.get("diff", 0)
            new_elo = ch.get("new", 0)
            rank = get_rank(new_elo)
            winner_lines.append(f"**{name}**\n{ch.get('old', 0)} → {new_elo} (+{diff}) | {rank}")

        # Losers column
        loser_lines = []
        for name in loser_team:
            if "guest" in name.lower():
                continue
            ch = elo_changes.get(name, {})
            if ch.get("afk"):
                loser_lines.append(f"**{name}** ⚠️ AFK\n{ch.get('old', 0)} (sin cambio)")
            else:
                diff = ch.get("diff", 0)
                new_elo = ch.get("new", 0)
                rank = get_rank(new_elo)
                loser_lines.append(f"**{name}**\n{ch.get('old', 0)} → {new_elo} ({diff}) | {rank}")

        embed.add_field(name="👑 Ganadores", value="\n\n".join(winner_lines) if winner_lines else "—", inline=True)
        embed.add_field(name="💀 Perdedores", value="\n\n".join(loser_lines) if loser_lines else "—", inline=True)

        # Guest notice
        guests = [p for p in winner_team + loser_team if "guest" in p.lower()]
        if guests:
            embed.add_field(name="👤 Guests (ignorados)", value=", ".join(guests), inline=False)

        embed.set_thumbnail(url=image_attachment.url)
        embed.set_footer(text=f"Subido por {submitter}")
        await processing_msg.edit(content=None, embed=embed)

    except Exception as e:
        await processing_msg.edit(content=f"❌ Error: {str(e)}")

    await bot.process_commands(message)


# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

@bot.tree.command(name="ranking", description="Top 10 jugadores por ELO")
async def ranking_cmd(interaction: discord.Interaction):
    players = await asyncio.to_thread(get_top_players, 10)
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
    result = await asyncio.to_thread(get_player, jugador)
    if not result:
        await interaction.response.send_message(f"❌ No encontré a **{jugador}**. Verifica el nombre.")
        return
    _, data = result
    total = data["wins"] + data["losses"]
    winrate = f"{(data['wins']/total*100):.1f}%" if total > 0 else "N/A"
    if data["streak"] > 0:
        streak_text = f"🔥 {data['streak']}W"
    elif data["streak"] < 0:
        streak_text = f"❄️ {abs(data['streak'])}L"
    else:
        streak_text = "—"
    elo = data["elo"]
    tier_code = get_tier_code(elo)
    progress_in_tier = 0
    for tier_start, tier_end, tier_num, code in TIERS:
        if tier_start <= elo <= tier_end:
            progress_in_tier = ((elo - tier_start) / (tier_end - tier_start + 1)) * 100
            break
    bar_filled = round(progress_in_tier / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    embed = discord.Embed(title=f"📊 Perfil de {data['name']}", color=0x00BFFF)
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
    w1, w2 = await asyncio.to_thread(get_h2h, jugador1, jugador2)
    p1, p2 = sorted([jugador1.lower(), jugador2.lower()])
    if w1 == 0 and w2 == 0:
        await interaction.response.send_message(f"No hay partidas registradas entre **{jugador1}** y **{jugador2}**.")
        return
    total = w1 + w2
    embed = discord.Embed(title=f"⚔️ {p1} vs {p2}", color=0xFF6600)
    embed.add_field(name=p1, value=f"**{w1}** victorias", inline=True)
    embed.add_field(name="vs", value=f"{total} partidas", inline=True)
    embed.add_field(name=p2, value=f"**{w2}** victorias", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="scrims", description="Historial de scrims")
async def scrims_cmd(interaction: discord.Interaction):
    records = await asyncio.to_thread(get_all_scrims)
    if not records:
        await interaction.response.send_message("No hay scrims registrados aún.")
        return
    embed = discord.Embed(title="⚔️ Historial de Scrims", color=0xFF4444)
    recent = records[-10:]
    lines = []
    for row in recent:
        fecha = row[0] if row[0] else "?"
        jugadores_a = row[4] if len(row) > 4 else "?"
        jugadores_b = row[5] if len(row) > 5 else "?"
        lines.append(f"**{fecha}**\n🏆 {jugadores_a}\n💀 {jugadores_b}")
    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Mostrando últimos {len(recent)} scrims")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="anular", description="[Admin] Anular última partida de un jugador")
@app_commands.describe(jugador="Nombre del jugador", razon="Razón de la anulación")
async def anular_cmd(interaction: discord.Interaction, jugador: str, razon: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores pueden usar este comando.", ephemeral=True)
        return
    await interaction.response.send_message(f"⚠️ Función de anulación en desarrollo. Contacta a un admin para ajustes manuales en Google Sheets.\nJugador: **{jugador}** | Razón: {razon}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
