"""
Logging applicatif.

En conteneur, la bonne pratique est de logger sur stdout : c'est ce que `docker logs`
capture nativement, sans configuration supplémentaire. Le fichier local est optionnel
(désactivé par défaut en prod) car le système de fichiers d'un conteneur est éphémère —
un fichier de log y disparaît au moindre redémarrage sauf volume monté explicitement.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "false").lower() == "true"
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "logs/inference.log")


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("detection_api")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.handlers.clear()  # évite les handlers en double si appelé plusieurs fois

    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if LOG_TO_FILE:
        try:
            os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
            file_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=10 * 1024 * 1024, backupCount=5)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as e:
            # Ne jamais faire planter l'appli pour un problème de log fichier :
            # on log l'échec sur stdout (déjà configuré juste au-dessus) et on continue.
            logger.warning(f"Impossible d'écrire les logs dans {LOG_FILE_PATH} : {e}")

    return logger


logger = setup_logging()