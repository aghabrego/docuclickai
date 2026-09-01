#!/usr/bin/env bash
# Build del binario standalone de DocuClickAI con PyInstaller.
# Resultado: helper/dist/docuclickai (~50 MB, un solo archivo).
#
# Uso:
#   cd helper && ./build.sh
#
# Requisitos:
#   pip install -r requirements-dev.txt   # PyInstaller
#   pip install -r requirements.txt       # deps runtime (ollama, pypdf, redis)

set -euo pipefail

cd "$(dirname "$0")"

echo "[build] Limpiando builds previos..."
rm -rf build/ dist/

echo "[build] PyInstaller --onefile --name docuclickai docuclickai"
pyinstaller \
    --onefile \
    --name docuclickai \
    --log-level WARN \
    --noconfirm \
    --paths . \
    --hidden-import ollama_client \
    --hidden-import openai_client \
    --hidden-import llm_client \
    --hidden-import pdf_extractor \
    --hidden-import events \
    --hidden-import tmp_manager \
    --hidden-import preflight \
    --hidden-import dotenv_loader \
    docuclickai

BIN=dist/docuclickai
if [[ ! -x "$BIN" ]]; then
    echo "[build] ERROR: no se generó $BIN"
    exit 1
fi

SIZE=$(du -h "$BIN" | cut -f1)
echo
echo "[build] OK: $BIN ($SIZE)"
echo
echo "Próximos pasos:"
echo "  1. Probar localmente:  $BIN --help"
echo "  2. Copiar a otro server:"
echo "       scp $BIN user@server:/usr/local/bin/docuclickai"
echo "  3. En el server destino:"
echo "       docuclickai --pdf ruta/al.pdf"
echo
echo "El binario NO autoinstala nada. Si Ollama/modelo faltan, fallará"
echo "con exit 5/6 e instrucciones."