/* ==========================================================================
   SIMTIM API LAYER (Netwerk & Data-fetching)
   ========================================================================== */

const SimRailAPI = {
    async _fetchWithTimeout(url, options = {}, timeoutMs = 6000) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
            return await fetch(url, { ...options, signal: controller.signal });
        } finally {
            clearTimeout(timer);
        }
    },

    /* Active Servers */
    async getServers() {
        const res = await this._fetchWithTimeout("/api/servers");
        return await res.json();
    },

     /* Selected Train Details */
    async getTrainData(server, trainNumber) {
        if (!trainNumber) return { error: "Geen treinnummer opgegeven" };
        const res = await this._fetchWithTimeout(
            `/api/my_train_data?server=${server}&number=${trainNumber}`,
            { cache: 'no-store' }
        );
        return await res.json();
    },

    /* Health Check */
    async getSystemHealth(server, trainNumber) {
        if (!trainNumber) return { error: "Geen actieve trein voor health check" };
        const serverId = server || 'int1';
        const res = await this._fetchWithTimeout(
            `/api/health?server_id=${serverId}&train_id=${trainNumber}`,
            { cache: 'no-store' }
        );
        return await res.json();
    },

    /*  ServerTime */
    async getServerTime(server) {
        const res = await this._fetchWithTimeout(`/api/server_time?server=${server}`);
        return await res.json();
    }
};