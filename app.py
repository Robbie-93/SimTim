import sqlite3
import requests
import urllib3
import re
import time
import json
import os
import sys
import logging
import traceback
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request, send_from_directory
from threading import Thread, Lock
from werkzeug.exceptions import HTTPException
from log_setup import set_log_context
from version import __version__

# ------------------ SETTING ---------------------------
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = Flask(__name__)

# ------------------- LOGGING --------------------------
logger = logging.getLogger("app")
cache_logger = logging.getLogger("app.cache")
radar_logger = logging.getLogger("app.radar")
traindb_logger = logging.getLogger("app.traindb")
vehicles_logger = logging.getLogger("app.vehicles")
request_logger = logging.getLogger("app.request")
health_logger = logging.getLogger("app.health")

# ------------------- CONFIGURATION -------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (SimTim Terminal)"}

data_cache = {
    "trains": {"data": None, "server_id": None, "ts": 0},
    "stations": {"data": None, "server_id": None, "ts": 0},
    "edr": {"data": None, "server_id": None, "ts": 0},
    "servers": {"data": None, "server_id": None, "ts": 0},
}
CACHE_LOCK = Lock()

HTTP = requests.Session()
HTTP.headers.update(HEADERS)
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
HTTP.mount("https://", _adapter)
HTTP.mount("http://", _adapter)

ACTIVE_SERVER_STATE = {"server_id": "en1"}
LAST_SELECTED_TRAIN = {"train_num": None, "server_id": None}

# ------------------- SERVER-TIJD CACHE -------------------
SERVER_TIME_CACHE = {"epoch_ms": None, "server_id": None, "ts": 0}
SERVER_TIME_LOCK = Lock()
SERVER_TIME_REFRESH_INTERVAL = 15  # seconden


def _server_time_refresh_loop():
    while True:
        server_id = ACTIVE_SERVER_STATE["server_id"]
        try:
            url = f"https://api1.aws.simrail.eu:8082/api/getTime?serverCode={server_id}"
            response = HTTP.get(url, timeout=5)
            unix_val = int(response.text.strip())
            if unix_val < 10_000_000_000:
                unix_val *= 1000
            with SERVER_TIME_LOCK:
                SERVER_TIME_CACHE.update({
                    "epoch_ms": unix_val,
                    "server_id": server_id,
                    "ts": time.time()
                })
            set_log_context(server_id=server_id, server_time=get_cached_server_time_str())
        except Exception as e:
            cache_logger.warning(f"[server_time] Failed to refresh server time for {server_id}: {e}")
        time.sleep(SERVER_TIME_REFRESH_INTERVAL)


def get_cached_server_time_str():
    with SERVER_TIME_LOCK:
        epoch_ms = SERVER_TIME_CACHE.get("epoch_ms")
        age = time.time() - SERVER_TIME_CACHE.get("ts", 0)

    if epoch_ms is None:
        return "unknown"

    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        stale_marker = " [stale]" if age > SERVER_TIME_REFRESH_INTERVAL * 3 else ""
        return f"{dt.strftime('%H:%M')}{stale_marker}"
    except Exception:
        return "unknown"

def get_server_now(server_id):
    """
    Actuele servertijd als naive datetime (zelfde 'wandklok' als de timetable-strings).
    Doortellen vanaf de laatste refresh. None als er geen tijd voor deze server bekend is.
    """
    with SERVER_TIME_LOCK:
        epoch_ms = SERVER_TIME_CACHE.get("epoch_ms")
        cached_server = SERVER_TIME_CACHE.get("server_id")
        ts = SERVER_TIME_CACHE.get("ts", 0)

    if epoch_ms is None or cached_server != server_id:
        return None
    try:
        base = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
        return base + timedelta(seconds=time.time() - ts)
    except Exception:
        return None


# ------------------- BEVROREN ACTUELE TIJDEN -------------------
# Zodra een stop gepasseerd is, liggen de actuele aankomst/vertrek vast en worden
# ze bij latere polls niet meer herberekend. Alleen de huidige rit wordt bewaard:
# bij een andere rit begint het geheugen leeg en vult het zich opnieuw uit de API.
FROZEN_TIMES = {"trip_key": None, "stops": {}}
FROZEN_TIMES_LOCK = Lock()


def _get_frozen_trip(trip_key):
    with FROZEN_TIMES_LOCK:
        if FROZEN_TIMES["trip_key"] != trip_key:
            if FROZEN_TIMES["trip_key"] is not None:
                request_logger.info(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Frozen times reset: "
                    f"{FROZEN_TIMES['trip_key']} -> {trip_key}"
                )
            FROZEN_TIMES["trip_key"] = trip_key
            FROZEN_TIMES["stops"] = {}
        return FROZEN_TIMES["stops"]

def _edr_is_passed(edr_point):
    """
    Is dit EDR-punt echt gepasseerd? 'isConfirmed' is leidend: EDR vult
    actualArrival/DepartureTime ook vooraf in als prognose, dus die zeggen niets.
    Alleen als isConfirmed ontbreekt valt dit terug op een echte vertrektijd.
    """
    if not edr_point:
        return False
    confirmed = edr_point.get('isConfirmed')
    if confirmed is not None:
        return bool(confirmed)
    return bool(edr_point.get('actualDepartureTime'))


TIMETABLE_TTL_CACHE = {}
TIMETABLE_TTL_SECONDS = 60

VEHICLES_FILE = os.path.join(DATA_DIR, "vehicle_details.json")
vehicles_db = {"locomotives": {}, "wagons": {}, "cargo": {}}
# -------------------- HELPERS --------------------

def format_api_time(time_str):
    if not time_str:
        return "--:--"
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%H:%M")
    except:
        return "--:--"

_radio_db_cache = None

def get_radio_db():
    global _radio_db_cache
    if _radio_db_cache is not None:
        return _radio_db_cache

    json_path = os.path.join(DATA_DIR, "simrail_radio.json")
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            _radio_db_cache = json.load(f)
            return _radio_db_cache
    return {}

def get_station_radio(station_name, radio_db, _visited=None):
 
    if _visited is None:
        _visited = set()

    if station_name in _visited:
        request_logger.warning(
            f"Cycle detected in supervisedBy chain for station '{station_name}' "
            f"in radio database - stopping lookup to avoid infinite recursion."
        )
        return None
    _visited.add(station_name)

    if station_name not in radio_db:
        return None
    
    data = radio_db[station_name]
    
    # 1. Heeft dit station zelf een radio?
    if data.get('radio'):
        return data['radio']
    
    # 2. Heeft het een moederstation? (Recursie voor ketens)
    parent = data.get('supervisedBy')
    if parent and parent in radio_db:
        return get_station_radio(parent, radio_db, _visited)
        
    return None

def resolve_supervising_station(station_name, radio_db, _visited=None):

    if _visited is None:
        _visited = set()

    if not station_name or station_name in _visited:
        return station_name
    _visited.add(station_name)

    data = radio_db.get(station_name)
    if not data:
        return station_name

    parent = data.get('supervisedBy')
    if parent and parent in radio_db:
        return resolve_supervising_station(parent, radio_db, _visited)

    return station_name

TIMETABLE_CACHE = {}

def get_train_length_from_api(server_id, train_id):

    if train_id in TIMETABLE_CACHE:
        return TIMETABLE_CACHE[train_id]
        
    try:
        url = f"https://api1.aws.simrail.eu:8082/api/getAllTimetables?serverCode={server_id}&train={train_id}"
        response = HTTP.get(url, timeout=2) # Korte timeout om hangen te voorkomen
        if response.status_code == 200:
            data = response.json()
            timetable_obj = data[0] if isinstance(data, list) else data
            
            length = timetable_obj.get('trainLength', 200) # 200 als fallback
            TIMETABLE_CACHE[train_id] = length # Opslaan in het geheugen
            return length
    except Exception as e:
        logger.warning(f"Failed to fetch timetable length for train {train_id}: {e}")
        
    return 200 # Fallback als de API offline is of herhaaldelijk faalt

train_db = {}
CACHE_DIR = DATA_DIR
CACHE_FILE = os.path.join(CACHE_DIR, "train_db.json")
TRAIN_DB_URL = "https://wiki.simrail.eu/tips_tricks/spawnlist/maping_2026_processed.json"

def load_train_data():
    global train_db
    
    os.makedirs(CACHE_DIR, exist_ok=True)
    raw_list = None

    try:
        traindb_logger.info("Downloading latest train database...")
        response = HTTP.get(TRAIN_DB_URL, timeout=10)
        if response.status_code == 200:
            raw_list = response.json()

            # Sla lokaal op als cache
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(raw_list, f, ensure_ascii=False, indent=2)

            traindb_logger.info(f"Cache updated: {CACHE_FILE}")
        else:
            traindb_logger.warning(f"Unexpected status {response.status_code} while downloading train database.")

    except Exception as e:
        traindb_logger.warning(f"Online download failed, falling back to local cache: {e}")

    # ==========================================================
    # 2. FALLBACK NAAR LOKALE CACHE
    # ==========================================================
    if raw_list is None:
        try:
            traindb_logger.info("Loading local cache...")

            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                raw_list = json.load(f)

            traindb_logger.info(f"Local cache loaded: {CACHE_FILE}")

        except Exception as e:
            traindb_logger.error(f"Failed to load local cache, starting without train database: {e}")
            raw_list = []

    # ==========================================================
    # 3. CONVERTEER NAAR LOOKUP DATABASE
    # ==========================================================
    temp_db = {}

    for item in raw_list:
        for num in item.get("numbers", []):
            temp_db[str(num)] = item

    train_db = temp_db

    traindb_logger.info(f"Loaded {len(train_db)} train entries.")

def load_vehicles_db():
    global vehicles_db
    if os.path.exists(VEHICLES_FILE):
        try:
            with open(VEHICLES_FILE, 'r', encoding='utf-8') as f:
                vehicles_db = json.load(f)
            vehicles_logger.info("Enriched vehicle database loaded successfully.")
        except Exception as e:
            vehicles_logger.error(f"Error loading enriched vehicle database: {e}")
    else:
        vehicles_logger.warning(f"Vehicle database file not found: {VEHICLES_FILE}")

# ==========================================================================
# ACHTERGROND-CACHE SYSTEEM (netwerk-optimalisatie)
# ==========================================================================

_cache_failure_streaks = {}  # cache_key -> aantal opeenvolgende mislukte pogingen


def _cache_refresh_loop(cache_key, url_builder, interval, verify_ssl=True):
    while True:
        server_id = ACTIVE_SERVER_STATE["server_id"]
        set_log_context(server_id=server_id)
        try:
            url = url_builder(server_id)
            response = HTTP.get(url, timeout=5, verify=verify_ssl)

            if response.status_code != 200:
                _cache_failure_streaks[cache_key] = _cache_failure_streaks.get(cache_key, 0) + 1
                cache_logger.warning(
                    f"[{cache_key}] Unexpected status {response.status_code} while fetching "
                    f"(attempt #{_cache_failure_streaks[cache_key]} in a row)"
                )
            else:
                with CACHE_LOCK:
                    data_cache[cache_key] = {
                        "data": response.json(),
                        "server_id": server_id,
                        "ts": time.time()
                    }

                if _cache_failure_streaks.get(cache_key):
                    cache_logger.info(
                        f"[{cache_key}] Recovered after {_cache_failure_streaks[cache_key]} failed attempt(s)"
                    )
                    _cache_failure_streaks[cache_key] = 0
        except Exception as e:
            _cache_failure_streaks[cache_key] = _cache_failure_streaks.get(cache_key, 0) + 1
            cache_logger.warning(
                f"[{cache_key}] Background refresh failed "
                f"(attempt #{_cache_failure_streaks[cache_key]} in a row): {e}"
            )
        time.sleep(interval)

def start_background_loops():
    """Start de achtergrondloops voor trains/stations/edr. Eén keer aanroepen bij app-start."""
    Thread(target=_cache_refresh_loop, args=(
        "trains",
        lambda sid: f"https://panel.simrail.eu:8084/trains-open?serverCode={sid}",
        1.5, False
    ), daemon=True).start()

    Thread(target=_cache_refresh_loop, args=(
        "stations",
        lambda sid: f"https://panel.simrail.eu:8084/stations-open?serverCode={sid}",
        7, True
    ), daemon=True).start()

    Thread(target=_cache_refresh_loop, args=(
        "edr",
        lambda sid: f"https://api1.aws.simrail.eu:8082/api/getEDRTimetables?serverCode={sid}",
        60, True
    ), daemon=True).start()

    Thread(target=_server_time_refresh_loop, daemon=True).start()

    cache_logger.info("Background loops started for trains/stations/edr/server_time.")

def get_cached_or_fetch(cache_key, url, server_id, verify_ssl=True, max_age=None):

    with CACHE_LOCK:
        entry = dict(data_cache.get(cache_key) or {})

    is_stale = (
        entry.get("data") is None
        or entry.get("server_id") != server_id
        or (max_age is not None and (time.time() - entry.get("ts", 0)) > max_age)
    )

    if not is_stale:
        return entry["data"]

    try:
        response = HTTP.get(url, timeout=5, verify=verify_ssl)
        data = response.json()
        with CACHE_LOCK:
            data_cache[cache_key] = {"data": data, "server_id": server_id, "ts": time.time()}
        return data
    except Exception as e:
        set_log_context(server_id=server_id)
        cache_logger.warning(f"[{cache_key}] Fallback fetch failed: {e}")
        return entry.get("data")

_RADAR_DB_CONN = None
_RADAR_DB_LOCK = Lock()

def get_radar_db_connection():
    global _RADAR_DB_CONN
    if _RADAR_DB_CONN is None:
        db_path = os.path.join(DATA_DIR, "signals.db")
        _RADAR_DB_CONN = sqlite3.connect(db_path, check_same_thread=False)
        _RADAR_DB_CONN.row_factory = sqlite3.Row
    return _RADAR_DB_CONN

def build_radar_path(start_signal_name, dist_to_start_signal, live_signal_speed, view_range_meters=10000):
    if view_range_meters < 100:
        view_range_meters = 10000

    map_elements = []

    with _RADAR_DB_LOCK:
        try:
            conn = get_radar_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 1. GEPASSEERD SEIN (is_abs en empl_abbr toegevoegd aan SELECT)
            cursor.execute("""
                SELECT c.from_signal, 
                       COALESCE(c.max_distance, 0) as max_distance, 
                       s.emplacement_group,
                       s.is_abs,
                       s.empl_abbr
                FROM signal_connections c
                LEFT JOIN signals s ON c.from_signal = s.signal_name
                WHERE c.to_signal = ?
                ORDER BY COALESCE(c.measurements, 0) DESC
                LIMIT 1
            """, (start_signal_name,))
            prev_row = cursor.fetchone()
        
            if prev_row:
                prev_signal = prev_row['from_signal']
                prev_group = prev_row['emplacement_group']
                dist_between_signals = float(prev_row['max_distance']) * 1000
                passed_dist = dist_to_start_signal - dist_between_signals
            
                is_prev_empl = bool(prev_group and not str(prev_group).startswith('ABS_'))
                prev_empl_code = prev_row['empl_abbr'] if is_prev_empl else None
            
                map_elements.append({
                    "type": "signal",
                    "name": prev_signal,
                    "dist": int(passed_dist),
                    "speed": 32767, 
                    "is_emplacement": is_prev_empl,
                    "emplacement_code": prev_empl_code,
                    "is_abs": prev_row['is_abs'] if prev_row['is_abs'] is not None else 0
                })

            # 2. EERSTVOLGENDE STARTSEIN (is_abs en empl_abbr toegevoegd aan SELECT)
            cursor.execute("SELECT emplacement_group, is_abs, empl_abbr FROM signals WHERE signal_name = ?", (start_signal_name,))
            start_row = cursor.fetchone()
        
            start_group = start_row['emplacement_group'] if start_row else 'ABS_'
            start_abs = start_row['is_abs'] if start_row and start_row['is_abs'] is not None else 0
        
            is_start_empl = bool(start_group and not str(start_group).startswith('ABS_'))
            start_empl_code = start_row['empl_abbr'] if start_row and is_start_empl else None
        
            map_elements.append({
                "type": "signal",
                "name": start_signal_name,
                "dist": int(dist_to_start_signal),
                "speed": live_signal_speed, 
                "is_emplacement": is_start_empl,
                "emplacement_code": start_empl_code,
                "is_abs": start_abs
            })

            # 3. DE REST VAN DE SLIERT (s.is_abs en s.empl_abbr toegevoegd aan SELECT)
            accumulated_distance = dist_to_start_signal
            current_signal = start_signal_name

            while accumulated_distance <= view_range_meters:
                cursor.execute("""
                    SELECT c.to_signal, 
                           COALESCE(c.max_distance, 0) as max_distance, 
                           COALESCE(c.measurements, 0) as measurements, 
                           s.emplacement_group,
                           s.is_abs,
                           s.empl_abbr
                    FROM signal_connections c
                    LEFT JOIN signals s ON c.to_signal = s.signal_name
                    WHERE c.from_signal = ?
                    ORDER BY measurements DESC
                """, (current_signal,))
            
                raw_connections = cursor.fetchall()
                if not raw_connections:
                    break

                connections = []
                seen_signals = set()
                for row in raw_connections:
                    if row['to_signal'] not in seen_signals:
                        seen_signals.add(row['to_signal'])
                        connections.append(row)

                next_conn = None
                if len(connections) == 1:
                    next_conn = connections[0]
                elif len(connections) > 1:
                    best_option = connections[0]
                    second_option = connections[1]
                
                    if best_option['measurements'] >= (second_option['measurements'] * 1.333):
                        next_conn = best_option
                    else:
                        break
                else:
                    break

                if next_conn:
                    next_signal = next_conn['to_signal']
                    dist_increment = float(next_conn['max_distance']) * 1000
                    accumulated_distance += dist_increment
                
                    next_group = next_conn['emplacement_group']
                    is_next_empl = bool(next_group and not str(next_group).startswith('ABS_'))
                    next_empl_code = next_conn['empl_abbr'] if is_next_empl else None

                    map_elements.append({
                        "type": "signal",
                        "name": next_signal,
                        "dist": int(accumulated_distance),
                        "speed": 32767, 
                        "is_emplacement": is_next_empl,
                        "emplacement_code": next_empl_code,
                        "is_abs": next_conn['is_abs'] if next_conn['is_abs'] is not None else 0
                    })
                
                    current_signal = next_signal

        except Exception as e:
            radar_logger.error(f"Error building radar path from signal '{start_signal_name}': {e}")

    return map_elements

# ==========================================================================
# PERRONS (platforms.db) - koppeling via het eerstvolgende sein
# ==========================================================================
platform_logger = logging.getLogger("app.platforms")

PLATFORMS_DB_PATH = os.path.join(DATA_DIR, "platforms.db")
# signal_name -> lijst van perron-records (per richting 'up' / 'down')
_PLATFORM_INDEX = {"by_signal": {}, "mtime": None}
_PLATFORM_INDEX_LOCK = Lock()

_ROMAN_MAP = {"I": 1, "V": 5, "X": 10, "L": 50}

# Eén INFO-melding per onvolledig/dubbelzinnig perron, niet elke 2s-poll
_PLATFORM_INCOMPLETE_LOGGED = set()


def _normalize_station_name(name):
    """Naam vergelijkbaar maken: kleine letters, zonder diakrieten (ł expliciet!)."""
    import unicodedata
    if not name:
        return ""
    s = str(name).strip().lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.replace("-", " ").split())


def _normalize_platform_number(value):
    """Perron/spoor naar een vergelijkbare string ('IV' -> '4', 2 -> '2'). Onbekend -> None."""
    if value is None:
        return None
    s = str(value).strip().upper()
    if not s or s in ("-", "NONE", "NULL"):
        return None
    if s.isdigit():
        return str(int(s))
    # Romeinse cijfers (SimRail timetable gebruikt die voor perrons)
    if all(ch in _ROMAN_MAP for ch in s):
        total, prev = 0, 0
        for ch in reversed(s):
            val = _ROMAN_MAP[ch]
            total = total - val if val < prev else total + val
            prev = max(prev, val)
        return str(total)
    return s


def _load_platform_index():
    """Laadt platforms.db in het geheugen; herlaadt alleen als het bestand gewijzigd is."""
    try:
        mtime = os.path.getmtime(PLATFORMS_DB_PATH)
    except OSError:
        # Eén keer melden, niet bij elke poll
        if not _PLATFORM_INDEX.get("missing_logged"):
            platform_logger.warning(f"platforms.db not found at {PLATFORMS_DB_PATH} - platforms disabled")
            _PLATFORM_INDEX.update({"by_signal": {}, "mtime": None, "missing_logged": True})
        return _PLATFORM_INDEX["by_signal"]

    with _PLATFORM_INDEX_LOCK:
        if _PLATFORM_INDEX["mtime"] == mtime:
            return _PLATFORM_INDEX["by_signal"]

        by_signal = {}
        usable = 0
        try:
            # Alleen-lezen openen: de recorder mag ondertussen schrijven
            uri = "file:" + PLATFORMS_DB_PATH.replace("\\", "/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, station, perron, spoor, lengte,
                       up_next_signal, up_afstand, up_perronzijde,
                       down_next_signal, down_afstand, down_perronzijde
                FROM platforms
            """).fetchall()
            conn.close()
        except Exception as e:
            platform_logger.error(f"Failed to read platforms.db: {e}")
            return _PLATFORM_INDEX["by_signal"]

        for r in rows:
            # Per richting een eigen record; sein + afstand + zijde komen uit dezelfde categorie
            for direction in ("up", "down"):
                sig = r[f"{direction}_next_signal"]
                if not sig:
                    continue
                rec = {
                    "id": r["id"],
                    "station": r["station"],
                    "station_key": _normalize_station_name(r["station"]),
                    "perron": _normalize_platform_number(r["perron"]),
                    "spoor": _normalize_platform_number(r["spoor"]),
                    "perron_raw": r["perron"],
                    "spoor_raw": r["spoor"],
                    "lengte": r["lengte"],
                    "direction": direction,
                    "afstand": r[f"{direction}_afstand"],
                    "zijde": (r[f"{direction}_perronzijde"] or "").strip().upper() or None,
                }
                # Hoofdletter-ongevoelig: in de database staat bv. zowel '3877_Sk_P1' als '3877_SK_P1'
                by_signal.setdefault(sig.strip().upper(), []).append(rec)
                if rec["afstand"] is not None and rec["lengte"] and rec["zijde"] in ("L", "R"):
                    usable += 1

        _PLATFORM_INDEX.update({"by_signal": by_signal, "mtime": mtime, "missing_logged": False})
        platform_logger.info(
            f"[{datetime.now().strftime('%H:%M:%S')}] Platform index (re)loaded: "
            f"{len(rows)} platforms, {len(by_signal)} signals, {usable} drawable direction records"
        )
        return by_signal


def build_platform_elements(map_elements, final_timetable):
    """
    Bouwt 'platform'-elementen voor de radar.
    - Alleen voor stations die in de timetable als PH staan.
    - Gekoppeld via het sein in onze sliert (up_next_signal of down_next_signal),
      afstand en perronzijde uit dezelfde categorie.
    - Perron ligt vóór het sein: kop = sein.dist - afstand, staart = kop - lengte.
    """
    by_signal = _load_platform_index()
    if not by_signal:
        return []

    # PH-stops uit de timetable, met spoor/perron voor disambiguatie
    ph_stops = {}
    for stop in final_timetable:
        if stop.get("stop_type") != "PH":
            continue
        ph_stops[_normalize_station_name(stop.get("name"))] = {
            "track": _normalize_platform_number(stop.get("track")),
            "platform": _normalize_platform_number(stop.get("platform")),
        }
    if not ph_stops:
        return []

    platforms = []
    placed_ids = set()

    for sig in map_elements:
        if sig.get("type") != "signal":
            continue
        candidates = [
            c for c in by_signal.get(str(sig["name"]).strip().upper(), [])
            if c["station_key"] in ph_stops
        ]
        if not candidates:
            continue

        # Disambiguatie: eerst op spoor, dan op perronnummer uit de timetable
        tt = ph_stops[candidates[0]["station_key"]]
        if len(candidates) > 1 and tt["track"]:
            by_track = [c for c in candidates if c["spoor"] == tt["track"]]
            if by_track:
                candidates = by_track
        if len(candidates) > 1 and tt["platform"]:
            by_perron = [c for c in candidates if c["perron"] == tt["platform"]]
            if by_perron:
                candidates = by_perron
        if len(candidates) > 1:
            log_key = ("ambiguous", sig["name"], tt["track"], tt["platform"])
            if log_key not in _PLATFORM_INCOMPLETE_LOGGED:
                _PLATFORM_INCOMPLETE_LOGGED.add(log_key)
                platform_logger.info(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Platform via {sig['name']} not drawn - ambiguous: "
                    f"{[c['id'] for c in candidates]} (timetable track={tt['track']}, platform={tt['platform']})"
                )
            continue

        rec = candidates[0]
        if rec["afstand"] is None or not rec["lengte"] or rec["zijde"] not in ("L", "R"):
            log_key = (rec["id"], rec["direction"], sig["name"])
            if log_key not in _PLATFORM_INCOMPLETE_LOGGED:
                _PLATFORM_INCOMPLETE_LOGGED.add(log_key)
                platform_logger.info(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Platform {rec['id']} ({rec['direction']}) via "
                    f"{sig['name']} not drawn - incomplete data "
                    f"(afstand={rec['afstand']}, lengte={rec['lengte']}, zijde={rec['zijde']})"
                )
            continue
        place_key = (rec["id"], rec["direction"])
        if place_key in placed_ids:
            continue
        placed_ids.add(place_key)

        head_dist = int(sig["dist"] - rec["afstand"])
        tail_dist = int(head_dist - rec["lengte"])
        platforms.append({
            "type": "platform",
            "name": rec["station"],
            "dist": tail_dist,          # sorteersleutel: begin van het perron
            "start_dist": tail_dist,
            "end_dist": head_dist,
            "length": rec["lengte"],
            "side": rec["zijde"],
            "perron": rec["perron"],
            "spoor": rec["spoor"],
            "signal": sig["name"],
            "direction": rec["direction"],
        })

    return platforms

def _to_roman(value):
    """Perronnummer tonen zoals in de SimRail-dienstregeling ('2' -> 'II'). Niet-numeriek blijft ongewijzigd."""
    s = str(value).strip() if value is not None else ""
    if not s.isdigit() or int(s) <= 0:
        return s or None
    n = int(s)
    out = ""
    for val, sym in ((50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= val:
            out += sym
            n -= val
    return out


# Laatst gemelde perronwijziging per trein, zodat het log niet elke 2s-poll volloopt
_LAST_PLATFORM_NOTICE = {}


def detect_platform_notice(train_num, live_signal_name, final_timetable):
    """
    Vergelijkt het perron achter het live 'SignalInFront' (platforms.db) met
    spoor/perron uit de dienstregeling voor dezelfde (nog niet gepasseerde) PH-stop.
    Retourneert een notice-dict bij een afwijking, anders None.
    - Komt één van de kandidaten overeen met de dienstregeling: geen melding.
    - Wijzen meerdere kandidaten naar verschillende sporen: niet te bepalen, geen melding.
    """
    if not live_signal_name:
        return None

    by_signal = _load_platform_index()
    candidates_all = by_signal.get(live_signal_name.strip().upper(), [])
    if not candidates_all:
        return None

    # Nog niet gepasseerde PH-stops uit de dienstregeling
    ph_stops = {}
    for stop in final_timetable:
        if stop.get("stop_type") != "PH" or stop.get("is_passed"):
            continue
        key = _normalize_station_name(stop.get("name"))
        ph_stops.setdefault(key, stop)  # eerste (eerstvolgende) voorkomen telt

    candidates = [c for c in candidates_all if c["station_key"] in ph_stops]
    if not candidates:
        return None

    tt_stop = ph_stops[candidates[0]["station_key"]]
    tt_track = _normalize_platform_number(tt_stop.get("track"))
    tt_platform = _normalize_platform_number(tt_stop.get("platform"))

    # Niets om mee te vergelijken in de dienstregeling
    if not tt_track and not tt_platform:
        return None

    def matches_timetable(c):
        track_ok = not tt_track or not c["spoor"] or c["spoor"] == tt_track
        platform_ok = not tt_platform or not c["perron"] or c["perron"] == tt_platform
        return track_ok and platform_ok

    if any(matches_timetable(c) for c in candidates):
        _LAST_PLATFORM_NOTICE.pop(str(train_num), None)
        return None

    distinct = {(c["spoor"], c["perron"]) for c in candidates}
    if len(distinct) > 1:
        platform_logger.debug(
            f"[{datetime.now().strftime('%H:%M:%S')}] Platform change undetermined for train {train_num} "
            f"at signal {live_signal_name}: {[c['id'] for c in candidates]}"
        )
        return None

    rec = candidates[0]
    notice = {
        "key": f"{rec['station_key']}|{rec['spoor']}|{rec['perron']}",
        "station": rec["station"],
        "track": rec["spoor_raw"],
        "platform": _to_roman(rec["perron_raw"]),
        "side": rec["zijde"] if rec["zijde"] in ("L", "R") else None,
        "tt_track": tt_stop.get("track") if tt_track else None,
        "tt_platform": tt_stop.get("platform") if tt_platform else None,
        "signal": live_signal_name,
    }

    if _LAST_PLATFORM_NOTICE.get(str(train_num)) != notice["key"]:
        _LAST_PLATFORM_NOTICE[str(train_num)] = notice["key"]
        platform_logger.info(
            f"[{datetime.now().strftime('%H:%M:%S')}] Platform change for train {train_num} at {rec['station']}: "
            f"timetable track {notice['tt_track'] or '-'} / platform {notice['tt_platform'] or '-'} -> "
            f"detected track {notice['track'] or '-'} / platform {notice['platform'] or '-'} "
            f"(signal {live_signal_name}, side {notice['side'] or 'unknown'})"
        )
    return notice

# -------------------- ROUTES --------------------

@app.errorhandler(Exception)
def handle_uncaught_exception(e):

    if isinstance(e, HTTPException):
        return e

    logger.error(f"Uncaught error on route {request.path}: {e}\n{traceback.format_exc()}")
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500

@app.route('/')
def index():
    return render_template('index.html', version=__version__)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.root_path, 'icon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/api/status')
def status_ping():
    return {"status": "ok"}, 200

@app.route('/api/health')
def health_check():
    server_id = request.args.get('server_id', '')
    train_id = request.args.get('train_id', '')

    # Zonder gekozen server/trein worden die checks niet uitgevoerd maar als 'standby' gemeld
    endpoints = {
        "Servers": "https://panel.simrail.eu:8084/servers-open",
    }
    if server_id:
        endpoints["Server Time"] = f"https://api1.aws.simrail.eu:8082/api/getTime?serverCode={server_id}"

    results = {}
    overall_status = "online"

    for name, url in endpoints.items():
        try:
            response = HTTP.get(url, timeout=2)
            if response.status_code == 200:
                results[name] = "online"
            else:
                results[name] = "error"
                overall_status = "partial"
        except Exception:
            results[name] = "offline"
            overall_status = "offline"

    if not server_id:
        results["Server Time"] = "standby"

    if not server_id or not train_id:
        results["Train Timetable"] = "standby"
    else:
        tt_cache_key = (server_id, str(train_id))
        cached_tt_entry = TIMETABLE_TTL_CACHE.get(tt_cache_key)
        if cached_tt_entry and (time.time() - cached_tt_entry["ts"] < TIMETABLE_TTL_SECONDS):
            results["Train Timetable"] = "online"
        else:
            try:
                tt_url = f"https://api1.aws.simrail.eu:8082/api/getAllTimetables?serverCode={server_id}&train={train_id}"
                response = HTTP.get(tt_url, timeout=2)
                if response.status_code == 200:
                    results["Train Timetable"] = "online"
                    TIMETABLE_TTL_CACHE[tt_cache_key] = {"data": response.json(), "ts": time.time()}
                else:
                    results["Train Timetable"] = "error"
                    overall_status = "partial"
            except Exception:
                results["Train Timetable"] = "offline"
                overall_status = "offline"

    cache_checks = {
        "Trains": ("trains", 8),
        "EDR Data": ("edr", 70),
        "Stations": ("stations", 20),
    }
    with CACHE_LOCK:
        for name, (cache_key, max_age) in cache_checks.items():
            entry = data_cache.get(cache_key) or {}
            has_data = entry.get("data") is not None
            age = time.time() - entry.get("ts", 0) if has_data else None

            if has_data and age is not None and age <= max_age:
                results[name] = "online"
            else:
                results[name] = "error"
                if overall_status == "online":
                    overall_status = "partial"

    if overall_status != "online":
        set_log_context(server_id=server_id)
        health_logger.warning(f"Health check status={overall_status}: {results}")

    return jsonify({
        "overall": overall_status,
        "details": results
    })

@app.route('/api/servers')
def get_servers():
    url = "https://panel.simrail.eu:8084/servers-open"
    res = get_cached_or_fetch("servers", url, "global", verify_ssl=False, max_age=30) or {}
    return jsonify(res)

@app.route('/api/server_time')
def get_server_time():
    server_id = request.args.get('server', 'en1')
    try:
        url = f"https://api1.aws.simrail.eu:8082/api/getTime?serverCode={server_id}"
        res = HTTP.get(url, timeout=5)
        unix_val = int(res.text.strip())
        if unix_val < 10000000000:
            unix_val *= 1000 
        return jsonify({"time": unix_val})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/timezone')
def get_timezone():
    server_id = request.args.get('server')
    try:
        res = HTTP.get(
            f"https://api1.aws.simrail.eu:8082/api/getTimeZone?serverCode={server_id}",
            timeout=5
        )
        return jsonify({"timezone": res.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/data')
@app.route('/api/my_train_data')
def get_train_data():
    server_id = request.args.get('server', 'en1')
    train_num = request.args.get('number')
    
    if not train_num:
        return jsonify({"error": "No train number provided"}), 400

    set_log_context(server_id=server_id)

    previous_server = ACTIVE_SERVER_STATE["server_id"]
    if previous_server != server_id:
        request_logger.info(f"Active server switched: {previous_server} -> {server_id}")
    ACTIVE_SERVER_STATE["server_id"] = server_id

    if LAST_SELECTED_TRAIN.get("train_num") != train_num or LAST_SELECTED_TRAIN.get("server_id") != server_id:
        request_logger.info(f"Train {train_num} selected on server {server_id}")
        LAST_SELECTED_TRAIN["train_num"] = train_num
        LAST_SELECTED_TRAIN["server_id"] = server_id

    try:
        # 1. Haal Live Treinen Data op (voor basisinfo & overig verkeer)
        # Komt uit de achtergrond-cache (~1.5s vers) i.p.v. elke poll opnieuw op te halen.
        live_url = f"https://panel.simrail.eu:8084/trains-open?serverCode={server_id}"
        live_res = get_cached_or_fetch("trains", live_url, server_id, verify_ssl=False, max_age=3) or {}
        # Geen live data én geen cache: dit is een verbindingsprobleem, geen onbekende trein
        if not live_res:
            return jsonify({"error": "SimRail live train data unavailable"}), 502
        all_trains = live_res.get('data', [])
        my_live = next((t for t in all_trains if str(t.get('TrainNoLocal')) == str(train_num)), None)

        if not my_live:
            return jsonify({"error": "No service found"}), 404

        train_data = my_live.get('TrainData', {})
        td = my_live.get('TrainData') or {}
        
        # Basis bar gegevens
        response_train_num = train_num
        response_route = f"{my_live.get('StartStation', '-')} - {my_live.get('EndStation', '-')}"
        response_train_type = my_live.get("TrainName", "").split(" - ")[0] or "-"
        game_end_station = my_live.get('EndStation', '-')

        # 2. Haal Train Timetable op
        # Dienstregeling verandert niet elke 2s, dus 60s TTL-cache per (server, trein).
        tt_cache_key = (server_id, str(train_num))
        cached_tt_entry = TIMETABLE_TTL_CACHE.get(tt_cache_key)
        if cached_tt_entry and (time.time() - cached_tt_entry["ts"] < TIMETABLE_TTL_SECONDS):
            tt_res = cached_tt_entry["data"]
        else:
            tt_url = f"https://api1.aws.simrail.eu:8082/api/getAllTimetables?serverCode={server_id}&train={train_num}"
            tt_res = HTTP.get(tt_url, timeout=5).json()
            TIMETABLE_TTL_CACHE[tt_cache_key] = {"data": tt_res, "ts": time.time()}
        
        my_tt_data = next((t for t in tt_res if str(t.get('trainNoLocal')) == str(train_num)), None)
        if not my_tt_data:
            return jsonify({"error": "No timetable data found"}), 404
        
        raw_timetable = my_tt_data.get('timetable', [])

        # 3. Haal EDR Timetable op
        # Komt uit de achtergrond-cache (~4s vers) i.p.v. elke poll opnieuw op te halen.
        edr_url = f"https://api1.aws.simrail.eu:8082/api/getEDRTimetables?serverCode={server_id}"
        edr_res = get_cached_or_fetch("edr", edr_url, server_id, verify_ssl=True, max_age=65) or []
        my_edr_data = next((t for t in edr_res if str(t.get('trainNoLocal')) == str(train_num)), None)
        edr_points = my_edr_data.get('timetable', []) if my_edr_data else []
        edr_dict = {p.get('nameForPerson'): p for p in edr_points}

        # Bouw de lookup volgens jouw eigen voorspellings-methodiek
        edr_delay_map = {}
        for edr_train in edr_res:
            other_t_num = str(edr_train.get('trainNoLocal'))
            other_t_tt = edr_train.get('timetable', [])
            
            delay_minutes = 0
            
            # 1. Zoek de index van het eerste station dat nog niet bevestigd is
            start_idx = 0
            for idx, pt in enumerate(other_t_tt):
                if pt.get('isConfirmed') == False:
                    start_idx = idx
                    break
            
            # 2. Scan vanaf dat actieve punt vooruit naar het eerstvolgende meetpunt
            for idx in range(start_idx, len(other_t_tt)):
                pt = other_t_tt[idx]
                
                edr_arr = pt.get('actualArrivalTime')
                plan_arr = pt.get('arrivalTime')
                
                if not edr_arr or not plan_arr:
                    edr_arr = pt.get('actualDepartureTime')
                    plan_arr = pt.get('departureTime')
                
                if edr_arr and plan_arr:
                    try:
                        planned_dt = datetime.strptime(plan_arr, "%Y-%m-%d %H:%M:%S")
                        actual_dt = datetime.strptime(edr_arr, "%Y-%m-%d %H:%M:%S")
                        
                        delay_minutes = int((actual_dt - planned_dt).total_seconds() / 60)
                        break
                    except:
                        continue
            
            edr_delay_map[other_t_num] = delay_minutes
        
        # 4. Haal Stations-API op
        # Komt uit de achtergrond-cache (~7s vers) i.p.v. elke poll opnieuw op te halen.
        stations_url = f"https://panel.simrail.eu:8084/stations-open?serverCode={server_id}"
        stations_res = get_cached_or_fetch("stations", stations_url, server_id, verify_ssl=True, max_age=10) or {}
        stations_list = stations_res.get('data', []) if isinstance(stations_res, dict) else []
        stations_dict = {s.get('Name'): s for s in stations_list}

        # 5. Combineer en Bereken Dienstregeling met Vertragingsprojectie
        final_timetable = []
        current_station_index = -1
        for idx, pt in enumerate(raw_timetable):
            name = pt.get('nameForPerson', '')
            edr_match = edr_dict.get(name)
            if edr_match and edr_match.get('isConfirmed') == False:
                current_station_index = idx
                break
        
        if current_station_index == -1:
            for idx, pt in enumerate(raw_timetable):
                name = pt.get('nameForPerson', '')
                edr_match = edr_dict.get(name)
                if edr_match and not edr_match.get('actualDepartureTime'):
                    current_station_index = idx
                    break

        upcoming_edr_delay = 0
        for idx in range(max(0, current_station_index), len(raw_timetable)):
            pt = raw_timetable[idx]
            name = pt.get('nameForPerson', '')
            edr_match = edr_dict.get(name)
            if edr_match:
                edr_arr = edr_match.get('actualArrivalTime')
                plan_arr = pt.get('arrivalTime')
                if edr_arr and plan_arr:
                    try:
                        planned_arr = datetime.strptime(plan_arr, "%Y-%m-%d %H:%M:%S")
                        actual_arr = datetime.strptime(edr_arr, "%Y-%m-%d %H:%M:%S")
                        upcoming_edr_delay = int((actual_arr - planned_arr).total_seconds() / 60)
                        break
                    except:
                        continue
        
        radio_db = get_radio_db()

        # Servertijd (niet de pc-klok) voor de passeer-check van tussenhaltes
        server_now = get_server_now(server_id)

        # Laatste EDR-stop die bevestigd (isConfirmed) is: alles daarvóór is zeker gepasseerd.
        # Niet op actualArrival/DepartureTime: die bevatten ook prognoses voor stops die nog komen.
        last_edr_reached_idx = -1
        for idx, pt in enumerate(raw_timetable):
            em = edr_dict.get(pt.get('nameForPerson', ''))
            if em and _edr_is_passed(em):
                last_edr_reached_idx = idx

        # Bevroren tijden van deze rit (eerste geplande tijd onderscheidt ritten met hetzelfde nummer)
        first_pt = raw_timetable[0] if raw_timetable else {}
        trip_key = (server_id, str(train_num), first_pt.get('departureTime') or first_pt.get('arrivalTime'))
        frozen_trip = _get_frozen_trip(trip_key)
        newly_frozen = []
        
        for idx, pt in enumerate(raw_timetable):
            name = pt.get('nameForPerson', '')
            stop_type_raw = pt.get('stopType', 'NoStopOver')
            supervised_by_raw = pt.get('supervisedBy')
            supervised_by = supervised_by_raw.strip() if supervised_by_raw else ""

            dispatcher_lookup_name = resolve_supervising_station(name, radio_db)

            dispatcher_type = ""
            if dispatcher_lookup_name and dispatcher_lookup_name in stations_dict:
                disp_info = stations_dict[dispatcher_lookup_name].get('DispatchedBy', [])
                dispatcher_type = "user" if disp_info and len(disp_info) > 0 else "laptop"

            stop_type_display = "-"
            stop_duration_display = "-"
            arr_str = pt.get('arrivalTime')
            dep_str = pt.get('departureTime')
            
            if stop_type_raw == "CommercialStop":
                stop_type_display = "PH"
            elif stop_type_raw == "NoncommercialStop":
                stop_type_display = "PT"

            if stop_type_raw in ["CommercialStop", "NoncommercialStop"] and arr_str and dep_str:
                try:
                    dt_arr = datetime.strptime(arr_str, "%Y-%m-%d %H:%M:%S")
                    dt_dep = datetime.strptime(dep_str, "%Y-%m-%d %H:%M:%S")
                    diff_min = int((dt_dep - dt_arr).total_seconds() / 60)
                    stop_duration_display = "0⁵" if diff_min == 0 else str(diff_min)
                except:
                    stop_duration_display = "0⁵"

            edr_match = edr_dict.get(name)
            is_edr = edr_match is not None
            is_passed = False
            
            act_arr_str = "--:--"
            act_dep_str = "--:--"

            if is_edr:
                edr_arr = edr_match.get('actualArrivalTime')
                edr_dep = edr_match.get('actualDepartureTime')
                act_arr_str = datetime.strptime(edr_arr, "%Y-%m-%d %H:%M:%S").strftime("%H:%M") if edr_arr else format_api_time(arr_str)
                if edr_dep:
                    act_dep_str = datetime.strptime(edr_dep, "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
                else:
                    act_dep_str = format_api_time(dep_str)
                # Gepasseerd alleen als EDR de stop bevestigd heeft (vertrektijd kan een prognose zijn)
                is_passed = _edr_is_passed(edr_match)
            else:
                if arr_str:
                    planned_arr = datetime.strptime(arr_str, "%Y-%m-%d %H:%M:%S")
                    actual_arr = planned_arr + timedelta(minutes=upcoming_edr_delay)
                    act_arr_str = actual_arr.strftime("%H:%M")
                
                if dep_str:
                    planned_dep = datetime.strptime(dep_str, "%Y-%m-%d %H:%M:%S")
                    actual_dep = planned_dep + timedelta(minutes=upcoming_edr_delay)
                    act_dep_str = actual_dep.strftime("%H:%M")
                    
                # Gepasseerd als een latere EDR-stop al bereikt is, of als de
                # geprojecteerde vertrektijd vóór de servertijd ligt (volledige datum: middernacht-veilig)
                if idx < last_edr_reached_idx:
                    is_passed = True
                elif server_now is not None:
                    compare_dt = actual_dep if dep_str else (actual_arr if arr_str else None)
                    is_passed = compare_dt is not None and compare_dt < server_now

            # Bevriezen: eenmaal gepasseerd blijven tijden en status vast
            frozen_key = f"{idx}|{name}"
            frozen = frozen_trip.get(frozen_key)
            if frozen:
                is_passed = True
                # Echte EDR-tijden blijven leidend; anders de bevroren waarden
                edr_has_actuals = is_edr and _edr_is_passed(edr_match) and edr_match.get('actualDepartureTime')
                if not edr_has_actuals:
                    act_arr_str = frozen["arr"]
                    act_dep_str = frozen["dep"]
            elif is_passed:
                frozen_trip[frozen_key] = {"arr": act_arr_str, "dep": act_dep_str}
                newly_frozen.append(f"{name} ({act_arr_str}/{act_dep_str})")

            is_current = False  # wordt na de lus bepaald: eerste niet-gepasseerde stop
            current_station_delay = int(edr_match.get('delay', 0)) if is_edr else upcoming_edr_delay

            delayClass = "delay-ontime" 
            planned_time_str = format_api_time(pt.get('arrivalTime'))

            if act_arr_str != "--:--" and planned_time_str != "--:--":
                try:
                    plan_dt = datetime.strptime(planned_time_str, "%H:%M")
                    act_dt = datetime.strptime(act_arr_str, "%H:%M")
                    diff = (act_dt - plan_dt).total_seconds() / 60
                    if diff >= 5:
                        delayClass = "delay-severe"
                    elif diff > 0:
                        delayClass = "delay-warning"
                except ValueError:
                    pass
            
            final_timetable.append({
                "name": name,
                "arr_planned": format_api_time(arr_str),
                "dep_planned": format_api_time(dep_str),
                "arr_actual": act_arr_str,
                "dep_actual": act_dep_str,
                "stop_type": stop_type_display,
                "stop_duration": stop_duration_display,
                "line": pt.get('line', '-'),
                "platform": pt.get('platform', '-'),
                "track": pt.get('track', '-'),
                "radio": get_station_radio(pt.get('nameForPerson'), radio_db),
                "dispatcher": dispatcher_type,
                "supervisedBy": supervised_by,
                "is_current": is_current,
                "is_passed": is_passed,
                "delay_minutes": current_station_delay, 
                "delayClass": delayClass,
            })

        # Huidige stop = eerste niet-gepasseerde stop (ook tussenhaltes)
        for stop in final_timetable:
            if not stop["is_passed"]:
                stop["is_current"] = True
                break

        if newly_frozen:
            request_logger.info(
                f"[{datetime.now().strftime('%H:%M:%S')}] Train {train_num}: actual times frozen for "
                f"{len(newly_frozen)} passed stop(s): {', '.join(newly_frozen)}"
            )
        if server_now is None:
            request_logger.debug(f"Server time for {server_id} not yet cached - time-based passing check skipped")

        # 6. Voertuigverrijking (Summary / Technical / Consist)
        train_extra = train_db.get(str(train_num), {})
        raw_vehicles = train_extra.get("vehicles") or []
        enriched_vehicles = []
        lowest_wagon_vmax = 999
        
        for v_str in raw_vehicles:
            parts = v_str.split(':')
            if len(parts) >= 3:
                id_part = parts[0]
                cargo_info = parts[2]
            else:
                id_part = v_str.split(':', 1)[0]
                cargo_info = "0"
                
            variant = id_part.split('/', 1)[1] if '/' in id_part else id_part
            wagon_info = vehicles_db.get("wagons", {}).get(variant, {})
            
            cargo_weight = 0.0
            cargo_name = ""
            
            if '@' in cargo_info:
                weight_part, cargo_name = cargo_info.split('@', 1)
            else:
                weight_part = cargo_info
                
            try:
                clean_weight = "".join(c for c in weight_part if c.isdigit() or c == '.')
                cargo_weight = float(clean_weight) if clean_weight else 0.0
            except ValueError:
                cargo_weight = 0.0
                
            if cargo_name:
                cargo_translation = vehicles_db.get("cargo", {}).get(cargo_name, cargo_name)
            elif cargo_weight > 0:
                cargo_translation = "Loaded"
            else:
                cargo_translation = "-"
            
            is_loaded = cargo_weight > 0
            if is_loaded:
                w_vmax = wagon_info.get("vmax_loaded") or wagon_info.get("vmax")
                status_display = "Geladen"
            else:
                w_vmax = wagon_info.get("vmax_unloaded") or wagon_info.get("vmax")
                status_display = "Leeg"
                cargo_translation = "-"
            
            if w_vmax and isinstance(w_vmax, (int, float)) and w_vmax < lowest_wagon_vmax:
                lowest_wagon_vmax = w_vmax
                
            wagon_base_weight = wagon_info.get("service_weight") or wagon_info.get("weight") or 0.0
            if not isinstance(wagon_base_weight, (int, float)):
                try:
                    wagon_base_weight = float(wagon_base_weight)
                except ValueError:
                    wagon_base_weight = 0.0
            
            total_wagon_weight = wagon_base_weight + cargo_weight
                
            enriched_vehicles.append({
                "raw_string": v_str,
                "variant": variant,
                "base_type": wagon_info.get("base_type", "-"),
                "wiki_name": wagon_info.get("wiki_name") or variant,
                "cargo": cargo_translation or "-",
                "status": status_display,
                "vmax": w_vmax or "-",
                "length": wagon_info.get("length") or "-",
                "weight": round(total_wagon_weight, 1) if total_wagon_weight > 0 else "-"
            })

        loco_vmax = 999
        total_power = 0
        raw_locos = train_extra.get("locos") or []
        
        for loco_str in raw_locos:
            loco_type = re.split(r'[-\s]', loco_str)[0]
            loco_info = vehicles_db.get("locomotives", {}).get(loco_type, {})
            l_vmax = loco_info.get("vmax")
            if l_vmax and isinstance(l_vmax, (int, float)) and l_vmax < loco_vmax:
                loco_vmax = l_vmax
            l_power = loco_info.get("power")
            if l_power and isinstance(l_power, (int, float)):
                total_power += l_power

        api_vmax = train_extra.get("max_speed") or train_extra.get("vmax") or 0
        calculated_vmax = min(loco_vmax, lowest_wagon_vmax, api_vmax if api_vmax > 0 else 999)
        if calculated_vmax == 999: 
            calculated_vmax = api_vmax or "-"

        summary_data = {
            "train_number": train_num,
            "service_name": response_train_type,
            "route": response_route,
            "operator": train_extra.get("operator", "-"),
        }

        technical_data = {
            "locomotives": raw_locos or "-",
            "vmax": calculated_vmax,
            "power": total_power if total_power > 0 else "-",
            "mass": train_extra.get("totalMassTons") or "-",
            "length": train_extra.get("totalLengthM") or "-",
            "brake_regime": train_extra.get("brakeRegime") or "-",
        }

        consist_data = {
            "carriages_wagons_count": train_extra.get("vehicleNormalCount") or "-",
            "sleeper_carriages_count": train_extra.get("vehicleNightCount") or "-",
            "vehicles": enriched_vehicles
        }

        # ==========================================================================
        # LIVE RADAR EXTRACTION & OVERIG VERKEER INTEGRATIE
        # ==========================================================================
        raw_signal_str = td.get('SignalInFront', '')
        live_signal_name = raw_signal_str.split('@')[0].strip() if raw_signal_str else None
        live_signal_dist = int(td.get('DistanceToSignalInFront', 0))
        
        raw_signal_speed = td.get('SignalInFrontSpeed', 0)
        live_signal_speed = "Vmax" if raw_signal_speed == 32767 else raw_signal_speed

        # Perronwijziging t.o.v. dienstregeling (via het live sein vóór de trein)
        platform_notice = None
        try:
            platform_notice = detect_platform_notice(train_num, live_signal_name, final_timetable)
        except Exception as e:
            platform_logger.error(f"Error detecting platform change: {e}\n{traceback.format_exc()}")

        berekende_seinen = []
        if live_signal_name:
            radar_logger.debug(f"Live signal: {live_signal_name} at {live_signal_dist}m. Calculating chain...")
            berekende_seinen = build_radar_path(
                start_signal_name=live_signal_name, 
                dist_to_start_signal=live_signal_dist,
                live_signal_speed=live_signal_speed, 
                view_range_meters=10000
            )

            # --- INTERCEPT ANDERE TREINEN OP RADAR-TRAJECT ---
            for t in all_trains:
                other_num = str(t.get('TrainNoLocal'))
                # Sla onszelf over
                if other_num == str(train_num):
                    continue
                
                other_td = t.get('TrainData')
                if not other_td:
                    continue
                
                raw_other_sig = other_td.get('SignalInFront', '')
                if not raw_other_sig:
                    continue
                
                # Filter de pure seinnaam van de andere trein
                other_sig_name = raw_other_sig.split('@')[0].strip()
                
                # Controleer of de andere trein naar een sein rijdt dat in sliert ligt
                matching_sig = next((s for s in berekende_seinen if s['name'] == other_sig_name), None)
                
                if matching_sig:
                    # ==================================================================
                    # Kleur het sein in met de voorwaarde van de voorligger
                    # ==================================================================
                    raw_other_speed = other_td.get('SignalInFrontSpeed', 0)
                    matching_sig['speed'] = "Vmax" if raw_other_speed == 32767 else raw_other_speed
                    
                    other_dist_to_sig = float(other_td.get('DistanceToSignalInFront', 0))
                    other_rel_dist = matching_sig['dist'] - other_dist_to_sig
                    
                    train_id_or_num = t.get('TrainNoLocal') # Let op: SimRail gebruikt soms het interne ID of TrainNoLocal in de URL

                    exact_length = get_train_length_from_api(server_id, train_id_or_num)

                    other_delay = edr_delay_map.get(other_num, 0)

                    berekende_seinen.append({
                        "type": "traffic",
                        "name": other_num,
                        "dist": int(other_rel_dist),
                        "length": exact_length,  # <-- Dit is nu de ECHTE lengte uit de API!
                        "speed": round(other_td.get('Velocity', 0)),
                        "distance_to_signal": int(other_dist_to_sig),
                        "next_signal": other_sig_name,
                        "delay": other_delay,
                        "destination": t.get('EndStation', '')
                    })

            # --- PERRONS (alleen PH-stops, gekoppeld via sein in de sliert) ---
            # Na de verkeers-loop toegevoegd zodat de seinnaam-match daar niet geraakt wordt.
            try:
                platform_elements = build_platform_elements(berekende_seinen, final_timetable)
                if platform_elements:
                    radar_logger.debug(
                        f"Platforms on radar: "
                        f"{[(p['name'], p['signal'], p['direction'], p['side']) for p in platform_elements]}"
                    )
                berekende_seinen.extend(platform_elements)
            except Exception as e:
                platform_logger.error(f"Error building platform elements: {e}\n{traceback.format_exc()}")

            # Sorteer de sliert opnieuw zodat seinen en treinen op volgorde van afstand staan
            berekende_seinen.sort(key=lambda x: x['dist'])

        return jsonify({
            "train_num": response_train_num,
            "speed": round(td.get('Velocity', 0)),
            "route": response_route,
            "train_type": response_train_type,
            "game_end_station": game_end_station,
            "timetable": final_timetable,
            "delay": upcoming_edr_delay,
            "train_info": {
                "summary": summary_data,
                "technical": technical_data,
                "consist": consist_data,
            },
            "signal": {
                "name": live_signal_name,
                "dist": live_signal_dist,
                "speed": live_signal_speed
            } if live_signal_name else None,
            "map_elements": berekende_seinen,
            "platform_notice": platform_notice
        })

    except Exception as e:
        request_logger.error(
            f"Error in get_train_data for train {train_num}: {e}\n{traceback.format_exc()}"
        )
        return jsonify({"error": str(e)}), 500

# -------------------- RUN --------------------
@app.after_request
def add_header(response):
    if 'Cache-Control' not in response.headers:
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'public, max-age=3600'
        else:
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    return response

if __name__ == '__main__':
    DEBUG_MODE = True 
    IS_REAL_SERVER_PROCESS = not DEBUG_MODE or os.environ.get('WERKZEUG_RUN_MAIN') == 'true'

    if IS_REAL_SERVER_PROCESS:
        load_train_data()
        load_vehicles_db()

    if IS_REAL_SERVER_PROCESS:
        start_background_loops()

    app.run(debug=DEBUG_MODE, host='0.0.0.0', port=5000, threaded=True)