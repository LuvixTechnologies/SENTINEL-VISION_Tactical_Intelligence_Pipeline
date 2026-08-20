# ---------- Stage 1 : build des dépendances ----------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# ---------- Stage 2 : image finale, minimale ----------
FROM python:3.12-slim

# Dépendances runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=builder /root/.local /home/appuser/.local
# Copie explicite du code applicatif seulement — jamais COPY . . (ni COPY .. .).
# Le contexte de build est déjà la racine du projet (voir la commande docker build,
# le "." final), donc les chemins ci-dessous sont directs, sans "..".
# Ça évite d'embarquer weights/, k8s_deploy/, UI/, etc. dans cette image :
# le modèle arrive par MinIO au runtime, pas depuis le disque du build.
COPY api.py database.py db_models.py inference_service.py logging_config.py seed_model.py ./

RUN chown -R appuser:appuser /app

USER appuser

ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_CONCURRENCY=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY}"]