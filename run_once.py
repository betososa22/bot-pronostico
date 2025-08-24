# -*- coding: utf-8 -*-
import os
import random
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
import pytz
from typing import Tuple, List, Dict, Any

# =======================
# Configuración
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7500570637:AAEWH2Bdw8STZGoobHfabRpy_DOwwgLjTMY")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "510811e125a60b7e0caba0690fdd6874")
CHAT_ID = os.getenv("CHAT_ID")
if not BOT_TOKEN or not API_FOOTBALL_KEY or not CHAT_ID:
    raise SystemExit("❌ Faltan BOT_TOKEN y/o API_FOOTBALL_KEY y/o CHAT_ID.")

BASE_URL = "https://v3.football.api-sports.io"
BOGOTA_TZ = pytz.timezone("America/Bogota")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SEASON_HIST = int(os.getenv("SEASON_HIST", "2025"))
LAST_N = 10
HALF_LIFE = 5  # recencia (reservado para futuros usos)

# Rango de días a consultar (0 = solo hoy; 1 = hoy+mañana)
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "1"))

# Nuevo: ocultar partidos de HOY que ya pasaron (o están por arrancar ya mismo)
SKIP_PAST_TODAY = os.getenv("SKIP_PAST_TODAY", "1") == "1"
PAST_BUFFER_MIN = int(os.getenv("PAST_BUFFER_MIN", "0"))  # margen adicional

# =======================
# Ligas permitidas
# =======================
ALLOWED_LEAGUE_IDS = {
    239: "Liga BetPlay Dimayor (COL)",  # Colombia - Primera A
    39:  "Premier League (ENG)",        # Inglaterra
    #78:  "Bundesliga (GER)",            # Alemania
    #61:  "Ligue 1 (FRA)",               # Francia
    #135: "Serie A (ITA)",               # Italia
    140: "La Liga (ESP)",               # España
}

def es_liga_permitida(league_id: int) -> bool:
    return league_id in ALLOWED_LEAGUE_IDS

# =======================
# Estado global (cliente HTTP y control de tasa)
# =======================
RATE_SEM = asyncio.Semaphore(2)
async_client: httpx.AsyncClient | None = None  # será asignado en main()

# Cache simple para /fixtures/statistics
_STATS_CACHE: Dict[int, Any] = {}

async def safe_get_async(path: str, params=None, max_retries=5):
    """GET con reintentos y control de tasa usando el cliente global."""
    if async_client is None:
        raise RuntimeError("El cliente HTTP no está inicializado.")
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
def iso_to_bogota_dt(iso_str: str) -> datetime:
    """Convierte ISO utc/‘Z’ a datetime en zona Bogotá."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone(BOGOTA_TZ)

def iso_to_bogota_str(iso_str: str) -> str:
    return iso_to_bogota_dt(iso_str).strftime("%H:%M")

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

def fechas_consulta(dias_ahead: int) -> list[str]:
    """Lista de fechas YYYY-MM-DD desde hoy hasta hoy + dias_ahead (zona Bogotá)."""
    hoy = datetime.now(BOGOTA_TZ).date()
    return [(hoy + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dias_ahead + 1)]

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
# Historial y promedios (goles)
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
    """Devuelve últimos partidos terminados (≤ fin de temporada) con GF del equipo."""
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

def _last10_fixture_ids_from_year_end(rows, team_id: int, season: int, max_j=LAST_N) -> List[int]:
    """Devuelve los fixture_id de los últimos partidos terminados del equipo."""
    end_ts = int(datetime(season, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    eligibles = []
    for f in rows:
        status = ((f.get("fixture") or {}).get("status") or {}).get("short")
        if status not in _FINISHED_STATES:
            continue
        ts = ((f.get("fixture") or {}).get("timestamp")) or 0
        if not ts or ts > end_ts:
            continue
        fx_id = ((f.get("fixture") or {}).get("id"))
        if not isinstance(fx_id, int):
            continue
        home_id = ((f.get("teams") or {}).get("home") or {}).get("id")
        away_id = ((f.get("teams") or {}).get("away") or {}).get("id")
        if home_id == team_id or away_id == team_id:
            eligibles.append((ts, fx_id))
    eligibles.sort(key=lambda x: x[0], reverse=True)
    return [fx_id for _, fx_id in eligibles[:max_j]]

async def promedio_global(team_id: int, season: int) -> Tuple[float, int]:
    rows = await _fetch_team_fixtures_season(team_id, season)
    last10 = _last10_from_year_end(rows, team_id, season)
    if not last10:
        return 0.0, 0
    goles = [gf for (_, _, gf) in last10]
    return round(sum(goles)/len(goles), 2), len(goles)

# =======================
# Estadísticas por fixture (tarjetas y corners)
# =======================
def _extract_cards_from_statistics_block(block: Dict[str, Any]) -> Tuple[int, int]:
    yellow = 0
    red = 0
    for item in block.get("statistics", []):
        t = item.get("type")
        v = item.get("value")
        if v is None or isinstance(v, str):
            continue
        if t == "Yellow Cards":
            yellow = int(v)
        elif t == "Red Cards":
            red = int(v)
    return yellow, red

def _extract_corners_from_statistics_block(block: Dict[str, Any]) -> int:
    for item in block.get("statistics", []):
        t = item.get("type")
        v = item.get("value")
        if t == "Corner Kicks" and v is not None and not isinstance(v, str):
            return int(v)
    return 0

async def _fetch_fixture_statistics(fixture_id: int):
    if fixture_id in _STATS_CACHE:
        return _STATS_CACHE[fixture_id]
    r = await safe_get_async("/fixtures/statistics", params={"fixture": fixture_id})
    data = (r.json() or {}).get("response", []) if r else []
    _STATS_CACHE[fixture_id] = data
    return data

# =======================
# Promedios de tarjetas
# =======================
async def promedio_tarjetas(team_id: int, season: int) -> Tuple[float, float, float, int]:
    rows = await _fetch_team_fixtures_season(team_id, season)
    fx_ids = _last10_fixture_ids_from_year_end(rows, team_id, season)
    if not fx_ids:
        return 0.0, 0.0, 0.0, 0
    total_y = total_r = count = 0
    stats_list = await asyncio.gather(*[_fetch_fixture_statistics(fid) for fid in fx_ids])
    for stats in stats_list:
        block = None
        for b in stats or []:
            team = b.get("team") or {}
            if team.get("id") == team_id:
                block = b
                break
        if not block:
            continue
        y, r = _extract_cards_from_statistics_block(block)
        total_y += y
        total_r += r
        count += 1
    if count == 0:
        return 0.0, 0.0, 0.0, 0
    prom_y = round(total_y / count, 2)
    prom_r = round(total_r / count, 2)
    prom_t = round((total_y + total_r) / count, 2)  # roja = 1 (ajusta si la quieres 2)
    return prom_y, prom_r, prom_t, count

# =======================
# Promedios de corners
# =======================
async def promedio_corners(team_id: int, season: int) -> Tuple[float, int]:
    rows = await _fetch_team_fixtures_season(team_id, season)
    fx_ids = _last10_fixture_ids_from_year_end(rows, team_id, season)
    if not fx_ids:
        return 0.0, 0
    total_c = count = 0
    stats_list = await asyncio.gather(*[_fetch_fixture_statistics(fid) for fid in fx_ids])
    for stats in stats_list:
        block = None
        for b in stats or []:
            team = b.get("team") or {}
            if team.get("id") == team_id:
                block = b
                break
        if not block:
            continue
        total_c += _extract_corners_from_statistics_block(block)
        count += 1
    if count == 0:
        return 0.0, 0
    return round(total_c / count, 2), count

# =======================
# Main: construir y enviar mensaje(s)
# =======================
async def build_and_send():
    fechas = fechas_consulta(DAYS_AHEAD)
    bloques_totales = []

    now_bo = datetime.now(BOGOTA_TZ)
    cutoff = now_bo + timedelta(minutes=PAST_BUFFER_MIN)
    hoy_str = now_bo.date().strftime("%Y-%m-%d")

    for fecha in fechas:
        partidos = await fixtures_por_fecha(fecha)

        # Filtro: si es HOY y está activado, solo partidos cuyo kickoff >= ahora(+buffer)
        if fecha == hoy_str and SKIP_PAST_TODAY:
            partidos = [
                p for p in partidos
                if iso_to_bogota_dt(p["fecha_iso"]) >= cutoff
            ]

        if not partidos:
            if fecha == hoy_str and SKIP_PAST_TODAY:
                bloques_totales.append(f"📭 Para **{fecha}** no quedan partidos por jugar (o entran en {PAST_BUFFER_MIN} min).")
            else:
                bloques_totales.append(f"📭 No hay partidos para **{fecha}** en las ligas permitidas.")
            continue

        bloques = []
        for p in partidos:
            # Promedios de Goles
            promL_gf, nL_gf = await promedio_global(p["local_id"], SEASON_HIST)
            promV_gf, nV_gf = await promedio_global(p["visitante_id"], SEASON_HIST)
            total_estimado = round(promL_gf + promV_gf, 2) if nL_gf and nV_gf else None

            # Promedios de Tarjetas
            promL_y, promL_r, promL_t, nL_cards = await promedio_tarjetas(p["local_id"], SEASON_HIST)
            promV_y, promV_r, promV_t, nV_cards = await promedio_tarjetas(p["visitante_id"], SEASON_HIST)

            # Promedios de Corners
            promL_c, nL_c = await promedio_corners(p["local_id"], SEASON_HIST)
            promV_c, nV_c = await promedio_corners(p["visitante_id"], SEASON_HIST)

            hora_local = iso_to_bogota_str(p["fecha_iso"])

            msg = [
                f"⏰ {hora_local} — 🏆 {p['liga']}",
                f"⚽ {p['local_name']} vs {p['visitante_name']}",
                f"📊 Promedios GF últimos {LAST_N} (Temp {SEASON_HIST}):",
                f"  - {p['local_name']}: {promL_gf} GF/partido",
                f"  - {p['visitante_name']}: {promV_gf} GF/partido",
                f"🟨🟥 Promedios de tarjetas últimos {LAST_N}:",
                f"  - {p['local_name']}: {promL_y} 🟨 | {promL_r} 🟥 | {promL_t} tot.  (n={nL_cards})",
                f"  - {p['visitante_name']}: {promV_y} 🟨 | {promV_r} 🟥 | {promV_t} tot.  (n={nV_cards})",
                f"🚩 Promedios de corners últimos {LAST_N}:",
                f"  - {p['local_name']}: {promL_c} (n={nL_c})",
                f"  - {p['visitante_name']}: {promV_c} (n={nV_c})",
            ]
            if total_estimado is not None:
                lado = "Over 2.5" if total_estimado >= 2.5 else "Under 2.5"
                msg.append(f"🔢 Total estimado (goles): **{total_estimado}**")
                msg.append(f"💡 Sugerencia: **{lado}**")

            bloques.append("\n".join(msg))
            await asyncio.sleep(0.1)

        header = f"📅 **{fecha}**"
        bloques_totales.append(header + "\n" + "\n\n".join(bloques))

    header_global = (
        f"🤖 Pronósticos automáticos — Rango {fechas[0]} a {fechas[-1]}\n"
        f"(Ligas: {', '.join(ALLOWED_LEAGUE_IDS.values())})"
    )
    await tg_send_text(header_global + "\n\n" + "\n\n".join(bloques_totales))

# =======================
# Entry point (cliente HTTP con contexto)
# =======================
async def main():
    global async_client
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"},
        timeout=15.0,
    ) as client:
        async_client = client
        await build_and_send()

if __name__ == "__main__":
    asyncio.run(main())
