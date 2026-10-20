@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
pushd "%ROOT%" >nul 2>&1 || exit /b 1
set "ROOT=%CD%"
set "DIST=%ROOT%\dist\PEDAGIO_PORTAL"
set "RELEASE=%ROOT%\release"
set "PACKAGE=%RELEASE%\PEDAGIO_PORTAL_PORTATIL_v1.0.0.zip"

if not exist "%DIST%\PEDAGIO_PORTAL.exe" (
  echo [ERRO] Executavel nao encontrado. Execute GERAR_PORTATIL.bat antes.
  popd
  exit /b 1
)
if exist "%RELEASE%\PEDAGIO_PORTAL" rmdir /s /q "%RELEASE%\PEDAGIO_PORTAL"
if exist "%PACKAGE%" del /q "%PACKAGE%"
mkdir "%RELEASE%\PEDAGIO_PORTAL" >nul 2>&1
xcopy /E /I /Y "%DIST%\*" "%RELEASE%\PEDAGIO_PORTAL\" >nul
copy /Y "%ROOT%\INSTALAR_PEDAGIO.bat" "%RELEASE%\PEDAGIO_PORTAL\" >nul
copy /Y "%ROOT%\INICIAR_PEDAGIO.bat" "%RELEASE%\PEDAGIO_PORTAL\" >nul
copy /Y "%ROOT%\alerta_logistica.ico" "%RELEASE%\PEDAGIO_PORTAL\" >nul
copy /Y "%ROOT%\README_INSTALACAO.md" "%RELEASE%\PEDAGIO_PORTAL\" >nul
copy /Y "%ROOT%\.env.example" "%RELEASE%\PEDAGIO_PORTAL\" >nul
copy /Y "%ROOT%\requirements.txt" "%RELEASE%\PEDAGIO_PORTAL\" >nul
mkdir "%RELEASE%\PEDAGIO_PORTAL\data\temporary_uploads" >nul 2>&1
mkdir "%RELEASE%\PEDAGIO_PORTAL\logs" >nul 2>&1
mkdir "%RELEASE%\PEDAGIO_PORTAL\backups" >nul 2>&1
tar.exe -a -c -f "%PACKAGE%" -C "%RELEASE%" "PEDAGIO_PORTAL"
if errorlevel 1 (
  echo [ERRO] Falha ao criar o ZIP.
  popd
  exit /b 1
)
certutil -hashfile "%PACKAGE%" SHA256 > "%PACKAGE%.sha256"
echo [OK] Pacote criado: "%PACKAGE%"
popd
exit /b 0
