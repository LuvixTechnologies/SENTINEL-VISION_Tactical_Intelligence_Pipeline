#!/usr/bin/env bash
# Déploiement de SENTINEL-VISION sur Kubernetes (k3s - Linux / WSL2)
# À exécuter depuis la RACINE du projet
#
# Usage :
#   ./deploy.sh
#   ./deploy.sh --no-cache

set -euo pipefail

K8S_DIR="k8s_deploy"
NO_CACHE=""

if [[ "${1:-}" == "--no-cache" ]]; then
  NO_CACHE="--no-cache"
  echo "Mode --no-cache activé"
fi

echo "=== SENTINEL-VISION — Déploiement Kubernetes (Linux / k3s) ==="
echo

echo "--- Contexte kubectl ---"
kubectl config current-context
echo

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

echo "--- Vérification des secrets ---"
if grep -q "change_this_before_deploying" "$K8S_DIR/1-secret.yaml" 2>/dev/null; then
  echo "⚠️  $K8S_DIR/1-secret.yaml contient encore les mots de passe par défaut."
  echo "   Édite ce fichier avant de continuer (Ctrl+C pour annuler, ou Entrée pour continuer)."
  read -r
  echo
fi

echo "--- Application des manifestes Kubernetes ---"
kubectl apply -f "$K8S_DIR/0-namespace.yaml"
kubectl apply -f "$K8S_DIR/1-secret.yaml"
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

echo "--- Application de l'Ingress (Traefik déjà présent sur k3s) ---"
if [ -f "$K8S_DIR/6-Ingress.yml" ]; then
  kubectl apply -f "$K8S_DIR/6-Ingress.yml"
else
  echo "⚠️  Fichier $K8S_DIR/6-Ingress.yml introuvable"
fi
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