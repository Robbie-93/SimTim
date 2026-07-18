/* ==========================================================================
   SIMTIM API LAYER (Netwerk & Data-fetching)
   ========================================================================== */

const SimRailAPI = {
    /* Active Servers */
    async getServers() {
        const res = await fetch("/api/servers");
        return await res.json();
    },

     /* Selected Train Details */
    async getTrainData(server, trainNumber) {
        // Blokkeer de fetch als er (nog) geen treinnummer is ingevoerd
        if (!trainNumber) return { error: "Geen treinnummer opgegeven" };
        
        // Toegevoegd: { cache: 'no-store' } om het pendelen/cachen direct te stoppen
        const res = await fetch(`/api/my_train_data?server=${server}&number=${trainNumber}`, { cache: 'no-store' });
        return await res.json();
    },

    /* Health Check */
    async getSystemHealth(server, trainNumber) {
        if (!trainNumber) return { error: "Geen actieve trein voor health check" };

        const serverId = server || 'int1';
        const res = await fetch(`/api/health?server_id=${serverId}&train_id=${trainNumber}`, { cache: 'no-store' });
        return await res.json();
    },

    /*  ServerTime */
    async getServerTime(server) {
        const res = await fetch(`/api/server_time?server=${server}`);
        return await res.json();
    }
};