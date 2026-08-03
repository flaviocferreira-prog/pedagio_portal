@echo off
setlocal
cd /d "%~dp0"
py -3 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 ou superior nao foi encontrado. Instale o Python e execute novamente.
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 goto :erro
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :erro
python -m pip install -r requirements.txt
if errorlevel 1 goto :erro
if not exist data mkdir data
if not exist data\temporary_uploads mkdir data\temporary_uploads
python -c "from conferencia.infrastructure.database import SQLiteDatabase; SQLiteDatabase().initialize(); print('Banco inicializado com sucesso.')"
if errorlevel 1 goto :erro
echo.
echo Instalacao concluida com sucesso. Execute iniciar.bat.
exit /b 0
:erro
echo.
echo A instalacao nao foi concluida. Verifique a mensagem acima.
exit /b 1
