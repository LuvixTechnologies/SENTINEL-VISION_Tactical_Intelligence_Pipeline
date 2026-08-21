#!/usr/bin/env bash
# Déploiement de SENTINEL-VISION sur Kubernetes (k3s - Linux / WSL2)
# À exécuter depuis la RACINE du projet
#
# Usage :
#   ./deploy.sh
#   ./deploy.sh --no-cache

set -euo pipefail

K8S_DIR="k8s_deploy"
RENDER_DIR="$(mktemp -d)"
NO_CACHE=""

if [[ "${1:-}" == "--no-cache" ]]; then
  NO_CACHE="--no-cache"
  echo "Mode --no-cache activé"
fi

trap 'rm -rf "$RENDER_DIR"' EXIT

echo "=== SENTINEL-VISION — Déploiement Kubernetes (Linux / k3s) ==="
echo

echo "--- Contexte kubectl ---"
kubectl config current-context
echo

# ---------------------------------------------------------------------------
# 1. Questions de configuration
# ---------------------------------------------------------------------------
echo "=== Configuration du déploiement ==="
echo

read -rp "Nom de domaine à exposer (ex: api.mondomaine.com) : " DOMAIN
if [[ -z "$DOMAIN" ]]; then
  echo "❌ Un domaine est requis pour configurer l'Ingress. Abandon."
  exit 1
fi

read -rp "Activer le HTTPS automatique via Let's Encrypt ? [Y/n] : " ENABLE_TLS
ENABLE_TLS="${ENABLE_TLS:-Y}"

LETSENCRYPT_EMAIL=""
if [[ "$ENABLE_TLS" =~ ^[Yy]$ ]]; then
  read -rp "Email à utiliser pour Let's Encrypt : " LETSENCRYPT_EMAIL
  if [[ -z "$LETSENCRYPT_EMAIL" ]]; then
    echo "❌ Un email est requis pour Let's Encrypt. Abandon."
    exit 1
  fi
fi

read -rp "Générer automatiquement des mots de passe forts pour Postgres/MinIO ? [Y/n] : " AUTO_PASS
AUTO_PASS="${AUTO_PASS:-Y}"

if [[ "$AUTO_PASS" =~ ^[Yy]$ ]]; then
  PG_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
  MINIO_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
  echo "→ Mots de passe générés (conserve-les, ils ne seront pas réaffichés) :"
  echo "   POSTGRES_PASSWORD = $PG_PASSWORD"
  echo "   MINIO_ROOT_PASSWORD = $MINIO_PASSWORD"
else
  read -rsp "Mot de passe Postgres : " PG_PASSWORD; echo
  read -rsp "Mot de passe MinIO : " MINIO_PASSWORD; echo
fi
echo

# ---------------------------------------------------------------------------
# 2. Build des images Docker
# ---------------------------------------------------------------------------
echo "--- Build des images Docker ---"
echo "→ detection-api:local"
docker build $NO_CACHE -f "$K8S_DIR/Dockerfile" -t detection-api:local .

echo "→ weights-loader:local"
docker build $NO_CACHE -f "$K8S_DIR/Dockerfile.weights" -t weights-loader:local .
echo

echo "--- Import des images dans k3s (containerd) ---"
docker save detection-api:local | sudo k3s ctr images import -
docker save weights-loader:local | sudo k3s ctr images import -
echo

# ---------------------------------------------------------------------------
# 3. Rendu des manifestes (secret, ingress, clusterissuer) avec les réponses
# ---------------------------------------------------------------------------
echo "--- Génération des manifestes finaux (secrets/domaine/TLS) ---"

# Chaque remplacement est ancré sur le nom de sa clé pour ne toucher QUE la
# bonne ligne (POSTGRES_PASSWORD et MINIO_ROOT_PASSWORD ont la même valeur
# placeholder "change_this_before_deploying" dans le fichier source : un
# remplacement global les confondrait).
sed -e "/POSTGRES_PASSWORD:/s/change_this_before_deploying/${PG_PASSWORD}/" \
    -e "/MINIO_ROOT_PASSWORD:/s/change_this_before_deploying/${MINIO_PASSWORD}/" \
    -e "/MINIO_SECRET_KEY:/s/change_this_before_deploying/${MINIO_PASSWORD}/" \
    -e "s#DATABASE_URL:.*#DATABASE_URL: postgresql://detection_user:${PG_PASSWORD}@postgres:5432/detection_db#" \
    "$K8S_DIR/1-secret.yaml" > "$RENDER_DIR/1-secret.yaml"

if [[ "$ENABLE_TLS" =~ ^[Yy]$ ]]; then
  CERT_ANNOTATION="cert-manager.io/cluster-issuer: letsencrypt-prod"
  TLS_BLOCK="$(printf 'tls:\n    - hosts:\n        - %s\n      secretName: detection-api-tls' "$DOMAIN")"
else
  CERT_ANNOTATION="# TLS désactivé (choix utilisateur)"
  TLS_BLOCK="# pas de TLS"
fi

sed -e "s#__DOMAIN__#${DOMAIN}#g" \
    -e "s#__CERT_MANAGER_ANNOTATION__#${CERT_ANNOTATION}#" \
    "$K8S_DIR/6-Ingress.yml" > "$RENDER_DIR/6-Ingress.yml"
# Insertion multi-lignes du bloc TLS via python pour éviter les soucis d'échappement sed
python3 - "$RENDER_DIR/6-Ingress.yml" "$TLS_BLOCK" << 'PYEOF'
import sys
path, tls_block = sys.argv[1], sys.argv[2]
content = open(path).read().replace("__TLS_BLOCK__", tls_block)
open(path, "w").write(content)
PYEOF

if [[ "$ENABLE_TLS" =~ ^[Yy]$ ]]; then
  sed -e "s/__EMAIL__/${LETSENCRYPT_EMAIL}/" \
      "$K8S_DIR/8-cluster_issuer.yaml" > "$RENDER_DIR/8-cluster_issuer.yaml"
fi
echo

# ---------------------------------------------------------------------------
# 4. cert-manager (si HTTPS demandé)
# ---------------------------------------------------------------------------
if [[ "$ENABLE_TLS" =~ ^[Yy]$ ]]; then
  if ! kubectl get ns cert-manager >/dev/null 2>&1; then
    echo "--- Installation de cert-manager ---"
    kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
    echo "Attente que cert-manager soit prêt..."
    kubectl wait --for=condition=Available --timeout=120s -n cert-manager deployment/cert-manager
    kubectl wait --for=condition=Available --timeout=120s -n cert-manager deployment/cert-manager-webhook
  else
    echo "cert-manager déjà présent, on passe."
  fi
  echo
  echo "--- Application du ClusterIssuer Let's Encrypt ---"
  kubectl apply -f "$RENDER_DIR/8-cluster_issuer.yaml"
  echo
fi

# ---------------------------------------------------------------------------
# 5. Application des manifestes Kubernetes
# ---------------------------------------------------------------------------
echo "--- Vérification des secrets rendus ---"
if grep -q "change_this_before_deploying" "$RENDER_DIR/1-secret.yaml" 2>/dev/null; then
  echo "❌ Substitution des mots de passe échouée. Abandon."
  exit 1
fi

echo "--- Application des manifestes Kubernetes ---"
kubectl apply -f "$K8S_DIR/0-namespace.yaml"
kubectl apply -f "$RENDER_DIR/1-secret.yaml"
kubectl apply -f "$K8S_DIR/2-postgre.yaml"
kubectl apply -f "$K8S_DIR/3-minio.yaml"
kubectl apply -f "$K8S_DIR/4-api_configmap.yml"
echo

echo "--- Attente que Postgres et MinIO soient prêts ---"
kubectl rollout status statefulset/postgres -n sentinel-vision --timeout=120s
kubectl rollout status deployment/minio -n sentinel-vision --timeout=120s
echo

echo "--- Déploiement de l'API ---"
kubectl apply -f "$K8S_DIR/5-api_deployment.yml"
kubectl apply -f "$K8S_DIR/7-pdb.yml"
echo

echo "--- Application de l'Ingress (rendu avec ton domaine) ---"
kubectl apply -f "$RENDER_DIR/6-Ingress.yml"
echo

echo "--- Attente que l'API soit prête (initContainers inclus) ---"
kubectl rollout status deployment/detection-api -n sentinel-vision --timeout=300s
echo

echo "=== Déploiement terminé ==="
kubectl get pods -n sentinel-vision
echo
echo "Services :"
kubectl get svc -n sentinel-vision
echo
echo "Ingress :"
kubectl get ingress -n sentinel-vision 2>/dev/null || echo "(aucun)"

if [[ "$ENABLE_TLS" =~ ^[Yy]$ ]]; then
  echo
  echo "Certificat (peut prendre 1-2 min à passer à READY=True) :"
  kubectl get certificate -n sentinel-vision 2>/dev/null || echo "(pas encore créé)"
  echo
  echo "→ Une fois prêt : https://${DOMAIN}"
else
  echo
  echo "→ http://${DOMAIN}"
fi