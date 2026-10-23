#!/usr/bin/env bash
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null || PY=python
echo "Instalando y actualizando dependencias..."
$PY -m pip install -U --disable-pip-version-check --prefer-binary -r requirements.txt || echo "[AVISO] No se pudieron actualizar dependencias."
echo; echo "Iniciando VidBajar Pro..."
$PY app.py
