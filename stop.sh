#!/usr/bin/env bash
# Arrêt de SENTINEL-VISION sur Kubernetes (k3s)
#
# Usage :
#   ./stop.sh api          # met en pause uniquement l'API (Postgres/MinIO restent actifs)
#   ./stop.sh all          # met en pause API + Postgres + MinIO (données conservées)
#   ./stop.sh start        # relance ce qui a été mis en pause (API + Postgres + MinIO)
#   ./stop.sh destroy      # supprime TOUT le namespace (⚠️ données perdues sauf PV en Retain)

set -euo pipefail

NAMESPACE="sentinel-vision"
MODE="${1:-}"

usage() {
  echo "Usage: $0 {api|all|start|destroy}"
  echo "  api      - met en pause uniquement detection-api"
  echo "  all      - met en pause detection-api + minio + postgres"
  echo "  start    - relance detection-api + minio + postgres (replicas=1)"
  echo "  destroy  - supprime tout le namespace $NAMESPACE (destructif)"
  exit 1
}

[[ -z "$MODE" ]] && usage

case "$MODE" in
  api)
    echo "--- Mise en pause de detection-api ---"
    kubectl scale deployment/detection-api -n "$NAMESPACE" --replicas=0
    echo "→ API arrêtée. Postgres et MinIO restent actifs."
    ;;

  all)
    echo "--- Mise en pause de l'ensemble des services ---"
    kubectl scale deployment/detection-api -n "$NAMESPACE" --replicas=0
    kubectl scale deployment/minio -n "$NAMESPACE" --replicas=0
    kubectl scale statefulset/postgres -n "$NAMESPACE" --replicas=0
    echo "→ Tout est arrêté. Les données (PVC) sont conservées."
    echo "→ Pour redémarrer : ./stop.sh start"
    ;;

  start)
    echo "--- Redémarrage des services ---"
    kubectl scale statefulset/postgres -n "$NAMESPACE" --replicas=1
    echo "Attente que Postgres soit prêt..."
    kubectl rollout status statefulset/postgres -n "$NAMESPACE" --timeout=120s
    kubectl scale deployment/minio -n "$NAMESPACE" --replicas=1
    kubectl rollout status deployment/minio -n "$NAMESPACE" --timeout=120s
    kubectl scale deployment/detection-api -n "$NAMESPACE" --replicas=1
    kubectl rollout status deployment/detection-api -n "$NAMESPACE" --timeout=300s
    echo "→ Tout est reparti."
    echo
    echo "Rappel : si tu relances ./deploy.sh entre-temps (au lieu de ./stop.sh start),"
    echo "il régénère un NOUVEAU mot de passe Postgres/MinIO. Le secret k8s change,"
    echo "mais le mot de passe déjà initialisé DANS le volume Postgres, lui, ne change"
    echo "pas automatiquement -> l'API ne pourra plus se connecter à la base."
    echo "Pour un simple redémarrage, utilise toujours ./stop.sh start, jamais ./deploy.sh."
    ;;

  destroy)
    echo "Ceci va supprimer le namespace '$NAMESPACE' et TOUTES ses ressources,"
    echo "y compris les PVC (données Postgres/MinIO), sauf si leur reclaimPolicy est 'Retain'."
    read -rp "Tape le nom du namespace pour confirmer ($NAMESPACE) : " CONFIRM
    if [[ "$CONFIRM" != "$NAMESPACE" ]]; then
      echo "Confirmation invalide. Abandon (aucune suppression effectuée)."
      exit 1
    fi
    kubectl delete namespace "$NAMESPACE"
    echo "→ Namespace supprimé."
    ;;

  *)
    usage
    ;;
esac