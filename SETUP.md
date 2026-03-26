# León Coach League — Setup Guide

## Variables de entorno (Railway)

Configura estas 4 variables en Railway > Variables:

| Variable | Valor |
|---|---|
| `DISCORD_TOKEN` | El token del bot de Discord |
| `ANTHROPIC_API_KEY` | Tu API key de Anthropic (sk-ant-...) |
| `GOOGLE_SHEET_ID` | El ID de tu Google Sheet (de la URL) |
| `GOOGLE_CREDENTIALS_JSON` | El contenido COMPLETO del archivo .json de Google |

## Para GOOGLE_CREDENTIALS_JSON:

1. Abre el archivo .json que descargaste de Google Cloud
2. Copia TODO el contenido (es un JSON largo)
3. Pégalo como valor de la variable en Railway

## Canales de Discord necesarios:

- `#ranked` — jugadores suben capturas de partidas individuales
- `#scrims` — capitanes suben capturas de scrims
- `#reclamos` — jugadores piden anulaciones

## Comandos del bot:

- `/ranking` — Top 10 por ELO
- `/perfil [jugador]` — Stats de un jugador
- `/vs [jugador1] [jugador2]` — Récord entre dos jugadores
- `/scrims` — Historial de scrims
- `/anular [jugador] [razón]` — (Admin) Revierte última partida

## Cómo funciona:

1. Jugador sube captura en #ranked o #scrims
2. El bot envía la imagen a Claude Vision
3. Claude identifica ganador, perdedor y nombres
4. El bot actualiza ELO, H2H y Google Sheets
5. El bot responde con un embed mostrando el resultado

## Reglas:

- Para marcar AFK: escribe "afk" junto con la captura
- En ranked: se ignoran los Guest, la partida cuenta para quien la subió
- En scrims: no se permiten Guest, los 6 jugadores deben tener IGN
