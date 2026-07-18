import threading
import logging
import os
import sys

from waitress import create_server

from config import load_config, save_config
from log_setup import setup_logging, LOG_PATH
from version import __version__

# ============================================================================
# INTEGRATIEPUNT MET app.py
# ============================================================================
from app import app as flask_app, load_train_data, load_vehicles_db, start_background_loops

logger = logging.getLogger("launcher")


def clear_previous_log():
    """
    Wist het logbestand van de vorige sessie, inclusief eventuele
    rotatie-backups (simtim.log.1, .2, .3), zodat elke start van de launcher
    met een schone lei begint en alleen deze sessie vastlegt.

    Wordt bewust vóór setup_logging() aangeroepen: de RotatingFileHandler
    opent het bestand in append-modus, dus verwijderen moet daarvoor gebeuren.
    """
    candidates = [LOG_PATH] + [f"{LOG_PATH}.{i}" for i in range(1, 4)]
    for path in candidates:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            # Niet fataal (bv. bestand tijdelijk vergrendeld) - er is nog geen
            # logger beschikbaar op dit punt, dus dit gaat naar stderr.
            print(f"Could not clear old log file {path}: {e}", file=sys.stderr)


class ServerManager:
    """Beheert het starten/stoppen/herstarten van de waitress-server."""

    def __init__(self, flask_app, host, port):
        self.flask_app = flask_app
        self.host = host
        self.port = port
        self._server = None
        self._thread = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._server = create_server(self.flask_app, host=self.host, port=self.port)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        self._running = True
        logger.info(f"Server started on {self.host}:{self.port}")

    def stop(self):
        if not self._running or self._server is None:
            return
        self._server.close()
        self._running = False
        logger.info("Server stopped")

    def restart(self, new_port=None):
        self.stop()
        if new_port is not None:
            self.port = new_port
        self.start()

    def is_running(self):
        return self._running


def main():
    clear_previous_log()
    setup_logging()
    logger.info(f"SimTim Terminal v{__version__} starting up")

    cfg = load_config()

    load_train_data()
    load_vehicles_db()
    start_background_loops()

    manager = ServerManager(flask_app, cfg["host"], cfg["port"])
    manager.start()

    # GUI pas hier importeren, zodat de server al draait vóórdat het venster opent
    from gui import ControlPanel

    def handle_port_change(new_port):
        try:
            manager.restart(new_port=new_port)
            cfg["port"] = new_port
            save_config(cfg)
            return True, None
        except OSError as e:
            return False, str(e)

    def handle_start():
        try:
            manager.start()
            return True, None
        except OSError as e:
            return False, str(e)

    def handle_stop():
        manager.stop()
        return True, None

    panel = ControlPanel(
        initial_host=cfg["host"],
        initial_port=cfg["port"],
        on_port_change=handle_port_change,
        on_start=handle_start,
        on_stop=handle_stop,
        get_status=manager.is_running,
        log_path=LOG_PATH,
    )
    panel.mainloop()

    manager.stop()


if __name__ == "__main__":
    main()