/* ==========================================================================
   SIMTIM UI LAYER (DOM Manipulatie & Canvas Rendering)
   ========================================================================== */
let lastLoadedTrainNumber = null;

const SimRailUI = {
    _autoScrollLocked: true,
    _mapViewRange: 5000, 
    _lastMapData: null, 

    /* Update wifi-indicator status */
    setStatus(status) {
        const ind = document.getElementById('conn-indicator');
        const wifiIcon = ind?.querySelector('i, svg');
        if (!ind || !wifiIcon) return;

        if (status === 'online') {
            ind.style.color = "#008000";
            wifiIcon.setAttribute('data-lucide', 'wifi');
        } else if (status === 'offline') {
            ind.style.color = "#ff4444";
            wifiIcon.setAttribute('data-lucide', 'wifi-off');
        } else {
            ind.style.color = "#ffaa00";
            wifiIcon.setAttribute('data-lucide', 'wifi-high');
        }
        if (typeof lucide !== 'undefined') lucide.createIcons();
    },

    /* API Status display */
    renderStatusPopup(lastApiData) {
        const detailsDiv = document.getElementById('status-details');
        const modal = document.getElementById('status-modal');
        if (!detailsDiv || !modal) return;

        detailsDiv.innerHTML = '';

        if (lastApiData) {
            Object.entries(lastApiData).forEach(([apiName, status]) => {
                let color = "#ff4444";
                let msg = "NO CONNECTION";

                if (status === "online") { color = "#008000"; msg = "ONLINE"; }
                else if (status === "error") { color = "#ffaa00"; msg = "SOMETHING WENT WRONG"; }

                const row = document.createElement('div');
                row.style.cssText = "display: flex; justify-content: space-between; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 1px solid #222; font-family: 'Courier New', monospace; font-size: 13px;";
                row.innerHTML = `
                    <span style="color: #888;">${apiName.toUpperCase()}</span>
                    <span style="color: ${color}; font-weight: normal;">${msg}</span>
                `;
                detailsDiv.appendChild(row);
            });
        } else {
            detailsDiv.innerHTML = '<div style="color: #666; font-family: monospace;">INITIALIZING DIAGNOSTICS...</div>';
        }
        modal.style.display = 'block';
    },

    /* Updates topbar train labels */
    updateTrainLabel(state) {
        const label = document.getElementById('train-label');
        const type = document.getElementById('train-type');
        if (!label || !type) return;

        if (!state.server) {
            label.innerText = "Select service";
            type.classList.add("hidden");
            return;
        }
        if (!state.train) {
            label.innerText = "Select train";
            type.classList.add("hidden");
            return;
        }

        label.innerText = `Train: ${state.train}`;
        if (state.trainType && state.trainType !== "-") {
            type.innerText = state.trainType;
            type.classList.remove("hidden");
        } else {
            type.classList.add("hidden");
        }
    },

    /* Updates elements in Top and Bottom bar */
    updateDashboardFields(data) {
        document.getElementById('v-act').innerText = data.speed;
        document.getElementById('route-id').innerText = data.route;

        const delayEl = document.getElementById('delay');
        const delayVal = data.delay;
        delayEl.innerText = delayVal;

        if (delayVal < -1) delayEl.style.color = "#008000";
        else if (delayVal >= -1 && delayVal <= 1) delayEl.style.color = "var(--text-color)";
        else if (delayVal > 1 && delayVal <= 5) delayEl.style.color = "#ffaa00";
        else delayEl.style.color = "#ff4444";
    },

    /* CMD Terminal console input */
    logToCMD(message, status = 'white', showScreen = false) {
        const screen = document.getElementById('cmd-overlay');
        const log = document.getElementById('cmd-log');
        if (!screen || !log) return;
        
        if (showScreen && screen.style.display !== 'flex') {
            log.innerHTML = "";
            screen.style.display = 'flex';
        }

        const oldBlock = log.querySelector('.cmd-loading-block');
        if (oldBlock) oldBlock.classList.remove('cmd-loading-block');

        const timestamp = new Date().toLocaleTimeString([], { hour12: false });
        
        let color = "#ffffff";
        if (status === 'green') color = "#008000";
        if (status === 'error') color = "#ff4444";
        if (status === 'warn') color = "#ffaa00";
        if (status === 'sub') color = "#888888"; 

        const line = document.createElement('div');
        
        if (status === 'sub') {
            line.innerHTML = `&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span style="color: ${color};">${message}</span>`;
        } else {
            line.innerHTML = `<span style="color: #555;">[${timestamp}]</span> <span style="color: ${color};">> ${message}</span>`;
        }

        if (status === 'white') {
            line.className = "cmd-loading-block";
        }
        
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
    },

    /* Renders digital en tikking clock */
    renderClock(hours, minutes) {
        document.getElementById('server-time').innerHTML = `${hours}${minutes}`;
    },

    /* Schakelt tussen de zoom-niveaus en update de indicator */
    setMapZoom(direction) {
        const steps = [3000, 5000, 7000, 10000];
        let currentIndex = steps.indexOf(this._mapViewRange);
        if (currentIndex === -1) currentIndex = 1; // Fallback naar 5000

        if (direction === 'in' && currentIndex > 0) {
            this._mapViewRange = steps[currentIndex - 1];
        } else if (direction === 'out' && currentIndex < steps.length - 1) {
            this._mapViewRange = steps[currentIndex + 1];
        } else if (direction === 'reset') {
            this._mapViewRange = 5000;
        }

        // Update de kleine afstand-indicator onderin de zijbalk
        const rangeEl = document.getElementById('map-range-val');
        if (rangeEl) {
            rangeEl.innerText = `${this._mapViewRange / 1000} km`;
        }

        if (this._lastMapData) {
            this.renderMap(this._lastMapData);
        }
    },

    /* Look Ahead in HTML5 Canvas */
renderMap(data) {
        if (!data) return;
        this._lastMapData = data; // Cache data zoom-updates

        const canvas = document.getElementById('mapCanvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');

        const dpr = window.devicePixelRatio || 1;
        const cssWidth = canvas.clientWidth;
        const cssHeight = canvas.clientHeight;
        canvas.width = cssWidth * dpr;
        canvas.height = cssHeight * dpr;
        ctx.scale(dpr, dpr);

        const centerX = cssWidth / 2;
        const viewRange = this._mapViewRange; // <-- NU DYNAMISCH
        const pixelsPerMeter = cssHeight / viewRange;
        const trainY = cssHeight - 50;

        ctx.clearRect(0, 0, cssWidth, cssHeight);

        ctx.strokeStyle = document.body.getAttribute('data-theme') === 'day' ? 'rgba(0,0,0,0.1)' : 'rgba(255,255,255,0.1)';
        ctx.lineWidth = 1;
        ctx.font = '10px Arial';
        ctx.fillStyle = '#666';

        // DYNAMIC  HORIZONTAL KMLINES
        for (let m = 0; m <= viewRange; m += 1000) {
            const y = trainY - (m * pixelsPerMeter);
            if (y < 0) continue;
            
            ctx.beginPath(); 
            ctx.moveTo(0, y); 
            ctx.lineTo(cssWidth, y); 
            ctx.stroke();
            ctx.fillText((m / 1000) + " km", 10, y - 5);
        }

        // ==================================================================
        // LAAG 1: LAAG MET EMPLACEMENT-BLOKKEN OVER DE HELE BREEDTE (PRE-PASS)
        // ==================================================================
        if (data.map_elements) {
            for (let i = 0; i < data.map_elements.length; i++) {
                const el = data.map_elements[i];
                
                // Als dit het begin is van een emplacement, kleur de hele breedte tot het volgende sein
                if (el.type === 'signal' && el.is_emplacement) {
                    const startY = trainY - (el.dist * pixelsPerMeter);
                    if (startY < 0) continue;

                    let endY = 0;
                    for (let j = i + 1; j < data.map_elements.length; j++) {
                        if (data.map_elements[j].type === 'signal') {
                            endY = trainY - (data.map_elements[j].dist * pixelsPerMeter);
                            break;
                        }
                    }
                    if (endY < 0) endY = 0;

                    const blockHeight = startY - endY; 

                    if (el.emplacement_code) {
                        ctx.save();
                        ctx.fillStyle = 'var(--text-muted)';
                        ctx.font = '1.2Em "Courier New", Courier, monospace';
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'middle';
                        // Geplaatst aan de linkerkant van het spoor (kwart van de canvasbreedte)
                        ctx.fillText(el.emplacement_code, centerX / 2, endY + (blockHeight / 2));
                        ctx.restore();
                    }
                    // WIJZIGING: Teken over de VOLLEDIGE breedte (van X=0 tot X=cssWidth)
                    ctx.fillStyle = document.body.getAttribute('data-theme') === 'day' 
                        ? 'rgba(0, 100, 255, 0.05)'  // Heel lichtblauw overdag
                        : 'rgba(255, 255, 255, 0.07)';    // Strak CMD Slate-Blue 's nachts
                    ctx.fillRect(0, endY, cssWidth, blockHeight);
                }
            }
        }

        // Main Track
        ctx.strokeStyle = '#444'; ctx.lineWidth = 4;
        ctx.beginPath(); ctx.moveTo(centerX, 0); ctx.lineTo(centerX, cssHeight); ctx.stroke();

        // ==================================================================
        // LAAG 2 & 3: ELEMENTEN EN TEXT VOORGROND
        // ==================================================================
        if (data.map_elements) {
            const absOverrides = this._computeAbsCascade(data.map_elements);

            data.map_elements.forEach(el => {
                const y = trainY - (el.dist * pixelsPerMeter);
                if (y < 0) return;

                if (el.type === 'platform') {
                    ctx.fillStyle = '#666'; ctx.fillRect(centerX + 8, y - 20, 10, 40);
                    ctx.fillStyle = '#aaa'; ctx.font = '10px Arial';
                    ctx.fillText(el.name, centerX + 22, y + 4);
                }
                else if (el.type === 'signal') {
                    // 1. De montagesteun
                    ctx.strokeStyle = document.body.getAttribute('data-theme') === 'day' ? '#333' : '#444';
                    ctx.lineWidth = 2;
                    ctx.beginPath(); 
                    ctx.moveTo(centerX, y); 
                    ctx.lineTo(centerX + 20, y); 
                    ctx.stroke();

                    // 2. De basis seinkop
                    let dotColor = '#2a2a2a';  
                    let drawLine = false;      
                    let lineColor = '';        

                    // Een sein dat we al gepasseerd zijn (dist < 0) beschermt nu het
                    // blok waar wijzelf in staan - dat moet dus altijd rood tonen,
                    // ongeacht wat de API/cascade eerder liet zien, totdat het sein
                    // buiten beeld scrollt. Dit heeft dus voorrang op alles hieronder.
                    const isPassedSignal = el.dist < 0;

                    // ABS-blokcascade heeft voorrang: als dit ABS-sein op basis van
                    // afgeleide blokbezetting rood/geel/knipperend-groen/groen moet
                    // zijn, gebruiken we dat i.p.v. de (nog onbekende) el.speed.
                    const absOverride = absOverrides.get(el);

                    if (isPassedSignal) {
                        dotColor = '#fc1010';
                    } else if (absOverride) {
                        if (absOverride.mode === 'red') {
                            dotColor = '#fc1010';
                        } else if (absOverride.mode === 'yellow') {
                            dotColor = '#facc15';
                        } else if (absOverride.mode === 'flash-green') {
                            dotColor = (Math.floor(Date.now() / 500) % 2 === 0) ? '#008000' : '#2a2a2a';
                        } else if (absOverride.mode === 'green') {
                            dotColor = '#008000';
                        }
                    } else if (el.speed === 'Vmax') {
                        dotColor = '#008000';
                    } else if (el.speed === 130) {
                        dotColor = (Math.floor(Date.now() / 500) % 2 === 0) ? '#008000' : '#2a2a2a';  
                    } else if (el.speed === 100) {
                        dotColor = '#facc15';  
                        drawLine = true;
                        lineColor = '#008000'; 
                    } else if (el.speed === 60) {
                        dotColor = '#facc15';  
                        drawLine = true;
                        lineColor = '#facc15';
                    } else if (el.speed === 50) {
                        dotColor = (Math.floor(Date.now() / 500) % 2 === 0) ? '#facc15' : '#2a2a2a';   
                    } else if (el.speed === 40) {
                        dotColor = '#facc15';  
                    } else if (el.speed === 0) {
                        dotColor = '#fc1010';  
                    }

                    const isAbsSein = el.is_abs === true || el.is_abs === 1 || el.is_abs === 'yes' || el.is_abs === 'ja';

                    if (!isAbsSein) {
                        if (!drawLine) {
                            drawLine = true;
                            lineColor = '#2a2a2a';
                        }
                    }

                    // A. Teken het ronde bolletje
                    ctx.fillStyle = dotColor; 
                    ctx.beginPath(); 
                    ctx.arc(centerX + 30, y, 7, 0, Math.PI * 2); 
                    ctx.fill();

                    // B. Teken de horizontale streep eronder
                    if (drawLine) {
                        ctx.strokeStyle = lineColor;
                        ctx.lineWidth = 3; 
                        ctx.beginPath();
                        ctx.moveTo(centerX + 23, y + 12); 
                        ctx.lineTo(centerX + 37, y + 12); 
                        ctx.stroke();
                    }

                    // 3. Tekst-styling naar de RECHTS van het sein
                    ctx.textAlign = 'left'; 
                    const textX = centerX + 45; 

                    // --- LAAG 1: Seinnaam ---
                    ctx.font = '0.7Em "Courier New", Courier, monospace';
                    ctx.fillStyle = document.body.getAttribute('data-theme') === 'day' ? '#333333' : '#bbbbbb';
                    ctx.fillText(el.name, textX, y - 11);

                    // --- LAAG 2: Snelheid ---
                    if (el.speed !== 32767 && el.speed !== undefined) {
                        ctx.font = '0.8Em "Courier New", Courier, monospace';
                        let speedText = el.speed === 'Vmax' ? 'Vmax' : el.speed + " km/h";
                        ctx.fillText(speedText, textX, y + 3);
                    }

                    // --- LAAG 3: Afstand tot sein ---
                    ctx.font = '0.7Em "Courier New", Courier, monospace'; 
                    ctx.fillStyle = '#888888';
                    ctx.fillText(el.dist + "m", textX, y + 15);
                }
                // ==================================================================
                // TRAFFIC-LAAG
                // ==================================================================
                else if (el.type === 'traffic') {
                    ctx.fillStyle = '#ff9900';
                    ctx.fillRect(centerX - 6, y, 12, 20);

                    const visualCenterY = y + 10;

                    // Verbindingslijntje naar links vanuit het midden van de trein
                    ctx.strokeStyle = document.body.getAttribute('data-theme') === 'day' ? '#ff9900' : '#cc7700';
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(centerX - 6, visualCenterY);
                    ctx.lineTo(centerX - 18, visualCenterY);
                    ctx.stroke();

                    // Tekst-styling strak uitgelijnd aan de LINKERKANT van het spoor
                    ctx.textAlign = 'right';
                    const trafficTextX = centerX - 24;

                    // --- LAAG 1: Treinnummer ---
                    ctx.font = 'bold 0.8Em "Courier New", Courier, monospace';
                    ctx.fillStyle = '#ff9900';
                    let trafficLabel = el.name;
                    if (el.delay !== undefined && el.delay !== null) {
                        if (el.delay > 0) {
                            trafficLabel += ` (+${el.delay})`;
                        } else if (el.delay < 0) {
                            trafficLabel += ` (${el.delay})`; // Negatieve vertraging = voorsprong
                        } else {
                            trafficLabel += ` (+0)`; // Precies op tijd
                        }
                    }
                    
                    ctx.fillText(trafficLabel, trafficTextX, visualCenterY - 15);

                    // --- LAAG 2: Bestemming ---
                    if (el.destination) {
                        ctx.font = '0.7em "Courier New", Courier, monospace';
                        ctx.fillStyle = document.body.getAttribute('data-theme') === 'day' ? '#666666' : '#aaaaaa';
                        ctx.fillText(`→ ${el.destination}`, trafficTextX, visualCenterY - 2);
                    }

                    // --- LAAG 3: Snelheid ---
                    ctx.font = '0.7Em "Courier New", Courier, monospace';
                    ctx.fillStyle = document.body.getAttribute('data-theme') === 'day' ? '#222222' : '#dddddd';
                    ctx.fillText(el.speed + " km/h", trafficTextX, visualCenterY + 11);

                    // --- LAAG 4: Afstand  ---
                    ctx.font = '0.7Em "Courier New", Courier, monospace';
                    ctx.fillStyle = '#888888';
                    ctx.fillText(el.dist + "m", trafficTextX, visualCenterY + 24);  
                }
            });
        }

        // Eigen trein positie indicator
        ctx.fillStyle = '#00aaff';
        ctx.fillRect(centerX - 6, trainY, 12, 20);
    },

    /**
     * ABS-BLOKCASCADE
     * ================
     * Retourneert een Map<element, {mode: 'red'|'yellow'|'flash-green'|'green'}>.
     */
    _computeAbsCascade(mapElements) {
        const overrides = new Map();
        if (!mapElements) return overrides;

        const signals = mapElements.filter(el => el.type === 'signal');
        const trafficDists = mapElements
            .filter(el => el.type === 'traffic')
            .map(el => el.dist);

        if (signals.length === 0) return overrides;

        // Stap 1: is het EIGEN blok van elk sein (tot aan het eerstvolgende sein) bezet?
        const ownBlockOccupied = signals.map((sig, i) => {
            const blockStart = sig.dist;
            const blockEnd = signals[i + 1] ? signals[i + 1].dist : Infinity;
            return trafficDists.some(d => d > blockStart && d <= blockEnd);
        });

        // Stap 2: "rood" per sein - eigen blok bezet, of de backend heeft al
        // een echte (live/onderschepte) snelheid van 0 doorgegeven.
        const isRed = signals.map((sig, i) => ownBlockOccupied[i] || sig.speed === 0);

        // Stap 3: cascade + default-groen + emplacement-uitzondering, enkel
        // voor ABS-seinen die de backend nog niet live heeft kunnen invullen.
        signals.forEach((sig, i) => {
            const isAbsSein = sig.is_abs === true || sig.is_abs === 1 || sig.is_abs === 'yes' || sig.is_abs === 'ja';
            if (!isAbsSein) return;

            const isUnknown = sig.speed === 32767;
            if (!isUnknown) return;

            if (ownBlockOccupied[i]) {
                overrides.set(sig, { mode: 'red' });
                return;
            }
            if (signals[i + 1] && isRed[i + 1]) {
                overrides.set(sig, { mode: 'yellow' });
                return;
            }
            if (signals[i + 2] && isRed[i + 2]) {
                overrides.set(sig, { mode: 'flash-green' });
                return;
            }

            // Laatste ABS-sein vóór een emplacement: flashing groen, want het
            // dispatcher-sein erna valt buiten deze cascade en kan alsnog
            // afwijken - dat zien we pas zodra het ons eigen eerstvolgende sein is.
            const nextIsEmplacementEntry = signals[i + 1] && signals[i + 1].is_emplacement;
            if (nextIsEmplacementEntry) {
                overrides.set(sig, { mode: 'flash-green' });
                return;
            }

            // Anders: geen bezetting in zicht en geen emplacement -> vrije baan.
            overrides.set(sig, { mode: 'green' });
        });

        return overrides;
    },

    _hasFlashingSignal(data) {
        if (!data || !data.map_elements) return false;
        const hasSpeedFlash = data.map_elements.some(el =>
            el.type === 'signal' && (el.speed === 130 || el.speed === 50)
        );
        if (hasSpeedFlash) return true;

        const cascade = this._computeAbsCascade(data.map_elements);
        for (const info of cascade.values()) {
            if (info.mode === 'flash-green') return true;
        }
        return false;
    },

    startFlashLoop() {
        if (this._flashLoopStarted) return; // voorkom dubbele intervals bij herhaald aanroepen
        this._flashLoopStarted = true;
        setInterval(() => {
            if (this._hasFlashingSignal(this._lastMapData)) {
                this.renderMap(this._lastMapData);
            }
        }, 125); // ruim onder de kortste flash-periode (250ms) voor een vloeiende toggle
    },

    /**
     * Renders de uitgebreide 3-rijige timetable met vertragingsprojecties
     */
    renderTimetable(timetableData, gameEndStation, state = {}) {
        const body = document.getElementById('timetable-scroll-area');
        const destVal = document.getElementById('game-end-station-val');
        const scrollArea = document.getElementById('timetable-scroll-area');
        if (!body) return;
        
        if (destVal) {
            destVal.innerText = gameEndStation ? gameEndStation.toUpperCase() : "UNKNOWN";
        }

        if (!timetableData || timetableData.length === 0) {
            body.innerHTML = '<div><div style="text-align:center; padding:20px; color:#666;">No timetable active.</div></div>';
            const oudeHeader = body.parentElement.querySelector('.pinned-final-destination');
            if (oudeHeader) oudeHeader.remove();
            return;
        }

        // 1. Bereken de actuele statussen en vertragingen in de data array (JOUW EIGEN LOGICA)
        let currentDelayMinutes = 0;
        const nextEDRStop = timetableData.find(stop => stop.has_edr && !stop.isConfirmed);
        if (nextEDRStop && nextEDRStop.delay_minutes) {
           currentDelayMinutes = nextEDRStop.delay_minutes;
        }

        let currentHHMM;
        if (state.serverTime) {
            currentHHMM = state.serverTime;
        } else if (state.hours && state.minutes) {
            currentHHMM = `${String(state.hours).padStart(2, '0')}:${String(state.minutes).padStart(2, '0')}`;
        } else {
            const now = new Date();
            currentHHMM = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
        }

        let currentStopFound = false;

        timetableData.forEach(stop => {
            if (stop.has_edr) {
               stop.is_passed = stop.isConfirmed;
            } else {
               stop.is_passed = stop.dep_actual < currentHHMM;
            }
        
            if (!stop.is_passed && !currentStopFound) {
                stop.is_current = true;
               currentStopFound = true;
            } else {
                stop.is_current = false;
            }
        
            if (!stop.is_passed && currentDelayMinutes > 0) {
                stop.arr_actual = this._addMinutesToTime(stop.arr_planned, currentDelayMinutes);
                stop.dep_actual = this._addMinutesToTime(stop.dep_planned, currentDelayMinutes);
            }
        });

        const finalStop = timetableData[timetableData.length - 1];
        const reversedTimetableData = [...timetableData].reverse();
        
        const buildRow3 = (stop) => {
            const buildRow = (items) => {
                return items.filter(item =>
                    item !== null &&
                    item !== undefined &&
                    item !== "null" &&
                    item.toString().trim() !== ""
                ).join(' | ');
            };

            const finalRadio = (stop.radio && stop.radio.trim() !== "")
                ? `<i data-lucide="radio-tower" style="width:12px; height:12px; margin-right:4px; color:#00aaff;"></i> ${stop.radio.trim()}`
                : null;

            let dispatcherHTML = null;

            if (stop.supervisedBy && stop.supervisedBy.trim().length > 0) {
                const dispIcon = stop.dispatcher === 'user' ? 'user-round-check' : 'laptop';
                const dispColor = stop.dispatcher === 'user' ? '#008000' : '#ffaa00';
                const label = stop.dispatcher === 'user' ? 'PLAYER' : 'BOT';

                dispatcherHTML = `<i data-lucide="${dispIcon}" style="width:12px; height:12px; margin-right:4px; color:${dispColor};"></i> ${label}`;
            }

            return buildRow([finalRadio, dispatcherHTML]);
        };

        // 2. CONTROLE & UPDATE VAN DE VASTE HEADER (Buiten de scroll-area)
        let pinnedHeader = body.parentElement.querySelector('.pinned-final-destination');
        
        const generateRowHTML = (stop, isPinnedHeader = false) => {
            // Helper voor opschonen
            const buildRow = (items) => {
                return items.filter(item => 
                    item !== null && 
                    item !== undefined && 
                    item !== "null" && 
                    item.toString().trim() !== ""
                ).join(' | ');
            };

            const delayClass = stop.delayClass || 'delay-default';
            
            // RIJ 2: Line, Track, Platform
            const row2 = buildRow([
                stop.line ? `Line: ${stop.line}` : null,
                stop.track ? `Track: ${stop.track}` : null,
                stop.platform ? `Platform: ${stop.platform}` : null
            ]);
            
            // 1. Radio logica: check of er na trimmen echte tekst overblijft
            const finalRadio = (stop.radio && stop.radio.trim() !== "") ? stop.radio.trim(): null;
            
            // 2. Dispatcher logica:
            let dispatcherHTML = null;

            if (stop.supervisedBy && stop.supervisedBy.trim().length > 0) {
                const dispIcon = stop.dispatcher === 'user' ? 'user-round-check' : 'laptop';
                const dispColor = stop.dispatcher === 'user' ? '#008000' : '#ffaa00';
                const label = stop.dispatcher === 'user' ? 'PLAYER' : 'BOT';
                dispatcherHTML = `<i data-lucide="${dispIcon}" style="width:12px; height:12px; margin-right:4px; color:${dispColor};"></i> ${label}`;
            }

            // 3. Bouw row3 (deze filtert automatisch null-waarden eruit)
            const row3 = buildRow3(stop);

            const stopDurationClean = stop.stop_duration && stop.stop_duration.includes('⁵')
            ? `${stop.stop_duration.replace('⁵','')}<sup style="font-size:0.7em;">5</sup>`
            : (stop.stop_duration || '');

            // 3. Return de template
            return `
                <div class="item-row item-row-main">
                    <div class="tt-col-point stop-name">${stop.name} ${isPinnedHeader ? ' <span style="color:#ffaa00; font-size:0.8em;">[DEST]</span>' : ''}</div>
                    <div class="tt-col-arrival time-planned">${stop.arr_planned}</div>
                    <div class="tt-col-depart time-planned">${stop.dep_planned}</div>
                    <div class="tt-col-type stop-type">${stop.stop_type}</div>
                    <div class="tt-col-duration time-planned">${stopDurationClean}</div>
                </div>

                <div class="item-row item-row-sub text-small">
                    <div class="tt-col-point track-info">${row2}</div>
                    <div class="tt-col-arrival time-actual ${delayClass}"><span class="arr-actual-val">${stop.arr_actual || ''}</span></div>
                    <div class="tt-col-depart time-actual ${delayClass}"><span class="dep-actual-val">${stop.dep_actual || ''}</span></div>
                    <div class="tt-col-type"></div>
                    <div class="tt-col-duration"></div>
                </div>

                <div class="item-row item-row-meta text-small" style="padding-bottom: 2px;">
                    <div class="tt-col-point track-info dispatcher-container">${row3}</div>
                </div>
            `;
        };

        if (!pinnedHeader && finalStop) {
            pinnedHeader = document.createElement('div');
            pinnedHeader.className = 'timetable-item pinned-final-destination';
            body.parentElement.insertBefore(pinnedHeader, body);
            pinnedHeader.innerHTML = generateRowHTML(finalStop, true);
        } else if (pinnedHeader && finalStop) {
            const arrEl = pinnedHeader.querySelector('.arr-actual-val');
            const depEl = pinnedHeader.querySelector('.dep-actual-val');
            if (arrEl) arrEl.innerText = finalStop.arr_actual || '';
            if (depEl) depEl.innerText = finalStop.dep_actual || '';

            // Update kleur van de pinned header elementen
            const arrContainer = pinnedHeader.querySelector('.tt-col-arrival.time-actual');
            const depContainer = pinnedHeader.querySelector('.tt-col-depart.time-actual');
            
            [arrContainer, depContainer].forEach(el => {
                if (el) {
                    el.classList.remove(
                        'delay-passed',
                        'delay-ontime',
                        'delay-warning',
                        'delay-severe'
                    );

                    el.classList.add(finalStop.delayClass || 'delay-ontime');
                }
            });
        }

        // 3. CHECK: Staat de scrollbare rittenlijst er al?
        const bestaandeScrollItems = body.querySelectorAll('.timetable-item');
        
        if (bestaandeScrollItems.length === reversedTimetableData.length) {
            // ==========================================================================
            // OPTIE A: DE SLIMME UPDATE
            // ==========================================================================
            bestaandeScrollItems.forEach((item, index) => {
                const stop = reversedTimetableData[index];
                if (!stop) return;

                // ================================
                // 1. UPDATE ACTUELE TIJDEN
                // ================================
                const arrEl = item.querySelector('.arr-actual-val');
                const depEl = item.querySelector('.dep-actual-val');

                if (arrEl) arrEl.innerText = stop.arr_actual || '';
                if (depEl) depEl.innerText = stop.dep_actual || '';

                // ================================
                // 2. UPDATE DELAY KLEUREN
                // ================================
                const arrContainer = item.querySelector('.tt-col-arrival.time-actual');
                const depContainer = item.querySelector('.tt-col-depart.time-actual');

                [arrContainer, depContainer].forEach(el => {
                    if (el) {
                        el.classList.remove(
                            'delay-passed',
                            'delay-ontime',
                            'delay-warning',
                            'delay-severe'
                        );

                        el.classList.add(stop.delayClass || 'delay-ontime');
                    }
                });

                // ================================
                // 3. UPDATE ROW 3 (RADIO + DISPATCHER)
                // ================================
                const metaRow = item.querySelector('.dispatcher-container');

                if (metaRow) {
                    metaRow.innerHTML = buildRow3(stop);
                }

                // ================================
                // 4. UPDATE CURRENT / PASSED STATES
                // ================================
                item.classList.remove('current-stop', 'past-stop');
                item.removeAttribute('id');

                if (stop.is_current) {
                    item.classList.add('current-stop');
                    item.id = "active-current-row";
                } else if (stop.is_passed) {
                    item.classList.add('past-stop');
                }
            });

            // ================================
            // 5. HERINITIALISEER LUCIDE ICONS
            // ================================
            if (typeof lucide !== 'undefined') {
                lucide.createIcons();
            }

            this._handleScrollBehavior(scrollArea, state);
            return;
        }

        // ==========================================================================
        // OPTIE B: VOLLEDIGE OPBOUW (Bij de allereerste render)
        // ==========================================================================
        body.innerHTML = '';

        reversedTimetableData.forEach(stop => {
            const timetableItem = document.createElement('div');
    
            let rowStatusClass = "";
            if (stop.is_current) rowStatusClass = "current-stop";
            else if (stop.is_passed) rowStatusClass = "past-stop";

            timetableItem.className = `timetable-item ${rowStatusClass}`;
            if (stop.is_current) timetableItem.id = "active-current-row";

            timetableItem.innerHTML = generateRowHTML(stop, false);
            body.appendChild(timetableItem);
        });

        if (typeof lucide !== 'undefined') lucide.createIcons();
        this._handleScrollBehavior(scrollArea, state);
    },

    // Helper functie voor de scroll-afhandeling
    _handleScrollBehavior(scrollArea, state) {
        if (!scrollArea) return;
        const isLocked = this._autoScrollLocked ?? state.timetableAutoScroll ?? true;

        if (isLocked) {
            const currentRow = document.getElementById('active-current-row');
            if (currentRow) {
                // Maakt gebruik van de ingebouwde browsercentrering
                currentRow.scrollIntoView({
                    behavior: 'smooth',
                    block: 'center' // Centreert de huidige halte netjes in het verticale midden
                });
            }
        }
    },
    
    // Helper functie om minuten op te tellen bij HH:MM string
    _addMinutesToTime(timeStr, minsToAdd) {
        if (!timeStr || timeStr === '--:--') return timeStr;
        const [hrs, mins] = timeStr.split(':').map(Number);
        const date = new Date();
        date.setHours(hrs, mins + minsToAdd, 0, 0);
        return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    },

    _autoScrollLocked: true,

    /**
    * Centraal schakelstation om de timetable lock aan of uit te zetten
    */
    setTimetableLock(shouldLock) {
        // Sla het intern op in de UI laag zelf
        this._autoScrollLocked = shouldLock;

        // Update eventueel ook de globale state als die bestaat
        if (typeof state !== 'undefined') {
            state.timetableAutoScroll = shouldLock;
        }

        const centerBtn = document.getElementById('btn-tt-center');
        if (!centerBtn) return;

        if (shouldLock) {
            centerBtn.classList.add('active-lock');
            const currentRow = document.getElementById('active-current-row');
            if (currentRow) {
                currentRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        } else {
            centerBtn.classList.remove('active-lock');
        }
    },

    updateInfoView(data) {
        const infoContainer = document.getElementById('info-scroll-area');
        if (!infoContainer) return;

        const info = data.train_info || {};
        const summary = info.summary || {};
        const currentTrainNumber = summary.train_number;
        if (currentTrainNumber && lastLoadedTrainNumber === currentTrainNumber) {
            return; 
        }

        lastLoadedTrainNumber = currentTrainNumber;
        const technical = info.technical || {};
        const consistInfo = info.consist || {};
        const consist = consistInfo.vehicles || [];

        let html = '';

        // =====================================================
        // SUMMARY
        // =====================================================
        html += `
        <div class="info-section">
            <div class="info-section-title">SUMMARY</div>
            <div class="info-grid">
                <div class="info-row">
                    <div class="info-label">Train Number</div>
                    <div class="info-value">${summary.train_number || '-'}</div>
                </div>
                <div class="info-row">
                    <div class="info-label">Service Name</div>
                    <div class="info-value">${summary.service_name || '-'}</div>
                </div>
                <div class="info-row">
                    <div class="info-label">Route</div>
                    <div class="info-value">${summary.route || '-'}</div>
                </div>
            </div>
        </div>
        `;

        // =====================================================
        // TECHNICAL
        // =====================================================
        html += `
        <div class="info-section">
            <div class="info-section-title">TECHNICAL</div>

            <div class="info-grid">

                <div class="info-row">
                    <div class="info-label">Locomotives</div>
                    <div class="info-value">${technical.locomotives || '-'}</div>
                </div>

                <div class="info-row">
                    <div class="info-label">Total Power</div>
                    <div class="info-value">${technical.power && technical.power !== '-' ? technical.power + ' kW' : '-'}</div>
                </div>

                <div class="info-row">
                    <div class="info-label">Total Mass</div>
                    <div class="info-value">${technical.mass || '-'} t</div>
                </div>

                <div class="info-row">
                    <div class="info-label">Total Length</div>
                    <div class="info-value">${technical.length || '-'} m</div>
                </div>

                <div class="info-row">
                    <div class="info-label">Vmax</div>
                    <div class="info-value">${technical.vmax || '-'} km/h</div>
                </div>

                <div class="info-row">
                    <div class="info-label">Brake Regime</div>
                    <div class="info-value">${technical.brake_regime || '-'}</div>
                </div>

            </div>
        </div>
        `;

        // =====================================================
        // CONSIST
        // =====================================================
        html += `
        <div class="info-section consist-section">
        
            <div class="consist-header-wrapper">
                <div class="consist-row consist-header">
                    <div style="grid-column: span 3; text-align: left;">CONSIST (${consist.length})</div>
                    <div class="col-status">STATUS</div>
                    <div class="col-cargo">CARGO</div>
                    <div class="col-weight">WEIGHT</div>
                    <div class="col-vmax">VMAX</div>
                </div>
            </div>

            <div id="info-consist-container" class="consist-container">
        `;

        consist.forEach((vehicle, index) => {
            const type = vehicle.base_type || '-';
            const name = vehicle.wiki_name || '-';
            const cargo = vehicle.cargo || '-';
            const status = vehicle.status === "Geladen" ? "Loaded" : "Empty";
            const weight = vehicle.weight && vehicle.weight !== '-' ? `${vehicle.weight} t` : '-';
            const vmax = vehicle.vmax ? `${vehicle.vmax} km/h` : '-';

            html += `
            <div class="consist-row">
                <div class="col-idx">${String(index + 1).padStart(3, '0')}</div>
                <div class="col-type" title="${vehicle.variant}">${type}</div>
                <div class="col-name">${name}</div>
                <div class="col-status">${status}</div>
                <div class="col-cargo">${cargo}</div>
                <div class="col-weight">${weight}</div>
                <div class="col-vmax">${vmax}</div>
            </div>
            `;
        });

        html += `
            </div>
        </div>
        `;
        
        // --- SCROLL POSITION PRESERVATION (GECORRIGEERD) ---
        // Pak de huidige container op VÓÓR we de innerHTML overschrijven
        const oldConsistScroll = document.getElementById('info-consist-container');
        const savedScrollTop = oldConsistScroll ? oldConsistScroll.scrollTop : 0;

        // Schrijf de nieuwe HTML nu exact één keer weg
        infoContainer.innerHTML = html;

        // Herstel de stand direct op de zojuist nieuw aangemaakte container
        const newConsistScroll = document.getElementById('info-consist-container');
        if (newConsistScroll) {
            newConsistScroll.scrollTop = savedScrollTop;
        }
    }  
     
};

const btnInfo = document.getElementById('btn-tt-info');
const btnView = document.getElementById('btn-tt-view');
const viewTimetable = document.getElementById('view-timetable');
const viewInfo = document.getElementById('view-info');
const navButtons = document.querySelectorAll('#btn-tt-up, #btn-tt-down');
const btnCenter = document.getElementById('btn-tt-center');

let activeRightPanel = 'timetable';

function switchView(target) {
    if (target === 'info') {
        activeRightPanel = 'info';
        viewTimetable.style.display = 'none';
        viewInfo.style.display = 'flex';
        btnInfo.style.opacity = '0.3';
        btnInfo.style.pointerEvents = 'none';
        btnView.style.opacity = '1';
        btnView.style.pointerEvents = 'auto';
        btnCenter.style.opacity = '0.3';
        btnCenter.classList.remove('active-lock')
        btnCenter.style.pointerEvents = 'none';
        btnView.classList.remove('active');
    } else {
        activeRightPanel = 'timetable';
        viewTimetable.style.display = 'flex';
        viewInfo.style.display = 'none';
        btnInfo.style.opacity = '1';
        btnInfo.style.pointerEvents = 'auto';
        btnView.style.opacity = '0.3';
        btnView.style.pointerEvents = 'none';
        btnCenter.style.opacity = '1';
        btnCenter.style.pointerEvents = 'auto';
        btnInfo.classList.remove('active');
        ;
    }
}

btnInfo.addEventListener('click', () => switchView('info'));
btnView.addEventListener('click', () => switchView('timetable'));

// Zorg dat de listeners pas geladen worden als de DOM klaar is
document.addEventListener('DOMContentLoaded', () => {
    // Start de losse knipper-loop voor 130/50-seinen op de Look Ahead kaart.
    SimRailUI.startFlashLoop();

    const scrollArea = document.getElementById('timetable-scroll-area');
    const btnUp = document.getElementById('btn-tt-up');
    const btnDown = document.getElementById('btn-tt-down');
    const btnCenter = document.getElementById('btn-tt-center');

    // Functie die de exacte hoogte van één rij meet
    const getRowHeight = () => {
        // We zoeken de eerste rij binnen de scroll-area
        const firstRow = scrollArea ? scrollArea.querySelector('.timetable-item') : null;
        // offsetHeight geeft de volledige hoogte inclusief padding en borders
        return firstRow ? firstRow.offsetHeight : 65; 
    };

    function getConsistRowHeight() {
        const row = document.querySelector('.consist-row');
        return row ? row.offsetHeight : 35; // Pak de echte hoogte, anders fallback op 35px
    }

    if (scrollArea) {
        scrollArea.addEventListener('wheel', () => SimRailUI.setTimetableLock(false), { passive: true });
        scrollArea.addEventListener('touchmove', () => SimRailUI.setTimetableLock(false), { passive: true });
    }

    if (btnUp) {
        btnUp.addEventListener('click', () => {
            if (activeRightPanel === 'timetable') {
                SimRailUI.setTimetableLock(false);
                scrollArea.scrollBy({
                    top: -getRowHeight(),
                    behavior: 'smooth'
                });
            } else {
                const infoScroll = document.getElementById('info-consist-container');
                if (infoScroll) {
                    infoScroll.scrollBy({
                        top: -getConsistRowHeight(),
                        behavior: 'smooth'
                    });
                }
            }
        });
    }

    if (btnDown) {
        btnDown.addEventListener('click', () => {
            if (activeRightPanel === 'timetable') {
                SimRailUI.setTimetableLock(false);
                scrollArea.scrollBy({
                    top: getRowHeight(),
                    behavior: 'smooth'
                });
            } else {
                const infoScroll = document.getElementById('info-consist-container');
                if (infoScroll) {
                    infoScroll.scrollBy({
                        top: getConsistRowHeight(),
                        behavior: 'smooth'
                    });
                }
            }
        });
    }

    if (btnCenter) {
        btnCenter.addEventListener('click', () => {
            SimRailUI.setTimetableLock(true);
        });
    }

    const btnZoomIn = document.getElementById('btn-map-zoom-in');
    const btnZoomOut = document.getElementById('btn-map-zoom-out');
    const btnZoomReset = document.getElementById('btn-map-zoom-reset');

    if (btnZoomIn) {
        btnZoomIn.addEventListener('click', () => SimRailUI.setMapZoom('in'));
    }

    if (btnZoomOut) {
        btnZoomOut.addEventListener('click', () => SimRailUI.setMapZoom('out'));
    }

    if (btnZoomReset) {
        btnZoomReset.addEventListener('click', () => SimRailUI.setMapZoom('reset'));
    }
});