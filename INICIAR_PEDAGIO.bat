@echo off
setlocal EnableExtensions DisableDelayedExpansion
title PEDAGIO PORTAL
set "ROOT=%~dp0"
pushd "%ROOT%" >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Nao foi possivel acessar a pasta do projeto.
  exit /b 1
)
set "ROOT=%CD%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "APP_EXE=%ROOT%\PEDAGIO_PORTAL.exe"
set "LOG=%ROOT%\logs"
set "PID_FILE=%ROOT%\servidor.pid"
set "PORT=8080"

if exist "%ROOT%\.env" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%ROOT%\.env") do if /I "%%A"=="CONFERENCE_PORT" set "PORT=%%B"
)
call :vport
set "LOCAL_URL=http://127.0.0.1:%PORT%"

if not exist "%APP_EXE%" if not exist "%PY%" (
  echo [ERRO] Ambiente Python nao encontrado.
  echo Execute o instalador novamente ou use o pacote portatil completo.
  pause
  exit /b 1
)
if not exist "%LOG%" mkdir "%LOG%" >nul 2>&1

if exist "%PID_FILE%" (
  set /p PED_PID=<"%PID_FILE%"
  set "PED_PID=%PED_PID: =%"
  powershell.exe -NoProfile -Command "try{$id=[int]$env:PED_PID; if(Get-Process -Id $id -ErrorAction SilentlyContinue){exit 0}}catch{}; exit 1" >nul 2>&1
  if errorlevel 1 del /q "%PID_FILE%" >nul 2>&1
)

echo ================================================
echo              PEDAGIO PORTAL
echo ================================================
echo.
echo [1/3] Verificando servidor local...
call :ownserver
if not errorlevel 1 goto :OPEN_BROWSER

call :portbusy
if not errorlevel 1 (
  echo [ERRO] A porta %PORT% esta ocupada por outro servico local.
  echo Nenhum processo foi encerrado. Consulte a TI ou ajuste CONFERENCE_PORT no .env.
  pause
  goto :END
)

echo [2/3] Iniciando servidor em 127.0.0.1:%PORT%...
set "PED_PY=%PY%"
set "PED_ROOT=%ROOT%"
set "PED_OUT=%LOG%\servidor_saida.log"
set "PED_ERR=%LOG%\servidor_erro.log"
if exist "%APP_EXE%" (
  set "PED_EXECUTABLE=%APP_EXE%"
  set "PED_ARGS=--host|127.0.0.1|--port|%PED_PORT%"
) else (
  set "PED_EXECUTABLE=%PY%"
  set "PED_ARGS=app.py|--host|127.0.0.1|--port|%PED_PORT%"
)
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $args=$env:PED_ARGS -split '\|'; $p=Start-Process -FilePath $env:PED_EXECUTABLE -ArgumentList $args -WorkingDirectory $env:PED_ROOT -WindowStyle Hidden -RedirectStandardOutput $env:PED_OUT -RedirectStandardError $env:PED_ERR -PassThru; [System.IO.File]::WriteAllText((Join-Path $env:PED_ROOT 'servidor.pid'), [string]$p.Id, [System.Text.Encoding]::ASCII)" >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Nao foi possivel iniciar o servidor.
  echo Consulte: %PED_ERR%
  pause
  exit /b 1
)

for /L %%I in (1,1,30) do (
  timeout /t 1 /nobreak >nul
  call :ownserver
  if not errorlevel 1 goto :OPEN_BROWSER
)

echo [ERRO] O servidor nao respondeu dentro do limite de seguranca.
echo Consulte: %PED_ERR%
pause
exit /b 1

:OPEN_BROWSER
echo [3/3] Abrindo a ferramenta no navegador padrao...
start "" "%LOCAL_URL%"
goto :END

:ownserver
powershell.exe -NoProfile -Command "try{$r=Invoke-WebRequest -UseBasicParsing -Uri $env:LOCAL_URL -TimeoutSec 2; if($r.StatusCode -eq 200 -and $r.Content -match 'access-form' -and $r.Content -match 'static/js/access.js'){exit 0}; exit 1}catch{exit 1}" >nul 2>&1
exit /b %errorlevel%

:portbusy
powershell.exe -NoProfile -Command "$listener=Get-NetTCPConnection -LocalPort ([int]$env:PED_PORT) -State Listen -ErrorAction SilentlyContinue; if($listener){exit 0}; exit 1" >nul 2>&1
exit /b %errorlevel%

:vport
for /f "delims=0123456789" %%A in ("%PORT%") do set "PORT=8080"
if "%PORT%"=="" set "PORT=8080"
if %PORT% LSS 1 set "PORT=8080"
if %PORT% GTR 65535 set "PORT=8080"
set "PED_PORT=%PORT%"
exit /b 0

:END
popd
exit /b 0
