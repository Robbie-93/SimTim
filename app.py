import sqlite3
import requests
import urllib3
import re
import time
import json
import os
import sys
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template, request
from threading import Thread, Lock

# ------------------ SETTING ---------------------------
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = Flask(__name__)

logger = logging.getLogger(__name__)

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
}
CACHE_LOCK = Lock()

HTTP = requests.Session()
HTTP.headers.update(HEADERS)
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
HTTP.mount("https://", _adapter)
HTTP.mount("http://", _adapter)

ACTIVE_SERVER_STATE = {"server_id": "en1"}

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

def get_station_radio(station_name, radio_db):
    if station_name not in radio_db:
        return None
    
    data = radio_db[station_name]
    
    # 1. Heeft dit station zelf een radio?
    if data.get('radio'):
        return data['radio']
    
    # 2. Heeft het een moederstation? (Recursie voor ketens)
    parent = data.get('supervisedBy')
    if parent and parent in radio_db:
        return get_station_radio(parent, radio_db)
        
    return None

TIMETABLE_CACHE = {}

def get_train_length_from_api(server_id, train_id):
    """Haalt de exacte lengte op uit de SimRail Timetable API met caching"""
    if train_id in TIMETABLE_CACHE:
        return TIMETABLE_CACHE[train_id]
        
    try:
        url = f"https://api1.aws.simrail.eu:8082/api/getAllTimetables?serverCode={server_id}&train={train_id}"
        response = HTTP.get(url, timeout=2) # Korte timeout om hangen te voorkomen
        if response.status_code == 200:
            data = response.json()
            # De API geeft een lijst of direct een object, even checken:
            timetable_obj = data[0] if isinstance(data, list) else data
            
            length = timetable_obj.get('trainLength', 200) # 200 als fallback
            TIMETABLE_CACHE[train_id] = length # Opslaan in het geheugen
            return length
    except Exception as e:
        logger.error(f"Failed to fetch timetable for train {train_id}: {e}")
        
    return 200 # Fallback als de API offline is of herhaaldelijk faalt

train_db = {}
CACHE_DIR = DATA_DIR
CACHE_FILE = os.path.join(CACHE_DIR, "train_db.json")
TRAIN_DB_URL = "https://wiki.simrail.eu/tips_tricks/spawnlist/maping_2025_processed.json"

def load_train_data():
    global train_db
    
    os.makedirs(CACHE_DIR, exist_ok=True)
    raw_list = None

    try:
        logger.info("[TRAIN_DB] Downloading latest train database...")
        response = HTTP.get(TRAIN_DB_URL, timeout=10)
        if response.status_code == 200:
            raw_list = response.json()

            # Sla lokaal op als cache
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(raw_list, f, ensure_ascii=False, indent=2)

            logger.info(f"[TRAIN_DB] Cache updated: {CACHE_FILE}")

    except Exception as e:
        logger.warning(f"[TRAIN_DB] Online download failed: {e}")

    # ==========================================================
    # 2. FALLBACK NAAR LOKALE CACHE
    # ==========================================================
    if raw_list is None:
        try:
            logger.info("[TRAIN_DB] Loading local cache...")

            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                raw_list = json.load(f)

            logger.info(f"[TRAIN_DB] Local cache loaded: {CACHE_FILE}")

        except Exception as e:
            logger.error(f"[TRAIN_DB] Failed to load local cache: {e}")
            raw_list = []

    # ==========================================================
    # 3. CONVERTEER NAAR LOOKUP DATABASE
    # ==========================================================
    temp_db = {}

    for item in raw_list:
        for num in item.get("numbers", []):
            temp_db[str(num)] = item

    train_db = temp_db

    logger.info(f"[TRAIN_DB] Loaded {len(train_db)} train entries.")

def load_vehicles_db():
    global vehicles_db
    if os.path.exists(VEHICLES_FILE):
        try:
            with open(VEHICLES_FILE, 'r', encoding='utf-8') as f:
                vehicles_db = json.load(f)
            logger.info("[VEHICLES_DB] Enriched vehicle database loaded successfully.")
        except Exception as e:
            logger.error(f"[VEHICLES_DB] Error loading enriched vehicle database: {e}")

# ==========================================================================
# ACHTERGROND-CACHE SYSTEEM (netwerk-optimalisatie)
# ==========================================================================

def _cache_refresh_loop(cache_key, url_builder, interval, verify_ssl=True):
    while True:
        server_id = ACTIVE_SERVER_STATE["server_id"]
        try:
            url = url_builder(server_id)
            response = HTTP.get(url, timeout=5, verify=verify_ssl)
            if response.status_code == 200:
                with CACHE_LOCK:
                    data_cache[cache_key] = {
                        "data": response.json(),
                        "server_id": server_id,
                        "ts": time.time()
                    }
        except Exception as e:
            logger.warning(f"[CACHE:{cache_key}] Background refresh failed: {e}")
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

    logger.info("[CACHE] Background loops started for trains/stations/edr.")

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
        logger.warning(f"[CACHE:{cache_key}] Fallback fetch failed: {e}")
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

            # 1. GEPASSEERD SEIN (is_abs toegevoegd aan SELECT)
            cursor.execute("""
                SELECT c.from_signal, 
                       COALESCE(c.max_distance, 0) as max_distance, 
                       s.emplacement_group,
                       s.is_abs
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
                prev_empl_code = prev_signal[:2] if is_prev_empl else None
            
                map_elements.append({
                    "type": "signal",
                    "name": prev_signal,
                    "dist": int(passed_dist),
                    "speed": 32767, 
                    "is_emplacement": is_prev_empl,
                    "emplacement_code": prev_empl_code,
                    "is_abs": prev_row['is_abs'] if prev_row['is_abs'] is not None else 0
                })

            # 2. EERSTVOLGENDE STARTSEIN (is_abs toegevoegd aan SELECT)
            cursor.execute("SELECT emplacement_group, is_abs FROM signals WHERE signal_name = ?", (start_signal_name,))
            start_row = cursor.fetchone()
        
            start_group = start_row['emplacement_group'] if start_row else 'ABS_'
            start_abs = start_row['is_abs'] if start_row and start_row['is_abs'] is not None else 0
        
            is_start_empl = bool(start_group and not str(start_group).startswith('ABS_'))
            start_empl_code = start_signal_name[:2] if is_start_empl else None
        
            map_elements.append({
                "type": "signal",
                "name": start_signal_name,
                "dist": int(dist_to_start_signal),
                "speed": live_signal_speed, 
                "is_emplacement": is_start_empl,
                "emplacement_code": start_empl_code,
                "is_abs": start_abs
            })

            # 3. DE REST VAN DE SLIERT (s.is_abs toegevoegd aan SELECT)
            accumulated_distance = dist_to_start_signal
            current_signal = start_signal_name

            while accumulated_distance <= view_range_meters:
                cursor.execute("""
                    SELECT c.to_signal, 
                           COALESCE(c.max_distance, 0) as max_distance, 
                           c.line_number, 
                           COALESCE(c.measurements, 0) as measurements, 
                           s.emplacement_group,
                           s.is_abs
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
                    next_empl_code = next_signal[:2] if is_next_empl else None

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
            logger.error(f"[RADAR] Radar path error: {e}")

    return map_elements
# -------------------- ROUTES --------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status_ping():
    return {"status": "ok"}, 200

@app.route('/api/health')
def health_check():
    server_id = request.args.get('server_id', 'int1')
    train_id = request.args.get('train_id', '')

    endpoints = {
        "Servers": "https://panel.simrail.eu:8084/servers-open",
        "Server Time": f"https://api1.aws.simrail.eu:8082/api/getTime?serverCode={server_id}",
    }

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

    return jsonify({
        "overall": overall_status,
        "details": results
    })

@app.route('/api/servers')
def get_servers():
    url = "https://panel.simrail.eu:8084/servers-open"
    res = HTTP.get(url, timeout=5, verify=False).json()
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
    res = HTTP.get(f"https://api1.aws.simrail.eu:8082/api/getTimeZone?serverCode={server_id}")
    return jsonify({"timezone": res.text.strip()})


@app.route('/api/data')
@app.route('/api/my_train_data')
def get_train_data():
    server_id = request.args.get('server', 'en1')
    train_num = request.args.get('number')
    
    if not train_num:
        return jsonify({"error": "No train number provided"}), 400

    ACTIVE_SERVER_STATE["server_id"] = server_id

    try:
        # 1. Haal Live Treinen Data op (voor basisinfo & overig verkeer)
        # Komt uit de achtergrond-cache (~1.5s vers) i.p.v. elke poll opnieuw op te halen.
        live_url = f"https://panel.simrail.eu:8084/trains-open?serverCode={server_id}"
        live_res = get_cached_or_fetch("trains", live_url, server_id, verify_ssl=False, max_age=3) or {}
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
        
        for idx, pt in enumerate(raw_timetable):
            name = pt.get('nameForPerson', '')
            stop_type_raw = pt.get('stopType', 'NoStopOver')
            supervised_by = pt.get('supervisedBy')
            
            dispatcher_type = ""
            if supervised_by:
                dispatcher_type = "laptop"
                if supervised_by in stations_dict:
                    disp_info = stations_dict[supervised_by].get('DispatchedBy', [])
                    if disp_info and len(disp_info) > 0:
                        dispatcher_type = "user"

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
                    is_passed = True
                else:
                    act_dep_str = format_api_time(dep_str)
            else:
                if arr_str:
                    planned_arr = datetime.strptime(arr_str, "%Y-%m-%d %H:%M:%S")
                    actual_arr = planned_arr + timedelta(minutes=upcoming_edr_delay)
                    act_arr_str = actual_arr.strftime("%H:%M")
                
                if dep_str:
                    planned_dep = datetime.strptime(dep_str, "%Y-%m-%d %H:%M:%S")
                    actual_dep = planned_dep + timedelta(minutes=upcoming_edr_delay)
                    act_dep_str = actual_dep.strftime("%H:%M")
                    
                try:
                    now_time = datetime.now().strftime("%H:%M")
                    dep_compare = act_dep_str if act_dep_str != "--:--" else format_api_time(dep_str)
                    is_passed = dep_compare < now_time
                except:
                    is_passed = False

            is_current = (idx == current_station_index)
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

        berekende_seinen = []
        if live_signal_name:
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
                    # HIER GEBEURT HET: Kleur het sein in met de voorwaarde van de voorligger
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
            "map_elements": berekende_seinen
        })

    except Exception as e:
        logger.error(f"Error in get_train_data: {e}")
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

    # Alleen relevant voor lokale dev-runs (python app.py): stuurt logger-output
    # naar de console, want dat gebeurt anders alleen als launcher.py eerst
    # setup_logging() heeft aangeroepen (zie log_setup.py).
    if IS_REAL_SERVER_PROCESS:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

    if IS_REAL_SERVER_PROCESS:
        load_train_data()
        load_vehicles_db()

    if IS_REAL_SERVER_PROCESS:
        start_background_loops()

    app.run(debug=DEBUG_MODE, host='0.0.0.0', port=5000, threaded=True)