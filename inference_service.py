"""
Service d'inférence YOLO.

- Le modèle actif est défini en base de données (table ml_models), pas en dur dans le code :
  changer de version = un UPDATE en base, pas un redéploiement.
- Chaque inférence est journalisée (logs applicatifs) ET persistée en base
  (image traitée + boites détectées + temps de réponse).
- Toute erreur est capturée, loggée, et tracée dans la table inference_logs.
"""
import io
import time
import base64
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel
from ultralytics import YOLO
import torch

from database import get_db_session
from db_models import MLModel, Detection, DetectionBox, InferenceLog, LogLevel
from logging_config import logger


# ============================================================
# Export / diagnostics (repris de ton code d'origine)
# ============================================================
def export_to_onnx(model_path: str, export_dir: str = "exports", imgsz: int = 640) -> bool:
    Path(export_dir).mkdir(parents=True, exist_ok=True)
    logger.info(f"Chargement du modèle : {model_path}")
    model = YOLO(model_path)

    logger.info("Export ONNX en cours...")
    model.export(
        format="onnx",
        imgsz=imgsz,
        dynamic=True,   # accepte n'importe quelle taille d'image
        simplify=True,
        opset=12,
    )
    logger.info(f"Export terminé. Fichiers disponibles dans {export_dir}/ ou à côté de {model_path}")
    return True


def check_pytorch() -> bool:
    logger.info(f"PyTorch version {torch.__version__}")
    cuda_available = torch.cuda.is_available()
    hip_version = getattr(torch.version, "hip", None)

    backend = f"ROCm (HIP {hip_version})" if hip_version else ("CUDA" if cuda_available else "CPU uniquement")
    logger.info(f"Backend : {backend} | GPU détecté : {cuda_available}")

    if not cuda_available:
        logger.warning("Aucun GPU détecté, l'inférence tournera en CPU.")
        return False

    props = torch.cuda.get_device_properties(0)
    logger.info(f"GPU : {torch.cuda.get_device_name(0)} | VRAM : {props.total_memory / 1024 ** 3:.1f} Go")

    try:
        x, y = torch.rand(2000, 2000, device="cuda"), torch.rand(2000, 2000, device="cuda")
        _ = x @ y
        torch.cuda.synchronize()
        logger.info("Test de calcul GPU OK")
    except Exception as e:
        logger.error(f"Test de calcul GPU échoué : {e}")
        return False
    return True


# ============================================================
# Schémas de réponse (API)
# ============================================================
class BoundingBox(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class DetectionOut(BaseModel):
    class_id: int
    class_name: str
    confidence: float
    bbox: BoundingBox


class PredictionResponse(BaseModel):
    success: bool
    detection_id: Optional[str] = None
    inference_time_ms: float
    image_width: int
    image_height: int
    num_detections: int
    detections: List[DetectionOut]
    annotated_image_base64: Optional[str] = None
    model_info: dict


# ============================================================
# Utilitaires image
# ============================================================
def load_image_from_bytes(image_bytes: bytes) -> np.ndarray:
    """Charge une image de n'importe quelle taille et la convertit en BGR (OpenCV)."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def encode_image_to_base64(image_bgr: np.ndarray) -> str:
    """Encode une image OpenCV en base64 JPEG."""
    _, buffer = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buffer).decode("utf-8")


def _ensure_bucket(bucket: str, s3_client=None) -> None:
    """Crée le bucket MinIO/S3 s'il n'existe pas encore. Idempotent, sans risque à rappeler."""
    import os
    from botocore.exceptions import ClientError
    import boto3

    s3 = s3_client or boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
    )
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        logger.info(f"Bucket '{bucket}' introuvable, création...")
        s3.create_bucket(Bucket=bucket)


def _download_from_s3(storage_path: str, dest_dir: str = "models_cache") -> str:
    """Télécharge le fichier de poids depuis MinIO/S3 si le registre pointe vers un bucket.
    storage_path attendu au format 'bucket/chemin/vers/le/modele.onnx'."""
    import boto3
    import os

    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    local_path = Path(dest_dir) / Path(storage_path).name
    if local_path.exists():
        return str(local_path)

    s3 = boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
    )
    bucket, key = storage_path.split("/", 1)
    _ensure_bucket(bucket, s3_client=s3)
    logger.info(f"Téléchargement du modèle depuis s3://{storage_path}")
    s3.download_file(bucket, key, str(local_path))
    return str(local_path)


# ============================================================
# Chargement du modèle depuis la base de données
# ============================================================
def load_model_from_db() -> Tuple[YOLO, MLModel]:
    """
    Récupère le modèle marqué actif dans la table ml_models, résout son emplacement
    (fichier local ou objet S3/MinIO) et charge YOLO. Lève une erreur explicite
    si aucun modèle actif n'est configuré.
    """
    with get_db_session() as session:
        model_record = (
            session.query(MLModel)
            .filter(MLModel.is_active.is_(True))
            .order_by(MLModel.created_at.desc())
            .first()
        )

        if model_record is None:
            logger.error("Aucun modèle actif trouvé en base de données.")
            raise RuntimeError("Aucun modèle actif en base. Active un modèle dans la table ml_models.")

        if model_record.storage_backend == "s3":
            weights_path = _download_from_s3(model_record.storage_path)
        else:
            weights_path = model_record.storage_path
            if not Path(weights_path).exists():
                logger.error(f"Fichier de poids introuvable : {weights_path}")
                raise FileNotFoundError(f"Fichier de poids introuvable : {weights_path}")

        device = "0" if torch.cuda.is_available() else "cpu"
        logger.info(f"Chargement du modèle '{model_record.name}' v{model_record.version} sur device={device}")
        model = YOLO(weights_path, task="detect")

        session.expunge(model_record)  # détache l'objet pour l'utiliser hors de la session
        return model, model_record


# ============================================================
# Inférence + persistance + logs
# ============================================================
def inference(
    image_bytes: bytes,
    model: YOLO,
    model_record: MLModel,
    source_filename: Optional[str] = None,
    save_annotated: bool = True,
) -> PredictionResponse:
    """
    Exécute la détection sur une image, journalise la performance et les erreurs,
    et persiste le résultat (image + boites détectées) en base de données.
    """
    start = time.perf_counter()
    detection_id = None

    try:
        img_bgr = load_image_from_bytes(image_bytes)
        h, w = img_bgr.shape[:2]

        results = model.predict(
            source=img_bgr,
            conf=model_record.conf_threshold,
            iou=model_record.iou_threshold,
            verbose=False,
        )[0]

        detections_out: List[DetectionOut] = []
        for box in results.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cls_id = int(box.cls[0])
            detections_out.append(
                DetectionOut(
                    class_id=cls_id,
                    class_name=results.names[cls_id],
                    confidence=float(box.conf[0]),
                    bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2),
                )
            )

        elapsed_ms = (time.perf_counter() - start) * 1000

        annotated_b64 = encode_image_to_base64(results.plot()) if save_annotated else None

        # --- Persistance en base : image + boites + log de succès ---
        with get_db_session() as session:
            detection = Detection(
                model_id=model_record.id,
                source_filename=source_filename,
                image_width=w,
                image_height=h,
                inference_time_ms=elapsed_ms,
                num_detections=len(detections_out),
            )
            session.add(detection)
            session.flush()  # attribue detection.id avant le commit

            for det in detections_out:
                session.add(
                    DetectionBox(
                        detection_id=detection.id,
                        class_id=det.class_id,
                        class_name=det.class_name,
                        confidence=det.confidence,
                        x1=det.bbox.x1, y1=det.bbox.y1,
                        x2=det.bbox.x2, y2=det.bbox.y2,
                    )
                )

            session.add(
                InferenceLog(
                    level=LogLevel.INFO,
                    endpoint="/predict",
                    duration_ms=elapsed_ms,
                    success=True,
                    detection_id=detection.id,
                )
            )
            detection_id = detection.id

        logger.info(
            f"Inférence OK | {len(detections_out)} détection(s) | {elapsed_ms:.1f} ms | id={detection_id}"
        )

        return PredictionResponse(
            success=True,
            detection_id=detection_id,
            inference_time_ms=round(elapsed_ms, 2),
            image_width=w,
            image_height=h,
            num_detections=len(detections_out),
            detections=detections_out,
            annotated_image_base64=annotated_b64,
            model_info={"name": model_record.name, "version": model_record.version},
        )

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.error(f"Échec de l'inférence après {elapsed_ms:.1f} ms : {e}", exc_info=True)

        # On essaie quand même de tracer l'échec en base pour le monitoring
        try:
            with get_db_session() as session:
                session.add(
                    InferenceLog(
                        level=LogLevel.ERROR,
                        endpoint="/predict",
                        duration_ms=elapsed_ms,
                        success=False,
                        error_message=str(e),
                    )
                )
        except Exception as db_err:
            logger.error(f"Impossible d'écrire le log d'erreur en base : {db_err}")

        raise