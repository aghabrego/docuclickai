#!/usr/bin/env bash
# Build del binario docuclickai dentro de un contenedor Docker
# con base Debian 11 (GLIBC 2.31), para máxima portabilidad.
#
# Resultado: helper/dist/docuclickai (~50 MB, un solo archivo).
#
# Uso:
#   cd helper && ./build-docker.sh
#
# Requisitos:
#   - Docker instalado y funcionando (docker info debe responder).
#
# El binario generado funciona en:
#   - Debian 11 (bullseye) en adelante
#   - Ubuntu 20.04+ en adelante
#   - Cualquier Linux con GLIBC >= 2.31

set -euo pipefail

cd "$(dirname "$0")"

IMAGE_NAME="docuclickai-builder"

echo "[build-docker] Verificando Docker..."
if ! docker info >/dev/null 2>&1; then
    echo "[build-docker] ERROR: Docker no responde. ¿Está el daemon corriendo?" >&2
    exit 1
fi

echo "[build-docker] Construyendo imagen base ($IMAGE_NAME)..."
docker build \
    -f Dockerfile.build \
    -t "$IMAGE_NAME" \
    --quiet \
    .

echo "[build-docker] Limpiando builds previos..."
rm -rf build/ dist/

echo "[build-docker] Ejecutando PyInstaller dentro del contenedor..."
# Montamos el directorio actual como /src para que el spec vea los módulos.
# pyinstaller corre desde /src (el WORKDIR del Dockerfile).
docker run --rm \
    -v "$PWD":/src \
    -w /src \
    "$IMAGE_NAME" \
        --onefile \
        --name docuclickai \
        --log-level WARN \
        --noconfirm \
        --paths . \
        --hidden-import ollama_client \
        --hidden-import pdf_extractor \
        --hidden-import events \
        --hidden-import tmp_manager \
        --hidden-import preflight \
        --distpath /src/dist \
        --workpath /src/build \
        --specpath /src \
        docuclickai

BIN=dist/docuclickai
if [[ ! -x "$BIN" ]]; then
    echo "[build-docker] ERROR: no se generó $BIN" >&2
    exit 1
fi

# Arregla ownership: los archivos creados dentro del contenedor pertenecen a root.
# Los hacemos escribibles para que el usuario local pueda sobrescribirlos.
sudo chown -R "$(id -u):$(id -g)" build/ dist/ 2>/dev/null || \
    chown -R "$(id -u):$(id -g)" build/ dist/ 2>/dev/null || true

# Limpia la imagen para no dejar basura (~600 MB).
echo "[build-docker] Limpiando imagen temporal..."
docker rmi "$IMAGE_NAME" >/dev/null 2>&1 || true

SIZE=$(du -h "$BIN" | cut -f1)
echo
echo "[build-docker] OK: $BIN ($SIZE)"
echo
echo "Próximos pasos:"
echo "  1. Probar localmente:  $BIN --help"
echo "  2. Verificar requisitos GLIBC del server destino:"
echo "       ssh user@server 'ldd --version | head -1'   # debe ser >= 2.31"
echo "  3. Subir al server:"
echo "       scp $BIN user@server:/usr/local/bin/docuclickai"
echo "  4. En el server:"
echo "       docuclickai --help"
echo
echo "Si Ollama/modelo faltan, el binario falla con exit 5/6 e instrucciones."
