import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "simtim.log")


# ------------------- SERVER-CONTEXT VOOR LOGREGELS -------------------
# app.py roept set_log_context() aan zodra de actieve server of de laatst
# bekende in-game servertijd wijzigt. Elke logregel toont die context dan
# automatisch in een eigen kolom, zonder dat elke individuele log-aanroep
# dit handmatig in de berichttekst hoeft op te nemen.
_log_context = {"server_id": "-", "server_time": "--:--"}


def set_log_context(server_id=None, server_time=None):
    """Werkt de server/servertijd-context bij die in elke logregel verschijnt."""
    if server_id is not None:
        _log_context["server_id"] = server_id
    if server_time is not None:
        _log_context["server_time"] = server_time


class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.server_id = _log_context.get("server_id", "-")
        record.server_time = _log_context.get("server_time", "--:--")
        return True


class _SimTimFormatter(logging.Formatter):
    """
    Standaard logging.Formatter toont milliseconden (3 cijfers) na de komma.
    Voor de leesbaarheid tonen we hier honderdsten (2 cijfers) i.p.v.
    duizendsten, en houden we vaste kolombreedtes aan voor niveau en
    loggernaam, zodat het logbestand netjes in kolommen blijft staan.
    """
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        s = time.strftime("%Y-%m-%d %H:%M:%S", ct)
        centis = int(record.msecs / 10)
        return f"{s}.{centis:02d}"


def setup_logging():
    """
    Configureert logging naar een roterend logbestand (max 2MB per bestand,
    3 backups). Wordt één keer aangeroepen bij het opstarten van launcher.py.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return LOG_PATH

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    formatter = _SimTimFormatter(
        "%(asctime)s [%(levelname)-7s] [%(server_id)s - %(server_time)s] %(name)-12s: %(message)s"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_ContextFilter())
    logger.addHandler(file_handler)

    return LOG_PATH


def set_debug_logging(enabled: bool):
    """
    Zet het root-logniveau live tussen INFO en DEBUG, zonder herstart.
    Wordt aangeroepen vanuit het debug-vinkje in het controlepaneel.
    """
    logging.getLogger().setLevel(logging.DEBUG if enabled else logging.INFO)
    logging.getLogger("launcher").info(
        f"Debug logging {'ingeschakeld' if enabled else 'uitgeschakeld'} via control panel."
    )