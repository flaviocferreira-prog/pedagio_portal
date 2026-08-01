@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente nao encontrado. Execute instalar.bat primeiro.
  exit /b 1
)
call .venv\Scripts\activate.bat
python app.py
if errorlevel 1 echo A aplicacao foi encerrada com erro. Verifique a mensagem acima.
