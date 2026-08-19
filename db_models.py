"""
Modèles SQLAlchemy.

MLModel        registre des versions de modèles YOLO/ONNX (un seul actif à la fois)
Detection      une image passée en inférence, avec ses métadonnées globales
DetectionBox   une boite détectée sur une image (relation 1-N avec Detection)
InferenceLog   historique des appels : performance + erreurs, exploitable pour un dashboard
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, ForeignKey, Text, Enum
)
from sqlalchemy.orm import relationship

from database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class MLModel(Base):
    """Un modèle ne devient utilisable par l'API que si is_active=True."""
    __tablename__ = "ml_models"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)               # ex: "yolo-military-detection"
    version = Column(String, nullable=False)             # ex: "v1.3.0"
    storage_path = Column(String, nullable=False)         # chemin local OU clé S3 (bucket/models/v1.3.0/best.onnx)
    storage_backend = Column(String, default="local")     # "local" | "s3"
    conf_threshold = Column(Float, default=0.25)
    iou_threshold = Column(Float, default=0.45)
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    detections = relationship("Detection", back_populates="model")


class Detection(Base):
    """Une image passée en inférence."""
    __tablename__ = "detections"

    id = Column(String, primary_key=True, default=gen_uuid)
    model_id = Column(String, ForeignKey("ml_models.id"))
    source_filename = Column(String, nullable=True)
    image_width = Column(Integer)
    image_height = Column(Integer)
    inference_time_ms = Column(Float)
    num_detections = Column(Integer, default=0)
    annotated_image_path = Column(String, nullable=True)  # clé S3 de l'image annotée si sauvegardée
    created_at = Column(DateTime, default=datetime.utcnow)

    model = relationship("MLModel", back_populates="detections")
    boxes = relationship("DetectionBox", back_populates="detection", cascade="all, delete-orphan")


class DetectionBox(Base):
    """Une boite englobante détectée sur une image."""
    __tablename__ = "detection_boxes"

    id = Column(String, primary_key=True, default=gen_uuid)
    detection_id = Column(String, ForeignKey("detections.id"))
    class_id = Column(Integer)
    class_name = Column(String)
    confidence = Column(Float)
    x1 = Column(Float)
    y1 = Column(Float)
    x2 = Column(Float)
    y2 = Column(Float)

    detection = relationship("Detection", back_populates="boxes")


class LogLevel(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class InferenceLog(Base):
    """Historique des appels d'inférence : perf + erreurs. Base pour un dashboard Grafana."""
    __tablename__ = "inference_logs"

    id = Column(String, primary_key=True, default=gen_uuid)
    level = Column(Enum(LogLevel), default=LogLevel.INFO)
    endpoint = Column(String, default="/predict")
    duration_ms = Column(Float, nullable=True)
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    detection_id = Column(String, ForeignKey("detections.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)