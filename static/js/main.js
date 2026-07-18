/* ==========================================================================
   SIMTIM MAIN CORE (App State, Event Listeners & Timers)
   ========================================================================== */

   /* ================== SAFE DOM HELPER ================== */
function el(id) {
    const element = document.getElementById(id);

    if (!element) {
        console.warn(`[UI] Missing element: #${id}`);
    }

    return element;
}

/* ================== CENTRAL STATE ================== */
const state = {
    server: null,
    train: "",
    trainType: "-"
};

let themes = ['night', 'dusk', 'day'];
let themeIndex = 0;

let serverTime = null;
let lastSync = null;
let lastApiData = null;
let syncIntervalId = null;

let _bootStarted = false;

/* ================== BOOT ENTRY ================== */
document.addEventListener('DOMContentLoaded', () => {
    console.log("[BOOT] DOM loaded");

    if (_bootStarted) return;
    _bootStarted = true;

    requestAnimationFrame(() => {
        waitForUI([
            'server',
            'conn-indicator',
            'train-label',
            'mapCanvas',
            'timetable-body',
            'route-id',
            'v-act',
            'server-time',
            'delay'
        ], 5000)
        .then(initApp)
        .catch(err => console.error("[BOOT] UI timeout:", err));
    });
});

/* ================== UI WAIT CORE ================== */
function waitForUI(requiredIds, timeout = 5000) {
    return new Promise((resolve, reject) => {
        const start = Date.now();

        const check = () => {
            const missing = requiredIds.filter(id => !document.getElementById(id));

            if (missing.length === 0) {
                resolve(true);
                return;
            }

            if (Date.now() - start > timeout) {
                reject(`Missing UI: ${missing.join(', ')}`);
                return;
            }

            requestAnimationFrame(check);
        };

        check();
    });
}

/* ================== APP INIT ================== */
async function initApp() {
    console.log("[BOOT] UI ready → starting app");

    setupCoreListeners();
    await initServers();

    startIntervals();

    syncDiagnosticsHealth();
    tickLocalClock();
}

/* ================== INTERVAL SYSTEM ================== */
function startIntervals() {
    if (window._simtimIntervalsStarted) return;
    window._simtimIntervalsStarted = true;

    setInterval(syncActiveData, 2000);
    setInterval(syncServerClockTime, 60000);
    setInterval(tickLocalClock, 1000);
    setInterval(syncDiagnosticsHealth, 30000);
}

/* ================== SAFE LISTENERS ================== */
function setupCoreListeners() {
    if (typeof lucide !== 'undefined') lucide.createIcons();

    // 1. Listener voor de API-status knop
    const conn = el('conn-indicator');
    conn?.addEventListener('click', () => {
        SimRailUI.renderStatusPopup(lastApiData);
    });

    // 2. Listener voor de Server Dropdown selectie
    const select = el('server');
select?.addEventListener('change', (e) => {
    state.server = e.target.value;
    console.log(`[STATE] Server gewijzigd naar: ${state.server}`);
    
    setTimeout(() => {
        select.blur();
    }, 100);

    syncServerClockTime();
});

    window.addEventListener('offline', () => SimRailUI.setStatus('offline'));
    window.addEventListener('online', () => SimRailUI.setStatus('online'));
}

/* ================== SERVER INIT ================== */
async function initServers() {
    const select = el('server');
    if (!select) return;

    try {
        select.innerHTML = '<option value="" disabled selected>SERVER</option>';

        const response = await SimRailAPI.getServers();
        console.log("[BOOT] Ruwe serverdata ontvangen:", response);

        const serverLijst = response?.data;

        if (serverLijst && Array.isArray(serverLijst)) {
            let toegevoegd = 0;
            
            serverLijst.forEach(srv => {
                if (srv.IsActive === true) { 
                    const opt = document.createElement('option');
                    opt.value = srv.ServerCode; 
                    opt.textContent = srv.ServerName;
                    select.appendChild(opt);
                    toegevoegd++;
                }
            });
            console.log(`[BOOT] Servers succesvol geladen! ${toegevoegd} actieve servers toegevoegd.`);
        } else {
            console.error("[initServers] Geen geldige data-array gevonden in API response.");
        }
    } catch (err) {
        console.error("[initServers] error:", err);
    }
}

/* ================== INTERACTIVE NUMPAD ACTIONS ================== */
function handleTrainClick() {
    if (!state.server) {
        document.getElementById('train-label').innerText = "Select a server first";
        return;
    }
    
    if (syncIntervalId) {
        clearInterval(syncIntervalId);
        syncIntervalId = null;
        SimRailUI.logToCMD(`SYNC_STOP: Oude tracking stopgezet voor invoer`, 'info');
    }

    state.train = ""; 
    document.getElementById('train-label').innerText = "_";
    document.getElementById('numpad').style.display = 'block';
}

function addNum(n) {
    if (!state.server) return;
    state.train += n;
    document.getElementById('train-label').innerText = state.train;
}

function clearNum() {
    state.train = "";
    document.getElementById('train-label').innerText = "Select train";
}

async function confirmNum() {
    document.getElementById('numpad').style.display = 'none';
    if (!state.train) return;

    const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

    try {
        SimRailUI.setStatus('loading');
        
        const dataPromise = syncActiveData();
        
        // STAP 1: Connecting to server
        SimRailUI.logToCMD(`Connecting to server (${state.server.toUpperCase()})`, 'white', true);
        await sleep(200); 

        // STAP 2: Connected to server
        SimRailUI.logToCMD(`Connected to server (${state.server.toUpperCase()})`, 'green');
        await sleep(200);

        // STAP 3: Input train number
        SimRailUI.logToCMD(`Input train number (${state.train})`, 'white');
        await sleep(200);

        // API Aanroep
        const data = await dataPromise;

        // STAP 4: Train found check
        if (data && !data.error) {
            SimRailUI.logToCMD(`Train ${state.train} found`, 'green');
            await sleep(200);
        } else {
            SimRailUI.logToCMD(`Train ${state.train} not found`, 'error');
            throw new Error("Train not found on server");
        }

        // STAP 5: Retrieving data
        SimRailUI.logToCMD(`Retrieving data`, 'white');
        await sleep(200);

        // --- HIER KOMEN DE SUGGESTIES ALS SUB-REGELS ---
        
        // 1. De Route
        SimRailUI.logToCMD(`MAPPING_ROUTE: ${data.route || 'UNKNOWN TRAJECTORY'}`, 'sub');
        
        // 2. Het Treintype
        const typeZonderStreepje = data.train_type || data.trainType || 'COMMUTER';
        SimRailUI.logToCMD(`TRAIN_TYPE: ${typeZonderStreepje}`, 'sub');
        
        // 3. Vertraging met status-tekst
        const delayVal = parseInt(data.delay) || 0;
        let delayStatus = "ON TIME";
        if (delayVal > 0) delayStatus = `+${delayVal} MIN DELAY`;
        if (delayVal < 0) delayStatus = `${delayVal} MIN AHEAD`;
        SimRailUI.logToCMD(`START_STATUS: ${delayStatus}`, 'sub');

        // 4. Aantal stations in de timetable tellen
        const totalStops = (data.timetable && data.timetable.length) || 0;
        SimRailUI.logToCMD(`TIMETABLE: ${totalStops} scheduled entries compiled`, 'sub');

        const timetableContainer = document.getElementById('timetable-scroll-area');
        if (timetableContainer) {
            timetableContainer.innerHTML = '';
            
            const oudePinnedHeader = timetableContainer.parentElement.querySelector('.pinned-final-destination');
            if (oudePinnedHeader) {
                oudePinnedHeader.remove();
            }
        }

        SimRailUI.renderTimetable(data.timetable, data.game_end_station, { serverTime: getCurrentServerTimeHHMM() });
        
        // 5. Eerstvolgende stop bepalen (als er een timetable is)
        if (data.timetable && data.timetable.length > 0) {
            // Zoek naar de stop die gemarkeerd staat als 'is_next'
            const nextStop = data.timetable.find(s => s.is_next) || data.timetable[0];
            if (nextStop && nextStop.name) {
                SimRailUI.logToCMD(`NEXT_STOP: ${nextStop.name} (Track ${nextStop.track || '?'})`, 'sub');
            }
        }

        await sleep(200);

        // STAP 6: All data received
        SimRailUI.logToCMD(`All data received`, 'white');
        
        // Afronden en knop tonen
        SimRailUI.setStatus('online');
        const footer = document.getElementById('cmd-footer');
        if (footer) footer.style.display = 'block';

    } catch (err) {
        SimRailUI.logToCMD(`FATAL_EXCEPTION: CONNECTION TERMINATED`, 'error');
        const footer = document.getElementById('cmd-footer');
        if (footer) footer.style.display = 'block';
        SimRailUI.setStatus('offline');
    }
}

function closeStatusModal() {
    const modal = document.getElementById('status-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

window.closeCMD = function() {
    const screen = document.getElementById('cmd-overlay');
    const footer = document.getElementById('cmd-footer');
    
    if (screen) {
        screen.style.display = 'none';
    }
    if (footer) {
        footer.style.display = 'none'; // Zet de footer alvast weer op hidden voor de volgende keer
    }
    console.log("[CMD] Terminal gesloten door gebruiker.");
};

/* ================== CORE DATA INTERVAL SYNC ================== */
async function syncActiveData() {
    if (!state.train || !state.server) return null;

    try {
        console.log(`[Sync] Fetching data for: ${state.server} / ${state.train}`);
        
        const data = await SimRailAPI.getTrainData(state.server, state.train);

        if (data.error) {
            console.warn("[Sync] Server returned error:", data.error);
            return data;
        }

        console.log("[Sync] Data received successfully.");

        if (data.server_time_unix && !serverTime) {
            serverTime = new Date(data.server_time_unix * 1000);
            lastSync = Date.now();
        }

        // Update Dashboard
        SimRailUI.updateDashboardFields(data);
        state.trainType = data.train_type || "-";
        SimRailUI.updateTrainLabel(state);
        SimRailUI.renderMap(data);  
        SimRailUI.renderTimetable(data.timetable, data.game_end_station, { serverTime: getCurrentServerTimeHHMM() });
        SimRailUI.updateInfoView(data);
        
        return data;
    } catch (e) {
        console.error("Fout tijdens data sync:", e);
        return null;
    }
}

/* ================== TIME KEEPING LOGIC ================== */
async function syncServerClockTime() {
    if (!state.server) return;
    try {
        const data = await SimRailAPI.getServerTime(state.server);
        if (data.time) {
            serverTime = new Date(data.time); 
            lastSync = Date.now();
        }
    } catch (e) { console.error("Servertijd synchronisatie mislukt:", e); }
}

function getCurrentServerTimeHHMM() {
    if (!serverTime || !lastSync) return undefined;

    const now = Date.now();
    const diff = now - lastSync;
    const current = new Date(serverTime.getTime() + diff);

    let hours = current.getUTCHours();
    if (hours >= 24) hours -= 24;
    if (hours < 0) hours += 24;

    const h = String(hours).padStart(2, '0');
    const m = String(current.getUTCMinutes()).padStart(2, '0');
    return `${h}:${m}`;
}

function tickLocalClock() {
    if (!serverTime || !lastSync) return;

    const now = Date.now();
    const diff = now - lastSync;
    const current = new Date(serverTime.getTime() + diff);

    let hours = current.getUTCHours(); 
    if (hours >= 24) hours -= 24;
    if (hours < 0) hours += 24;

    const h = String(hours).padStart(2, '0');
    const m = String(current.getUTCMinutes()).padStart(2, '0');
    const s = current.getUTCSeconds();
    const colon = (s % 2 === 0) 
        ? '<span style="opacity: 1;">:</span>' 
        : '<span style="opacity: 0;">:</span>';

    SimRailUI.renderClock(h, `${colon}${m}`);
}

async function syncDiagnosticsHealth() {
    try {
        const data = await SimRailAPI.getSystemHealth(state.server, state.train);
        lastApiData = data.details;
        SimRailUI.setStatus(data.overall);
    } catch (error) {
        SimRailUI.setStatus('offline');
        lastApiData = { "Systeem-Server": "offline" };
    }
}

/* ================== THEME ================== */
function toggleTheme() {
    themeIndex = (themeIndex + 1) % themes.length;
    document.body.setAttribute('data-theme', themes[themeIndex]);
}