# -*- coding: utf-8 -*-
import os
import time
import random
import asyncio
import httpx
from datetime import datetime, timezone
import pytz

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =======================
# Configuración y Tokens
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7500570637:AAEWH2Bdw8STZGoobHfabRpy_DOwwgLjTMY")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "510811e125a60b7e0caba0690fdd6874")
if not BOT_TOKEN or not API_FOOTBALL_KEY:
    raise SystemExit("❌ Faltan variables de entorno BOT_TOKEN y/o API_FOOTBALL_KEY.")

BASE_URL = "https://v3.football.api-sports.io"
BOGOTA_TZ = pytz.timezone("America/Bogota")

# Temporada historial (por defecto 2023)
SEASON_HIST = int(os.getenv("SEASON_HIST", "2023"))

# Config de históricos
LAST_N = 10           # últimos N terminados
HALF_LIFE = 5         # media-vida para recencia (None para desactivar)

# =======================
# Ligas permitidas (solo estas se muestran en /hoy, /pronostico, etc.)
# =======================
ALLOWED_LEAGUE_IDS = {
    239: "Liga BetPlay Dimayor (COL)",  # Colombia - Primera A
    140: "La Liga (ESP)",               # España - Primera División
    #39:  "Premier League (ENG)",        # Inglaterra
    #78:  "Bundesliga (GER)",            # Alemania
    #61:  "Ligue 1 (FRA)",               # Francia
    #135: "Serie A (ITA)",               # Italia
    # agrega más si quieres
}

def es_liga_permitida(league_id: int) -> bool:
    return league_id in ALLOWED_LEAGUE_IDS

# Concurrencia / rate-limit simple
RATE_CONCURRENCY = int(os.getenv("RATE_CONCURRENCY", "2"))
RATE_SEM = asyncio.Semaphore(RATE_CONCURRENCY)

# =======================
# Cliente HTTP asíncrono
# =======================
async_client = httpx.AsyncClient(
    base_url=BASE_URL,
    headers={
        "x-apisports-key": API_FOOTBALL_KEY,
        "Accept": "application/json",
        "User-Agent": "apifootball-telegram-bot/1.0",
    },
    timeout=15.0,
)

async def safe_get_async(path: str, params=None, max_retries=5):
    """
    GET con control de concurrencia, manejo de 429 y backoff con jitter.
    Devuelve httpx.Response o un Response 599 "sintético" con flag ._rate_info['limited']=True.
    """
    params = params or {}
    async with RATE_SEM:
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                r = await async_client.get(path, params=params)
                if 200 <= r.status_code < 300:
                    r._rate_info = {"limited": False}
                    return r

                if r.status_code == 429:
                    reset = r.headers.get("x-ratelimit-reset")
                    if reset:
                        try:
                            wait_s = max(0.0, float(reset) - time.time()) + 0.25
                        except Exception:
                            wait_s = 2.0 * attempt
                    else:
                        wait_s = 2.0 * attempt
                    await asyncio.sleep(wait_s)
                    continue

                if 500 <= r.status_code < 600:
                    await asyncio.sleep(1.2 * attempt + random.random() * 0.3)
                    continue

                r._rate_info = {"limited": False}
                return r

            except httpx.RequestError as e:
                last_exc = e
                await asyncio.sleep(0.8 * attempt + random.random() * 0.3)

        dummy = httpx.Response(599, request=None)
        dummy._rate_info = {"limited": True, "error": str(last_exc) if last_exc else "retry_exhausted"}
        return dummy

# =======================
# Cache simple
# =======================
_cache = {}  # key -> (value, ts)
def cache_get(key, ttl=60):
    data = _cache.get(key)
    if not data:
        return None
    value, ts = data
    if time.time() - ts <= ttl:
        return value
    _cache.pop(key, None)
    return None

def cache_set(key, value):
    _cache[key] = (value, time.time())

# =======================
# Utilidades
# =======================
TELEGRAM_CHAR_LIMIT = 4000

async def send_blocks(update: Update, bloques: list[str], sep: str = "\n\n", prefix: str = "", suffix: str = ""):
    current = prefix
    for b in bloques:
        if len(current) + len(b) + len(sep) + len(suffix) > TELEGRAM_CHAR_LIMIT:
            if current and current != prefix:
                await update.message.reply_text(current + suffix)
            current = prefix + b
        else:
            if current and current != prefix:
                current += sep + b
            else:
                current += b
    if current and current != prefix:
        await update.message.reply_text(current + suffix)

def iso_to_bogota_str(iso_str):
    dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    local_dt = dt.astimezone(BOGOTA_TZ)
    return local_dt.strftime('%H:%M')

# =======================
# Partidos del día
# =======================
async def fixtures_por_fecha(fecha_iso_yyyy_mm_dd: str):
    """
    Devuelve dicts con: fixture_id, fecha_iso, liga, local/visitante nombres e IDs.
    Solo ligas en ALLOWED_LEAGUE_IDS.
    """
    cache_key = ("fixtures", fecha_iso_yyyy_mm_dd, tuple(sorted(ALLOWED_LEAGUE_IDS.keys())))
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    r = await safe_get_async("/fixtures", params={"date": fecha_iso_yyyy_mm_dd, "timezone": "America/Bogota"})
    salida = []
    if r:
        body = r.json() or {}
        for fx in body.get("response", []):
            league = fx.get("league", {})
            league_id = league.get("id")
            if league_id is None or not es_liga_permitida(league_id):
                continue  # filtra por ligas permitidas
            salida.append({
                "fixture_id": fx["fixture"]["id"],
                "fecha_iso": fx["fixture"]["date"],
                "liga_id": league_id,
                "liga": ALLOWED_LEAGUE_IDS.get(league_id, league.get("name", "Liga")),
                "local_name": fx["teams"]["home"]["name"],
                "visitante_name": fx["teams"]["away"]["name"],
                "local_id": fx["teams"]["home"]["id"],
                "visitante_id": fx["teams"]["away"]["id"],
            })
    cache_set(cache_key, salida)
    return salida

# =======================
# Historial temporada 2023 (últimos 10 desde 31/12 hacia atrás)
# =======================
_FINISHED_STATES = {"FT", "AET", "PEN"}

async def _fetch_team_fixtures_season(team_id: int, season: int):
    """
    Devuelve (rows, limited):
      - rows: fixtures de la temporada (todas las competiciones)
      - limited: True si sospechamos límite/red.
    NO mandamos 'status' ni 'page' (filtramos localmente).
    """
    r = await safe_get_async("/fixtures", params={"team": team_id, "season": season})
    if not r:
        return [], True
    limited = getattr(r, "_rate_info", {}).get("limited", False)
    try:
        data = r.json() or {}
        return (data.get("response", []) or []), limited
    except Exception:
        return [], True

def _extract_goals_from_fixture(fx):
    goals = fx.get("goals") or {}
    gh, ga = goals.get("home"), goals.get("away")
    if isinstance(gh, int) and isinstance(ga, int):
        return gh, ga
    score = fx.get("score") or {}
    ft = score.get("fulltime") or {}
    gh, ga = ft.get("home"), ft.get("away")
    if isinstance(gh, int) and isinstance(ga, int):
        return gh, ga
    et = score.get("extratime") or {}
    gh, ga = et.get("home"), et.get("away")
    if isinstance(gh, int) and isinstance(ga, int):
        return gh, ga
    pen = score.get("penalty") or {}
    gh, ga = pen.get("home"), pen.get("away")
    if isinstance(gh, int) and isinstance(ga, int):
        return gh, ga
    return None, None

def _recency_weights(n, half_life=HALF_LIFE):
    if not half_life:
        return [1.0]*n
    import math
    lam = math.log(2)/half_life
    w = [math.exp(-lam*i) for i in range(n)]  # i=0: más reciente
    s = sum(w)
    return [x/s for x in w]

def _prom_ponderado(vec):
    if not vec:
        return None
    w = _recency_weights(len(vec))
    return round(sum(v*wi for v, wi in zip(vec, w)), 3)

def _last10_overall_from_year_end(rows, team_id: int, season_year: int, max_j=LAST_N):
    """
    Selecciona los ÚLTIMOS 'max_j' partidos TERMINADOS de la temporada,
    empezando desde el 31/12/<season_year> hacia atrás.
    Devuelve lista: [(ts, is_home, gf_equipo), ...]
    """
    end_ts = int(datetime(season_year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
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

async def promedios_temporada_por_equipo(team_id: int, season: int):
    """
    Calcula promedios usando SOLO los últimos 10 partidos TERMINADOS
    desde el 31/12/<season> hacia atrás (todas las competiciones).
    Devuelve (prom_gf_en_casa, prom_gf_de_visita, n_usados)
    """
    cache_key = ("hist_last10_from_year_end", team_id, season, LAST_N, HALF_LIFE)
    cached = cache_get(cache_key, ttl=300)
    if cached is not None:
        return cached

    rows, limited = await _fetch_team_fixtures_season(team_id, season)
    if limited and not rows:
        result = (None, None, 0)
        cache_set(cache_key, result)
        return result

    last10 = _last10_overall_from_year_end(rows, team_id, season, LAST_N)
    if not last10:
        result = (0.0, 0.0, 0)
        cache_set(cache_key, result)
        return result

    gf_home_list = [gf for _, is_home, gf in last10 if is_home]
    gf_away_list = [gf for _, is_home, gf in last10 if not is_home]

    prom_home = _prom_ponderado(gf_home_list) or 0.0
    prom_away = _prom_ponderado(gf_away_list) or 0.0
    n_total = len(last10)

    result = (prom_home, prom_away, n_total)
    cache_set(cache_key, result)
    return result

# ---- Promedio GLOBAL (sin separar) y total esperado por promedios ----
async def promedio_global_gf(team_id: int, season: int, max_j=LAST_N):
    """
    Promedio de GF en los últimos 'max_j' partidos TERMINADOS de la temporada,
    sin separar local/visita (si hay rate-limit devuelve (None,0)).
    """
    rows, limited = await _fetch_team_fixtures_season(team_id, season)
    if limited and not rows:
        return None, 0

    last10 = _last10_overall_from_year_end(rows, team_id, season, max_j)
    if not last10:
        return 0.0, 0
    goles = [gf for (_, _, gf) in last10]
    prom = round(sum(goles) / len(goles), 2)
    return prom, len(goles)

async def total_esperado_por_promedios(local_id, visitante_id, season: int = SEASON_HIST):
    """
    Suma de los promedios GLOBALes (últimos N) de cada equipo.
    """
    promL, nL = await promedio_global_gf(local_id, season, LAST_N)
    promV, nV = await promedio_global_gf(visitante_id, season, LAST_N)
    if promL is None or promV is None:
        return None
    if nL == 0 or nV == 0:
        return None
    return round(promL + promV, 2)

# =======================
# Odds
# =======================
async def odds_totales_fixture(fixture_id):
    cache_key = ("odds_totales", fixture_id)
    cached = cache_get(cache_key, ttl=60)
    if cached is not None:
        return cached

    r = await safe_get_async("/odds", params={"fixture": fixture_id, "bet": 5})
    resultados = []
    if r:
        body = r.json() or {}
        for entry in body.get("response", []):
            for book in (entry.get("bookmakers") or []):
                for bet in (book.get("bets") or []):
                    name = (bet.get("name", "") or "").lower()
                    if bet.get("id") == 5 or "over/under" in name:
                        for v in (bet.get("values") or []):
                            val = v.get("value", "")
                            odd_str = (v.get("odd", "0") or "0").replace(",", ".")
                            try:
                                odd = float(odd_str)
                                linea = float(val.split()[-1])
                            except Exception:
                                continue
                            reg = next((x for x in resultados if abs(x["line"] - linea) < 1e-9), None)
                            if not reg:
                                reg = {"line": linea, "over": None, "under": None}
                                resultados.append(reg)
                            low = val.lower()
                            if "over" in low:
                                reg["over"] = max(reg["over"] or 0.0, odd)
                            elif "under" in low:
                                reg["under"] = max(reg["under"] or 0.0, odd)

    resultados = [x for x in resultados if (x["over"] or x["under"])]
    resultados.sort(key=lambda d: d["line"])
    cache_set(cache_key, resultados)
    return resultados

async def odds_1x2_fixture(fixture_id):
    cache_key = ("odds_1x2", fixture_id)
    cached = cache_get(cache_key, ttl=60)
    if cached is not None:
        return cached

    best = {"home": None, "draw": None, "away": None}
    r = await safe_get_async("/odds", params={"fixture": fixture_id, "bet": 1})
    if r:
        body = r.json() or {}
        for entry in (body.get("response") or []):
            for book in (entry.get("bookmakers") or []):
                for bet in (book.get("bets") or []):
                    if bet.get("id") == 1 or (bet.get("name", "") or "").lower().startswith(("match winner","1x2")):
                        for v in (bet.get("values") or []):
                            name = (v.get("value","") or "").lower()
                            odd = float((v.get("odd","0") or "0").replace(",", "."))
                            if name in ("home","1","local"):
                                best["home"] = max(best["home"] or 0.0, odd)
                            elif name in ("draw","x","empate"):
                                best["draw"] = max(best["draw"] or 0.0, odd)
                            elif name in ("away","2","visitante"):
                                best["away"] = max(best["away"] or 0.0, odd)
    cache_set(cache_key, best)
    return best

# =======================
# Estimación O/U por home/away ponderado (para O/U con cuotas)
# =======================
def etiqueta_confianza(total, linea, cuota):
    diff = (total - linea) if (total is not None and linea is not None) else 0.0
    if total is None or linea is None or cuota is None:
        return ""
    if abs(diff) <= 0.2:
        return "⚠️ Zona gris (mejor pasar)"
    if abs(diff) >= 0.5 and cuota >= 1.85:
        return "🟢 Alta confianza"
    if abs(diff) >= 0.3:
        return "🟡 Media"
    return "🟠 Baja"

async def estimar_total_esperado_homeaway(local_id, visitante_id):
    promL_home, _, nL = await promedios_temporada_por_equipo(local_id, SEASON_HIST)
    _, promV_away, nV = await promedios_temporada_por_equipo(visitante_id, SEASON_HIST)
    if promL_home is None or promV_away is None:
        return None
    if nL == 0 or nV == 0:
        return None
    return round((promL_home + promV_away), 2)

async def elegir_over_under_recomendado(fixture_id, local_id, visitante_id):
    odds = await odds_totales_fixture(fixture_id)
    total_est = await estimar_total_esperado_homeaway(local_id, visitante_id)
    if total_est is None or not odds:
        return None

    linea_obj = next((o for o in odds if abs(o["line"] - 2.5) < 1e-6), None)
    if not linea_obj:
        linea_obj = min(odds, key=lambda x: abs(x["line"] - total_est))

    linea = linea_obj["line"]
    over_q = linea_obj.get("over")
    under_q = linea_obj.get("under")

    margen = 0.25
    if total_est >= linea + margen and over_q:
        lado, cuota = f"Over {linea}", over_q
    elif total_est <= linea - margen and under_q:
        lado, cuota = f"Under {linea}", under_q
    else:
        if (over_q or 0) >= (under_q or 0):
            lado, cuota = f"Over {linea}", over_q
        else:
            lado, cuota = f"Under {linea}", under_q

    return {
        "linea": linea,
        "lado": lado,
        "cuota": cuota,
        "total_estimado": total_est,
        "confianza": etiqueta_confianza(total_est, linea, cuota),
    }

# =======================
# Comandos del Bot
# =======================
MAX_FIXTURES_LIST = 40

async def hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = datetime.now(BOGOTA_TZ).strftime('%Y-%m-%d')
    partidos = await fixtures_por_fecha(fecha)
    if not partidos:
        await update.message.reply_text("📭 No hay partidos programados para hoy en las ligas permitidas.")
        return
    bloques = []
    for p in partidos[:MAX_FIXTURES_LIST]:
        hora_local = iso_to_bogota_str(p["fecha_iso"])
        bloques.append(f"🏆 {p['liga']}\n{p['local_name']} vs {p['visitante_name']}  ⏰ {hora_local}")
    await send_blocks(update, bloques)

async def pronostico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra:
    - promedios home/away (últimos 10 desde 31/12/SEASON_HIST hacia atrás, con recencia)
    - tendencia
    - y además: Recomendación Over/Under (con cuotas) + Cuotas 1X2
    Solo para ligas en ALLOWED_LEAGUE_IDS.
    """
    fecha = datetime.now(BOGOTA_TZ).strftime('%Y-%m-%d')
    partidos = await fixtures_por_fecha(fecha)
    if not partidos:
        await update.message.reply_text("📭 No hay partidos para hoy en las ligas permitidas.")
        return

    mensajes = []
    for p in partidos[:MAX_FIXTURES_LIST]:
        promL_home, _, nL = await promedios_temporada_por_equipo(p["local_id"], SEASON_HIST)
        _, promV_away, nV = await promedios_temporada_por_equipo(p["visitante_id"], SEASON_HIST)

        # Over/Under con cuotas
        reco = await elegir_over_under_recomendado(p["fixture_id"], p["local_id"], p["visitante_id"])
        # Cuotas 1X2
        o = await odds_1x2_fixture(p["fixture_id"])

        hora_local = iso_to_bogota_str(p["fecha_iso"])
        msg = [
            f"📅 {fecha} ⏰ {hora_local} - 🏆 {p['liga']}",
            f"⚽ {p['local_name']} vs {p['visitante_name']}",
            f"📈 Últimos {LAST_N} (Temp {SEASON_HIST}, todas las competiciones):",
        ]

        if promL_home is None or promV_away is None:
            msg.append("⛔ Límite de API/historial (intenta en 1–2 min).")
        else:
            msg.append(f"  - {p['local_name']} (en casa): {promL_home} GF/partido")
            msg.append(f"  - {p['visitante_name']} (de visita): {promV_away} GF/partido")

            if nL and nV:
                if promL_home > promV_away:
                    msg.append("🔮 Tendencia: **ligera ventaja del local**.")
                elif promL_home < promV_away:
                    msg.append("🔮 Tendencia: **ligera ventaja del visitante**.")
                else:
                    msg.append("🔮 Tendencia: **partido parejo**.")
            else:
                msg.append(f"🔮 Tendencia: datos insuficientes (Temp {SEASON_HIST}).")

        # Bloque Over/Under (con cuotas)
        if reco and (reco.get("cuota") is not None):
            msg.append(f"🎚️ Línea O/U usada: **{reco['linea']}**")
            msg.append(f"🔢 Total estimado (home/away): **{reco['total_estimado']}**")
            msg.append(f"🎯 Recomendación O/U: **{reco['lado']}** (mejor cuota {reco['cuota']})")
            if reco.get("confianza"):
                msg.append(reco["confianza"])
        else:
            # Si no hay cuotas o datos, avisamos sin duplicar el bloque global
            msg.append("ℹ️ O/U: sin cuotas disponibles o datos insuficientes.")

        # Bloque 1X2
        msg.append(f"💰 1X2: 1={o.get('home') or '-'}  X={o.get('draw') or '-'}  2={o.get('away') or '-'}")

        mensajes.append("\n".join(msg))
        await asyncio.sleep(0.2)  # micro pausa para evitar 429

    await send_blocks(update, mensajes)

async def overunder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        fecha = args[0]
    else:
        fecha = datetime.now(BOGOTA_TZ).strftime('%Y-%m-%d')

    partidos = await fixtures_por_fecha(fecha)
    if not partidos:
        await update.message.reply_text(f"📭 No hay partidos programados para {fecha} en las ligas permitidas.")
        return

    mensajes = []
    for p in partidos[:12]:
        reco = await elegir_over_under_recomendado(p["fixture_id"], p["local_id"], p["visitante_id"])
        hora_local = iso_to_bogota_str(p["fecha_iso"])

        base = [
            f"📅 {fecha} ⏰ {hora_local} - 🏆 {p['liga']}",
            f"⚽ {p['local_name']} vs {p['visitante_name']}",
        ]

        if reco and (reco.get("cuota") is not None):
            base.append(f"🎚️ Línea usada: **{reco['linea']}**")
            base.append(f"🔢 Total estimado (home/away): **{reco['total_estimado']}**")
            base.append(f"🎯 Recomendación: **{reco['lado']}** (mejor cuota {reco['cuota']})")
            if reco.get("confianza"):
                base.append(reco["confianza"])
        else:
            total_est = await estimar_total_esperado_homeaway(p["local_id"], p["visitante_id"])
            if total_est is not None:
                lado_sugerido = "Over 2.5" if total_est >= 2.5 else "Under 2.5"
                base.append(f"🔢 Total estimado (home/away): **{total_est}**")
                base.append(f"💡 Sugerencia por stats: **{lado_sugerido}** (sin cuotas disponibles)")
            else:
                base.append("ℹ️ Sin datos suficientes / límite de API.")
        mensajes.append("\n".join(base))
        await asyncio.sleep(0.2)

    await send_blocks(update, mensajes)

async def unoxtwo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = datetime.now(BOGOTA_TZ).strftime('%Y-%m-%d')
    partidos = await fixtures_por_fecha(fecha)
    if not partidos:
        await update.message.reply_text("📭 No hay partidos para hoy en las ligas permitidas.")
        return

    mensajes = []
    for p in partidos[:12]:
        o = await odds_1x2_fixture(p["fixture_id"])
        hora_local = iso_to_bogota_str(p["fecha_iso"])
        base = [
            f"📅 {fecha} ⏰ {hora_local} - 🏆 {p['liga']}",
            f"⚽ {p['local_name']} vs {p['visitante_name']}",
            f"💰 1X2: 1={o['home'] or '-'}  X={o['draw'] or '-'}  2={o['away'] or '-'}",
        ]
        mensajes.append("\n".join(base))
        await asyncio.sleep(0.15)
    await send_blocks(update, mensajes)

async def debugteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /debugteam <team_id>")
        return
    try:
        team_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El team_id debe ser numérico.")
        return

    rows, limited = await _fetch_team_fixtures_season(team_id, SEASON_HIST)
    last10 = _last10_overall_from_year_end(rows, team_id, SEASON_HIST, LAST_N)
    lines = [
        f"📊 Team {team_id} (Temp {SEASON_HIST}): {'LIMITADO' if (limited and not rows) else f'{len(last10)} partidos usados'}",
    ]
    for ts, is_home, gf in last10:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(BOGOTA_TZ).strftime("%Y-%m-%d")
        cond = "LOCAL" if is_home else "VISITA"
        lines.append(f" - {dt}: {cond} → GF={gf}")
    if not last10:
        lines.append("Sin partidos elegibles en la ventana o límite de API.")
    await send_blocks(update, lines, sep="\n")

# =======================
# Main
# =======================
if __name__ == '__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("hoy", hoy))
    app.add_handler(CommandHandler("pronostico", pronostico))
    app.add_handler(CommandHandler("overunder", overunder))
    app.add_handler(CommandHandler("1x2", unoxtwo))
    app.add_handler(CommandHandler("debugteam", debugteam))
    print("🤖 Bot corriendo... Esperando comandos en Telegram.")
    try:
        app.run_polling()
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(async_client.aclose())
        except Exception:
            pass
