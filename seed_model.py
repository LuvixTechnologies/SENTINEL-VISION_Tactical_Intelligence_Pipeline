"""
Enregistre le modèle actif en base de données.

Idempotent : si le modèle (même nom + version + backend) est déjà actif en base,
le script ne fait rien. Ça permet de le relancer à chaque démarrage de la stack
(via docker-compose) sans re-uploader le fichier ni créer de doublons.

Deux modes selon STORAGE_BACKEND (variable d'environnement, "local" par défaut) :
- "local" : le fichier de poids doit déjà être présent sur le disque (usage dev).
- "s3" : ce script UPLOAD le fichier local vers MinIO/S3 puis enregistre le
            chemin distant en base (usage cluster/prod).

Usage : python seed_model.py
"""
import os
from pathlib import Path

from database import init_db, get_db_session
from db_models import MLModel
from logging_config import logger

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")   # "local" | "s3", ici pas besoin de modifier, les variables d'environnements de configmap font l'affaire
LOCAL_WEIGHTS_PATH = os.getenv("LOCAL_WEIGHTS_PATH", "weights/best.pt")
S3_BUCKET = os.getenv("S3_MODEL_BUCKET", "ml-models")
MODEL_NAME = os.getenv("MODEL_NAME", "yolo-military-detection")
MODEL_VERSION = os.getenv("MODEL_VERSION", "v1.0.0")

init_db()

# --- Idempotence : ce modèle exact est-il déjà actif en base ? ---
with get_db_session() as session:
    already_active = (
        session.query(MLModel)
        .filter(
            MLModel.name == MODEL_NAME,
            MLModel.version == MODEL_VERSION,
            MLModel.storage_backend == STORAGE_BACKEND,
            MLModel.is_active.is_(True),
        )
        .first()
    )

if already_active is not None:
    logger.info(
        f"Modèle '{MODEL_NAME}' v{MODEL_VERSION} déjà actif en base "
        f"(backend={STORAGE_BACKEND}) — rien à faire."
    )
    raise SystemExit(0)

# --- Résolution du chemin de stockage (upload vers MinIO si besoin) ---
if STORAGE_BACKEND == "s3":
    from inference_service import _ensure_bucket
    import boto3

    if not Path(LOCAL_WEIGHTS_PATH).exists():
        raise FileNotFoundError(
            f"Fichier de poids introuvable : {LOCAL_WEIGHTS_PATH}. "
            "Il doit être présent localement pour être uploadé vers MinIO."
        )

    s3 = boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
    )
    _ensure_bucket(S3_BUCKET, s3_client=s3)

    filename = Path(LOCAL_WEIGHTS_PATH).name
    s3_key = f"{MODEL_VERSION}/{filename}"
    logger.info(f"Upload de {LOCAL_WEIGHTS_PATH} vers s3://{S3_BUCKET}/{s3_key}")
    s3.upload_file(LOCAL_WEIGHTS_PATH, S3_BUCKET, s3_key)

    storage_path = f"{S3_BUCKET}/{s3_key}"
else:
    storage_path = LOCAL_WEIGHTS_PATH
    if not Path(storage_path).exists():
        raise FileNotFoundError(f"Fichier de poids introuvable : {storage_path}")

# --- Enregistrement en base ---
with get_db_session() as session:
    session.query(MLModel).update({"is_active": False})

    model = MLModel(
        name=MODEL_NAME,
        version=MODEL_VERSION,
        storage_path=storage_path,
        storage_backend=STORAGE_BACKEND,
        conf_threshold=0.25,
        iou_threshold=0.45,
        is_active=True,
    )
    session.add(model)

logger.info(f"Modèle '{MODEL_NAME}' v{MODEL_VERSION} enregistré et activé (backend={STORAGE_BACKEND}).")