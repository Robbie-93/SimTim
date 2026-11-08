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
    trainType: "-",
    // Wordt false zolang het numpad open staat en de gebruiker nog aan het
    // typen is, en pas true na confirmNum(). Voorkomt dat de periodieke
    // syncActiveData-interval (elke 2s) al data ophaalt op basis van een
    // nog niet-bevestigd, gedeeltelijk ingetypt treinnummer.
    confirmed: true,
    // Treinnummer dat actief was voordat het numpad openging; wordt hersteld bij lege invoer
    prevTrain: ""
};

let themes = ['night', 'dusk', 'day'];
let themeIndex = 0;

let _syncInFlight = false;
let _healthInFlight = false;
let _healthPending = false; // refresh gevraagd terwijl er al een check liep
let serverTime = null;
let lastSync = null;
let lastApiData = null;
let syncIntervalId = null;

let _bootStarted = false;
let _confirmInFlight = false;

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
    // API-status direct verversen voor de nieuwe server
    syncDiagnosticsHealth();
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

    // Vorige trein onthouden; polling pauzeert zolang confirmed false is
    if (state.confirmed && state.train) state.prevTrain = state.train;

    state.train = ""; 
    state.confirmed = false;
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

    // Dubbel tikken: tweede sequentie negeren zolang de eerste loopt
    if (_confirmInFlight) return;

    // Lege invoer: vorige trein herstellen en gewoon verder pollen
    if (!state.train) {
        state.train = state.prevTrain || "";
        state.confirmed = true;
        SimRailUI.updateTrainLabel(state);
        console.log(`[Confirm] Empty input, restored previous train: ${state.train || 'none'}`);
        return;
    }

    _confirmInFlight = true;
    // confirmed blijft false tot de trein gevonden is, zodat de 2s-interval niet meeloopt
    state.confirmed = false;

    const server = state.server;
    const train = state.train;
    const serverLabel = server.toUpperCase();
    const footer = document.getElementById('cmd-footer');

    // Leesbare foutmelding voor fetch-fouten (timeout of backend onbereikbaar)
    const describeFetchError = (err) => {
        if (err && err.name === 'AbortError') return 'Connection timed out';
        if (err instanceof TypeError) return 'SimTim backend not reachable';
        return (err && err.message) ? err.message : String(err);
    };

    // Mislukte invoer: alles leegmaken en niet verder pollen
    const failAndReset = () => {
        state.train = "";
        state.prevTrain = "";
        state.trainType = "-";
        state.confirmed = true; // geen trein = interval doet niets
        SimRailUI.clearDashboard();
        SimRailUI.updateTrainLabel(state);
        SimRailUI.setStatus('offline');
    };

    try {
        SimRailUI.setStatus('loading');
        if (footer) footer.style.display = 'none';

        // STAP 1 + 2: echte verbindingscheck met de gekozen SimRail-server
        SimRailUI.logToCMD(`Connecting to server (${serverLabel})`, 'white', true);
        let timeRes;
        try {
            timeRes = await SimRailAPI.getServerTime(server);
        } catch (err) {
            SimRailUI.logToCMD(`Not connected to server (${serverLabel})`, 'error');
            SimRailUI.logToCMD(`ERROR: ${describeFetchError(err)}`, 'error');
            console.error('[Confirm] Server connection failed:', err);
            failAndReset();
            return;
        }
        if (!timeRes || timeRes.error) {
            SimRailUI.logToCMD(`Not connected to server (${serverLabel})`, 'error');
            SimRailUI.logToCMD(`ERROR: ${timeRes?.error || 'No response from SimRail'}`, 'error');
            console.error('[Confirm] Server time check returned error:', timeRes);
            failAndReset();
            return;
        }
        SimRailUI.logToCMD(`Connected to server (${serverLabel})`, 'green');

        // STAP 3: treinnummer
        SimRailUI.logToCMD(`Input train number (${train})`, 'white');

        // STAP 4: trein opvragen (eigen request, los van de interval-poll)
        let result;
        try {
            result = await SimRailAPI.getTrainDataWithStatus(server, train);
        } catch (err) {
            SimRailUI.logToCMD(`ERROR: ${describeFetchError(err)}`, 'error');
            console.error('[Confirm] Train data request failed:', err);
            failAndReset();
            return;
        }

        if (result.status === 404) {
            SimRailUI.logToCMD(`Train ${train} not found`, 'error');
            if (result.data?.error) SimRailUI.logToCMD(result.data.error, 'sub');
            console.warn(`[Confirm] Train ${train} not found: ${result.data?.error}`);
            failAndReset();
            return;
        }
        if (!result.ok || !result.data || result.data.error) {
            SimRailUI.logToCMD(`ERROR: ${result.data?.error || 'Unknown error'} (HTTP ${result.status})`, 'error');
            console.error('[Confirm] Train data error:', result);
            failAndReset();
            return;
        }
        SimRailUI.logToCMD(`Train ${train} found`, 'green');

        // STAP 5: ontvangen data verwerken in dashboard, kaart, rittenstaat en info
        SimRailUI.logToCMD(`Retrieving data`, 'white');
        SimRailUI.clearDashboard(); // restanten van de vorige trein weg
        applyTrainData(result.data);

        // STAP 6: klaar, interval mag weer pollen
        state.confirmed = true;
        state.prevTrain = train;
        SimRailUI.logToCMD(`All data received`, 'white');
        SimRailUI.setStatus('online');

    } catch (err) {
        // Onverwachte fout (bijv. in het renderen)
        SimRailUI.logToCMD(`ERROR: ${err?.message || err}`, 'error');
        console.error('[Confirm] Unexpected error:', err);
        failAndReset();
    } finally {
        _confirmInFlight = false;
        if (footer) footer.style.display = 'block';        // Icoon en API STATUS direct gelijktrekken met de werkelijke verbindingsstatus
        syncDiagnosticsHealth();
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
    if (!state.train || !state.server || !state.confirmed) return null;

    if (_syncInFlight) {
        console.warn("[Sync] Skipping poll: previous request still in flight");
        return null;
    }
    _syncInFlight = true;

    // Trein/server van dit request vastleggen, om verouderde antwoorden te herkennen
    const reqTrain = state.train;
    const reqServer = state.server;

    try {
        console.log(`[Sync] Fetching data for: ${reqServer} / ${reqTrain}`);
        
        const data = await SimRailAPI.getTrainData(reqServer, reqTrain);

        // Inmiddels andere trein gekozen of invoer bezig: dit antwoord niet meer tonen
        if (state.train !== reqTrain || state.server !== reqServer || !state.confirmed) {
            console.log(`[Sync] Discarding stale response for ${reqServer} / ${reqTrain}`);
            return null;
        }

        if (data.error) {
            console.warn("[Sync] Server returned error:", data.error);
            return data;
        }

        console.log("[Sync] Data received successfully.");
        applyTrainData(data);
        
        return data;
    } catch (e) {
        console.error("Fout tijdens data sync:", e);
        return null;
    } finally {
        _syncInFlight = false;
    }
}

/* Verwerkt een treindata-response in alle UI-onderdelen (gedeeld door sync en confirmNum) */
function applyTrainData(data) {
    if (data.server_time_unix && !serverTime) {
        serverTime = new Date(data.server_time_unix * 1000);
        lastSync = Date.now();
    }

    SimRailUI.updateDashboardFields(data);
    state.trainType = data.train_type || "-";
    SimRailUI.updateTrainLabel(state);
    // Eerst bijhouden of er een volgend sein is, zodat renderMap de banner meteen meeneemt
    SimRailUI.updateSignalLoss(data);
    SimRailUI.renderMap(data);  
    SimRailUI.renderTimetable(data.timetable, data.game_end_station, { serverTime: getCurrentServerTimeHHMM() });
    SimRailUI.updateInfoView(data);

    // Perronwijziging t.o.v. dienstregeling: popup (één keer per spoor/perron)
    if (data.platform_notice) {
        SimRailUI.showPlatformNotice(data.platform_notice, state.train);
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
    // Loopt er al een check of is confirmNum bezig: refresh onthouden en straks alsnog uitvoeren
    if (_healthInFlight || _confirmInFlight) {
        _healthPending = true;
        return;
    }
    _healthInFlight = true;

    try {
        const data = await SimRailAPI.getSystemHealth(state.server, state.train);

        // Host = de SimTim-backend zelf; bereikbaar want er kwam antwoord
        const details = (data && data.details) ? data.details : {};
        lastApiData = { "Host": "online", ...details };

        const overall = ['online', 'partial', 'offline'].includes(data?.overall) ? data.overall : 'partial';
        SimRailUI.setStatus(overall);
        if (overall !== 'online') console.warn(`[Health] ${new Date().toLocaleTimeString([], { hour12: false })} Status ${overall}:`, lastApiData);
    } catch (error) {
        // Backend niet bereikbaar: de overige checks zijn dan onbekend
        console.error(`[Health] ${new Date().toLocaleTimeString([], { hour12: false })} SimTim backend not reachable:`, error);
        lastApiData = { "Host": "offline" };
        SimRailUI.setStatus('offline');
    } finally {
        _healthInFlight = false;
        // Was er tussendoor een refresh gevraagd, dan direct een verse check met de actuele trein
        if (_healthPending && !_confirmInFlight) {
            _healthPending = false;
            syncDiagnosticsHealth();
        }
    }
}

/* ================== THEME ================== */
function toggleTheme() {
    themeIndex = (themeIndex + 1) % themes.length;
    document.body.setAttribute('data-theme', themes[themeIndex]);
}