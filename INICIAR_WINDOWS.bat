@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY python -c "import sys" >nul 2>nul && set PY=python
if not defined PY (
  echo No se encontro Python. Se abrira la pagina de descarga.
  echo IMPORTANTE: al instalar marca la casilla "Add Python to PATH".
  start https://www.python.org/downloads/
  pause
  exit /b
)
echo Instalando y actualizando (necesario para que TikTok y YouTube funcionen)...
%PY% -m pip install -U --disable-pip-version-check --prefer-binary yt-dlp curl_cffi imageio-ffmpeg
echo.
%PY% app.py
pause
