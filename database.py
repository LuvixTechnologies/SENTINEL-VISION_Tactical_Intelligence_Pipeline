"""
Configuration de la base de données.
Fonctionne avec PostgreSQL en prod et SQLite en local pour développer sans dépendance externe.
"""
import os
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./detection.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# Paramètres de pool ajustables par variable d'environnement : les valeurs par défaut
# conviennent à une petite instance, mais une prod avec plus de trafic voudra les augmenter.
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,                                   # évite de servir une connexion morte
    pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")),  # recycle avant coupure réseau/firewall
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db() -> None:
    """Crée les tables si elles n'existent pas. À appeler une fois au démarrage de l'API."""
    from db_models import MLModel, Detection, DetectionBox, InferenceLog  # noqa: F401
    Base.metadata.create_all(bind=engine)


def wait_for_db(max_attempts: int = 10, delay_seconds: float = 2.0) -> None:
    """
    Attend que la base soit joignable avant de continuer.
    Utile au démarrage du conteneur : Postgres peut mettre quelques secondes à accepter
    des connexions même une fois son propre processus lancé (healthcheck docker-compose
    couvre déjà ce cas, mais cette vérification applicative est une seconde ligne de défense).
    """
    from logging_config import logger

    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Connexion à la base de données établie.")
            return
        except OperationalError as e:
            logger.warning(f"Base de données injoignable (tentative {attempt}/{max_attempts}) : {e}")
            if attempt == max_attempts:
                raise
            time.sleep(delay_seconds)


@contextmanager
def get_db_session():
    """Context manager : commit automatique si tout se passe bien, rollback sinon."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()