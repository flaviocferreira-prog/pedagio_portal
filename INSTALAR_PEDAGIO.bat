@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Instalador PEDAGIO PORTAL

set "ROOT=%~dp0"
pushd "%ROOT%" >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Nao foi possivel acessar a pasta do pacote.
  exit /b 1
)
set "ROOT=%CD%"
set "LOG_DIR=%ROOT%\logs"
set "LOG_FILE=%LOG_DIR%\instalacao_pedagio.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
echo [%DATE% %TIME%] Inicio da validacao do pacote portatil.>> "%LOG_FILE%"

if not exist "%ROOT%\PEDAGIO_PORTAL.exe" goto :missing
if not exist "%ROOT%\_internal" goto :missing
if not exist "%ROOT%\INICIAR_PEDAGIO.bat" goto :missing
if not exist "%ROOT%\alerta_logistica.ico" goto :missing

for %%D in ("%ROOT%\data" "%ROOT%\data\temporary_uploads" "%ROOT%\logs" "%ROOT%\backups") do if not exist "%%~D" mkdir "%%~D" >nul 2>&1
if not exist "%ROOT%\.env" if exist "%ROOT%\.env.example" (
  copy /Y "%ROOT%\.env.example" "%ROOT%\.env" >nul
  if errorlevel 1 goto :env_error
  echo [%DATE% %TIME%] .env criado a partir do modelo sem credenciais.>> "%LOG_FILE%"
)

powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $desktop=[Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory); if([string]::IsNullOrWhiteSpace($desktop)){throw 'Area de Trabalho indisponivel.'}; $launcher=Join-Path $env:ROOT 'INICIAR_PEDAGIO.bat'; $icon=Join-Path $env:ROOT 'alerta_logistica.ico'; $shell=New-Object -ComObject WScript.Shell; $name='Alerta Log' + [char]0x00ED + 'stica.lnk'; $shortcut=$shell.CreateShortcut((Join-Path $desktop $name)); $shortcut.TargetPath=$launcher; $shortcut.Arguments=''; $shortcut.WorkingDirectory=$env:ROOT; $shortcut.IconLocation=$icon; $shortcut.Description='Iniciar PEDAGIO PORTAL'; $shortcut.Save()" >nul 2>&1
if errorlevel 1 goto :shortcut_error

echo [OK] Pacote portatil validado. Nenhuma biblioteca foi instalada.
echo [OK] Atalho "Alerta Logistica" criado ou atualizado na Area de Trabalho.
echo [%DATE% %TIME%] Instalacao portatil concluida.>> "%LOG_FILE%"
goto :end

:missing
echo [ERRO] Arquivos obrigatorios do pacote nao foram encontrados.
echo Extraia o ZIP completo antes de executar este arquivo.
echo [%DATE% %TIME%] ERRO: pacote incompleto.>> "%LOG_FILE%"
goto :failed

:env_error
echo [ERRO] Nao foi possivel criar .env a partir de .env.example.
echo [%DATE% %TIME%] ERRO: criacao de .env falhou.>> "%LOG_FILE%"
goto :failed

:shortcut_error
echo [AVISO] Pacote validado, mas o atalho nao foi criado.
echo Execute INICIAR_PEDAGIO.bat diretamente ou solicite suporte da TI.
echo [%DATE% %TIME%] AVISO: criacao do atalho falhou.>> "%LOG_FILE%"
goto :end

:failed
pause

:end
popd
exit /b 0
