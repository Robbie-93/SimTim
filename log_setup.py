import logging
from logging.handlers import RotatingFileHandler
import os
import sys

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "simtim.log")


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
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return LOG_PATH