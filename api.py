"""
API FastAPI exposant le service d'inférence.
Le modèle est chargé une seule fois au démarrage (pas à chaque requête).
"""
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from database import init_db, get_db_session, wait_for_db, engine
from db_models import Detection
from inference_service import load_model_from_db, inference, PredictionResponse
from logging_config import logger

_model = None
_model_record = None

MAX_STARTUP_ATTEMPTS = int(os.getenv("MAX_STARTUP_ATTEMPTS", "10"))
STARTUP_RETRY_DELAY = float(os.getenv("STARTUP_RETRY_DELAY_SECONDS", "3"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    global _model, _model_record

    wait_for_db(max_attempts=MAX_STARTUP_ATTEMPTS, delay_seconds=STARTUP_RETRY_DELAY)
    init_db()

    # Le modèle actif peut être enregistré juste après le démarrage de la DB/MinIO :
    # on retente plutôt que de planter au premier échec.
    last_error = None
    for attempt in range(1, MAX_STARTUP_ATTEMPTS + 1):
        try:
            _model, _model_record = load_model_from_db()
            logger.info("API prête, modèle chargé.")
            last_error = None
            break
        except Exception as e:
            last_error = e
            logger.warning(f"Chargement du modèle échoué (tentative {attempt}/{MAX_STARTUP_ATTEMPTS}) : {e}")
            if attempt < MAX_STARTUP_ATTEMPTS:
                time.sleep(STARTUP_RETRY_DELAY)

    if last_error is not None:
        logger.error(f"Démarrage impossible après {MAX_STARTUP_ATTEMPTS} tentatives : {last_error}")
        raise last_error

    yield
    # --- shutdown ---
    logger.info("Arrêt de l'API.")


app = FastAPI(title="Détection d'objets militaires - API", lifespan=lifespan)

# En prod, restreindre via la variable d'environnement ALLOWED_ORIGINS
# (liste séparée par des virgules), jamais laisser "*" tel quel.
_origins = os.getenv("ALLOWED_ORIGINS", "*")
allow_origins = ["*"] if _origins == "*" else [o.strip() for o in _origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Journalise chaque requête avec sa durée : base pour diagnostiquer un souci en prod."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.error(f"{request.method} {request.url.path} | ÉCHEC après {duration_ms:.1f} ms", exc_info=True)
        raise
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(f"{request.method} {request.url.path} | {response.status_code} | {duration_ms:.1f} ms")
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Filet de sécurité : une exception inattendue ne doit jamais renvoyer une stack trace au client."""
    logger.error(f"Exception non gérée sur {request.url.path} : {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Erreur interne du serveur."})


@app.get("/health")
def health():
    """Vérifie l'état réel du service : modèle chargé ET base de données joignable."""
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        db_ok = False
        logger.warning(f"/health : base de données injoignable : {e}")

    healthy = (_model is not None) and db_ok
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "model_loaded": _model is not None,
            "database_reachable": db_ok,
            "model_name": _model_record.name if _model_record else None,
            "model_version": _model_record.version if _model_record else None,
        },
    )


@app.post("/predict", response_model=PredictionResponse)
async def predict(file: UploadFile = File(...)):
    if _model is None:
        raise HTTPException(status_code=503, detail="Modèle non chargé")

    # Garde-fou simple : évite qu'un upload énorme ne sature la mémoire du conteneur.
    max_size_mb = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20"))
    image_bytes = await file.read()
    if len(image_bytes) > max_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {max_size_mb} Mo)")

    try:
        return inference(
            image_bytes=image_bytes,
            model=_model,
            model_record=_model_record,
            source_filename=file.filename,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur d'inférence : {e}")


@app.get("/detections")
def list_detections(limit: int = 20):
    """Historique des dernières images traitées, avec leurs détections, pour le dashboard."""
    limit = min(limit, 100)  # évite qu'un appel mal formé ne charge toute la table
    with get_db_session() as session:
        records = (
            session.query(Detection)
            .order_by(Detection.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": d.id,
                "source_filename": d.source_filename,
                "num_detections": d.num_detections,
                "inference_time_ms": d.inference_time_ms,
                "created_at": d.created_at.isoformat(),
                "boxes": [
                    {"class_name": b.class_name, "confidence": b.confidence}
                    for b in d.boxes
                ],
            }
            for d in records
        ]