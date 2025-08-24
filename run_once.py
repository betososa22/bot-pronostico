# -*- coding: utf-8 -*-
import os
import time
import random
import asyncio
import httpx
from datetime import datetime, timezone
import pytz

# =======================
# Configuración
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
CHAT_ID = os.getenv("CHAT_ID")
if not BOT_TOKEN or not API_FOOTBALL_KEY or not CHAT_ID:
    raise SystemExit("❌ Faltan BOT_TOKEN y/o API_FOOTBALL_KEY y/o CHAT_ID.")

BASE_URL = "https://v3.football.api-sports.io"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOGOTA_TZ = pytz.timezone("America/Bogota")

SEASON_HIST = int(os.getenv("SEASON_HIST", "2023"))
LAST_N = 10
HALF_LIFE = 5  # recencia

# =======================
# Ligas permitidas
# =======================
ALLOWED_LEAGUE_IDS = {
    239: "Liga BetPlay Dimayor (COL)",  # Colombia - Primera A
    39:  "Premier League (ENG)",        # Inglaterra
    78:  "Bundesliga (GER)",            # Alemania
    61:  "Ligue 1 (FRA)",               # Francia
    135: "Serie A (ITA)",               # Italia
    140: "La Liga (ESP)",               # España
}

def es_liga_permitida(league_id: int) -> bool:
    return league_id in ALLOWED_LEAGUE_IDS

# =======================
# Cliente HTTP asíncrono
# =======================
RATE_SEM = asyncio.Semaphore(2)
async_client = httpx.AsyncClient(
    base_url=BASE_URL,
    headers={"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"},
    timeout=15.0,
)

async def safe_get_async(path: str, params=None, max_retries=5):
    params = params or {}
    async with RATE_SEM:
        for attempt in range(1, max_retries + 1):
            try:
                r = await async_client.get(path, params=params)
                if 200 <= r.status_code < 300:
                    return r
                if r.status_code in (429, 500, 502, 503):
                    await asyncio.sleep(1.5 * attempt + random.random())
                    continue
                return r
            except httpx.RequestError:
                await asyncio.sleep(1.0 * attempt)
        return None

# =======================
# Utilidades
# =======================
def iso_to_bogota_str(iso_str):
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    local_dt = dt.astimezone(BOGOTA_TZ)
    return local_dt.strftime("%H:%M")

async def tg_send_text(text: str):
    """Envía texto a Telegram (se corta en bloques si es largo)."""
    max_len = 3800
    blocks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > max_len:
            blocks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        blocks.append(cur)

    async with httpx.AsyncClient(timeout=15.0) as c:
        for b in blocks:
            await c.post(f"{TELEGRAM_API}/sendMessage", data={"chat_id": CHAT_ID, "text": b})
            await asyncio.sleep(0.3)

# =======================
# Datos de fixtures
# =======================
async def fixtures_por_fecha(fecha_iso_yyyy_mm_dd: str):
    r = await safe_get_async("/fixtures", params={"date": fecha_iso_yyyy_mm_dd, "timezone": "America/Bogota"})
    salida = []
    if r:
        for fx in (r.json() or {}).get("response", []):
            league = fx.get("league", {})
            league_id = league.get("id")
            if not es_liga_permitida(league_id):
                continue
            salida.append({
                "fixture_id": fx["fixture"]["id"],
                "fecha_iso": fx["fixture"]["date"],
                "liga": ALLOWED_LEAGUE_IDS.get(league_id, league.get("name", "Liga")),
                "local_name": fx["teams"]["home"]["name"],
                "visitante_name": fx["teams"]["away"]["name"],
                "local_id": fx["teams"]["home"]["id"],
                "visitante_id": fx["teams"]["away"]["id"],
            })
    return salida

# =======================
# Historial y promedios
# =======================
_FINISHED_STATES = {"FT", "AET", "PEN"}

async def _fetch_team_fixtures_season(team_id: int, season: int):
    r = await safe_get_async("/fixtures", params={"team": team_id, "season": season})
    if not r:
        return []
    try:
        return (r.json() or {}).get("response", [])
    except Exception:
        return []

def _extract_goals_from_fixture(fx):
    goals = fx.get("goals") or {}
    gh, ga = goals.get("home"), goals.get("away")
    if isinstance(gh, int) and isinstance(ga, int):
        return gh, ga
    return None, None

def _last10_from_year_end(rows, team_id: int, season: int, max_j=LAST_N):
    end_ts = int(datetime(season, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    eligibles = []
    for f in rows:
        status = ((f.get("fixture") or {}).get("status") or {}).get("short")
        if status not in _FINISHED_STATES:
            continue
        ts = ((f.get("fixture") or {}).get("timestamp")) or 0
        if not ts or ts > end_ts:
            continue
        gh, ga = _extract_goals_from_fixture(f)
        if gh is None or ga is None:
            continue
        is_home = ((f.get("teams") or {}).get("home") or {}).get("id") == team_id
        gf_equipo = gh if is_home else ga
        eligibles.append((ts, is_home, gf_equipo))
    eligibles.sort(key=lambda x: x[0], reverse=True)
    return eligibles[:max_j]

async def promedio_global(team_id: int, season: int):
    rows = await _fetch_team_fixtures_season(team_id, season)
    last10 = _last10_from_year_end(rows, team_id, season)
    if not last10:
        return 0.0, 0
    goles = [gf for (_, _, gf) in last10]
    return round(sum(goles)/len(goles), 2), len(goles)

# =======================
# Main: construir mensaje
# =======================
async def build_and_send():
    fecha = datetime.now(BOGOTA_TZ).strftime("%Y-%m-%d")
    partidos = await fixtures_por_fecha(fecha)
    if not partidos:
        await tg_send_text(f"📭 No hay partidos para hoy ({fecha}) en las ligas permitidas.")
        return

    bloques = []
    for p in partidos:
        promL, nL = await promedio_global(p["local_id"], SEASON_HIST)
        promV, nV = await promedio_global(p["visitante_id"], SEASON_HIST)
        total_estimado = round(promL + promV, 2) if nL and nV else None
        hora_local = iso_to_bogota_str(p["fecha_iso"])

        msg = [
            f"📅 {fecha} ⏰ {hora_local} - 🏆 {p['liga']}",
            f"⚽ {p['local_name']} vs {p['visitante_name']}",
            f"📊 Promedios GF últimos {LAST_N} (Temp {SEASON_HIST}):",
            f"  - {p['local_name']}: {promL} GF/partido",
            f"  - {p['visitante_name']}: {promV} GF/partido",
        ]
        if total_estimado:
            lado = "Over 2.5" if total_estimado >= 2.5 else "Under 2.5"
            msg.append(f"🔢 Total estimado: **{total_estimado}** goles")
            msg.append(f"💡 Sugerencia: **{lado}**")
        bloques.append("\n".join(msg))
        await asyncio.sleep(0.2)

    header = f"🤖 Pronósticos automáticos — {fecha}\n(Ligas: {', '.join(ALLOWED_LEAGUE_IDS.values())})"
    await tg_send_text(header + "\n\n" + "\n\n".join(bloques))

if __name__ == "__main__":
    asyncio.run(build_and_send())

