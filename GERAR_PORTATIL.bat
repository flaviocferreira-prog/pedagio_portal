@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
pushd "%ROOT%" >nul 2>&1 || exit /b 1
set "ROOT=%CD%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "OUT=%ROOT%\dist"

if not exist "%PY%" (
  echo [ERRO] .venv nao encontrada. Execute INSTALAR_PEDAGIO.bat antes do build.
  popd
  exit /b 1
)
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo [ERRO] PyInstaller nao encontrado na .venv.
  echo Instale as dependencias de build com: "%PY%" -m pip install -r requirements-build.txt
  popd
  exit /b 1
)

"%PY%" -m PyInstaller --noconfirm --clean --onedir --name PEDAGIO_PORTAL --add-data "access.html;." --add-data "index.html;." --add-data "static;static" --add-data "alerta_logistica.ico;." --collect-all openpyxl --collect-data tzdata app.py
if errorlevel 1 (
  echo [ERRO] Falha ao gerar o executavel portatil.
  popd
  exit /b 1
)

echo [OK] Build criado em "%OUT%\PEDAGIO_PORTAL".
popd
exit /b 0
