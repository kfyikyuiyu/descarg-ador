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
echo Instalando y actualizando dependencias...
%PY% -m pip install -U --disable-pip-version-check --prefer-binary -r requirements.txt
if errorlevel 1 (
  echo.
  echo [AVISO] No se pudieron actualizar las dependencias. Revisa tu conexion a internet.
  echo Se intentara iniciar de todos modos con lo que ya este instalado...
)
echo.
echo Iniciando VidBajar Pro...
%PY% app.py
echo.
echo El programa se cerro. Si ves un error arriba, copialo para pedir ayuda.
pause
