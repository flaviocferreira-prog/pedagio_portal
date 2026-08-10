@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente nao encontrado. Execute instalar.bat primeiro.
  pause
  exit /b 1
)
rem O ambiente criado pelo instalador valida: sys.version_info >= (3, 11)
rem Mensagem padrao de requisito: Python %PYTHON_VERSION% encontrado

set "APP_PORT=8080"
if exist ".env" (
  for /f "tokens=1,2 delims==" %%A in ('findstr /r "^CONFERENCE_PORT=" ".env"') do if /I "%%A"=="CONFERENCE_PORT" set "APP_PORT=%%B"
)
set "APP_URL=http://127.0.0.1:%APP_PORT%/"
set "CV_APP_URL=%APP_URL%"

call :server_ready
if not errorlevel 1 (
  echo A ferramenta ja esta em execucao.
  exit /b 0
)

if not exist "logs" mkdir "logs"
start "" /b ".venv\Scripts\python.exe" app.py --host 127.0.0.1 --port %APP_PORT% >> "logs\server.log" 2>&1
set /a ATTEMPT=0
:wait_server
set /a ATTEMPT+=1
call :server_ready
if not errorlevel 1 goto :open_chrome
if !ATTEMPT! GEQ 30 goto :server_error
timeout /t 1 /nobreak >nul
goto :wait_server

:open_chrome
call :find_chrome
if not defined CHROME_EXE goto :chrome_error
start "" "%CHROME_EXE%" --no-first-run --new-window "%APP_URL%"
exit /b 0

:server_ready
powershell -NoProfile -Command "try { $response=Invoke-WebRequest -UseBasicParsing -Uri $env:CV_APP_URL -TimeoutSec 2; if($response.StatusCode -ge 200 -and $response.StatusCode -lt 500){exit 0}; exit 1 } catch { exit 1 }" >nul 2>&1
exit /b %ERRORLEVEL%

:find_chrome
set "CHROME_EXE="
for %%P in ("%ProgramFiles%\Google\Chrome\Application\chrome.exe" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" "%LocalAppData%\Google\Chrome\Application\chrome.exe") do if exist "%%~P" set "CHROME_EXE=%%~P"
exit /b 0

:chrome_error
echo O Google Chrome nao foi encontrado. O servidor esta pronto em %APP_URL%
pause
exit /b 1

:server_error
echo Nao foi possivel iniciar a ferramenta. Consulte logs\server.log.
pause
exit /b 1
