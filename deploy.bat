@echo off
setlocal enabledelayedexpansion

REM Déploiement de SENTINEL-VISION sur Kubernetes (Docker Desktop)
REM À exécuter depuis la RACINE du projet

set K8S_DIR=k8s_deploy

echo === SENTINEL-VISION - Deploiement Kubernetes (Windows / Docker Desktop) ===
echo.

echo --- Verification du contexte kubectl ---
kubectl config current-context
echo (doit afficher "docker-desktop" - sinon : kubectl config use-context docker-desktop)
pause
echo.

echo --- Build des images Docker ---
docker build -f %K8S_DIR%\Dockerfile -t detection-api:local .
if errorlevel 1 (
    echo ERREUR lors du build de detection-api:local
    exit /b 1
)

docker build -f %K8S_DIR%\Dockerfile.weights -t weights-loader:local .
if errorlevel 1 (
    echo ERREUR lors du build de weights-loader:local
    exit /b 1
)
echo.

echo --- Verification des secrets ---
findstr /C:"change_this_before_deploying" %K8S_DIR%\1-secret.yaml >nul
if not errorlevel 1 (
    echo ATTENTION : %K8S_DIR%\1-secret.yaml contient encore les mots de passe par defaut.
    echo Edite ce fichier avant de continuer.
    pause
)
echo.

echo --- Application des manifestes Kubernetes ---
kubectl apply -f %K8S_DIR%\0-namespace.yaml
kubectl apply -f %K8S_DIR%\1-secret.yaml
kubectl apply -f %K8S_DIR%\2-postgre.yaml
kubectl apply -f %K8S_DIR%\3-minio.yaml
kubectl apply -f %K8S_DIR%\4-api_configmap.yml
echo.

echo --- Attente que Postgres et MinIO soient prets ---
kubectl rollout status statefulset/postgres -n sentinel-vision --timeout=120s
if errorlevel 1 (
    echo ERREUR : Postgres n'est pas devenu pret a temps.
    exit /b 1
)
kubectl rollout status deployment/minio -n sentinel-vision --timeout=120s
if errorlevel 1 (
    echo ERREUR : MinIO n'est pas devenu pret a temps.
    exit /b 1
)
echo.

echo --- Deploiement de l'API ---
kubectl apply -f %K8S_DIR%\5-api_deployment.yml
kubectl apply -f %K8S_DIR%\7-pdb.yml
echo.

echo --- Ingress ---
echo Ingress non applique automatiquement sur Docker Desktop.
echo.
echo Options pour exposer l'API :
echo   1. Port-forward rapide :
echo        kubectl port-forward -n sentinel-vision svc/detection-api 8000:80
echo.
echo   2. Installer ingress-nginx puis appliquer l'Ingress :
echo        kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.1/deploy/static/provider/cloud/deploy.yaml
echo        kubectl apply -f %K8S_DIR%\6-Ingress.yml
echo.

echo --- Attente que l'API soit prete (initContainers inclus) ---
kubectl rollout status deployment/detection-api -n sentinel-vision --timeout=300s
echo.

echo === Deploiement termine ===
kubectl get pods -n sentinel-vision
echo.
kubectl get svc -n sentinel-vision

endlocal