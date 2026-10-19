@echo off
setlocal EnableExtensions
title PEDAGIO PORTAL
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "LOG=%ROOT%logs"
set "LOCAL_URL=http://127.0.0.1:8080"
set "SENIOR_URL=https://platform.senior.com.br/login/?redirectTo=https://platform.senior.com.br/senior-x/&tenant=gruposbf.com.br"

if not exist "%PY%" (
  echo [ERRO] Ambiente Python nao encontrado.
  echo Execute o instalador novamente.
  pause
  exit /b 1
)
if not exist "%LOG%" mkdir "%LOG%" >nul 2>&1

echo ================================================
echo              PEDAGIO PORTAL
echo ================================================
echo.
echo [1/3] Verificando servidor local...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try{$r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8080/' -TimeoutSec 2; exit 0}catch{exit 1}" >nul 2>&1
if not errorlevel 1 goto :OPEN_BROWSER

echo [2/3] Iniciando servidor em 127.0.0.1:8080...
set "PED_PY=%PY%"
set "PED_ROOT=%ROOT%"
set "PED_OUT=%LOG%\servidor_saida.log"
set "PED_ERR=%LOG%\servidor_erro.log"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath $env:PED_PY -ArgumentList @('app.py','--host','127.0.0.1','--port','8080') -WorkingDirectory $env:PED_ROOT -WindowStyle Hidden -RedirectStandardOutput $env:PED_OUT -RedirectStandardError $env:PED_ERR -PassThru; $p.Id | Set-Content -Encoding ASCII (Join-Path $env:PED_ROOT 'servidor.pid')" >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Nao foi possivel iniciar o servidor.
  echo Consulte: %PED_ERR%
  pause
  exit /b 1
)

for /L %%I in (1,1,30) do (
  timeout /t 1 /nobreak >nul
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try{$r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8080/' -TimeoutSec 2; exit 0}catch{exit 1}" >nul 2>&1
  if not errorlevel 1 goto :OPEN_BROWSER
)

echo [ERRO] O servidor nao respondeu dentro do limite de seguranca.
echo Consulte: %PED_ERR%
pause
exit /b 1

:OPEN_BROWSER
echo [3/3] Abrindo Google Chrome...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=@(\"$env:ProgramFiles\Google\Chrome\Application\chrome.exe\",\"${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe\",\"$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe\") | Where-Object {$_ -and (Test-Path $_)} | Select-Object -First 1; if(-not $p){$c=Get-Command chrome.exe -ErrorAction SilentlyContinue; if($c){$p=$c.Source}}; if($p){$a=@('--incognito','http://127.0.0.1:8080','https://platform.senior.com.br/login/?redirectTo=https://platform.senior.com.br/senior-x/&tenant=gruposbf.com.br'); Start-Process -FilePath $p -ArgumentList $a; exit 0}else{exit 2}" >nul 2>&1
if errorlevel 1 (
  echo [AVISO] Google Chrome nao foi encontrado.
  echo Abrindo no navegador padrao do Windows.
  start "" "%LOCAL_URL%"
  start "" "%SENIOR_URL%"
)
exit /b 0
