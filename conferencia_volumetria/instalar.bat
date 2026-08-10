@echo off
setlocal EnableExtensions EnableDelayedExpansion

if /I "%~1"=="--setup" goto :setup

set "APP_NAME=CONFERENCIA VOLUMETRIA"
set "ARCHIVE_URL=https://github.com/flaviocferreira-prog/pedagio_portal/archive/refs/heads/main.zip"
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('MyDocuments')"`) do set "DOCUMENTS_DIR=%%D"
set "APP_DIR=%DOCUMENTS_DIR%\Conferencia Volumetria"
set "TEMP_DIR=%TEMP%\cv-installer-%RANDOM%%RANDOM%"

echo Baixando a versao atual de %APP_NAME%...
mkdir "%TEMP_DIR%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '%ARCHIVE_URL%' -OutFile '%TEMP_DIR%\project.zip'"
if errorlevel 1 goto :bootstrap_error

echo Preparando os arquivos...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%TEMP_DIR%\project.zip' -DestinationPath '%TEMP_DIR%\source' -Force"
if errorlevel 1 goto :bootstrap_error
set "SOURCE_DIR=%TEMP_DIR%\source\pedagio_portal-main\conferencia_volumetria"
if not exist "%SOURCE_DIR%\instalar.bat" goto :bootstrap_error
if not exist "%APP_DIR%" mkdir "%APP_DIR%"

rem A atualizacao preserva banco SQLite, logs e backups locais.
robocopy "%SOURCE_DIR%" "%APP_DIR%" /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS
set "COPY_RESULT=!ERRORLEVEL!"
if !COPY_RESULT! GEQ 8 goto :bootstrap_error

call "%APP_DIR%\instalar.bat" --setup
set "SETUP_RESULT=!ERRORLEVEL!"
rmdir /S /Q "%TEMP_DIR%" >nul 2>&1
exit /b !SETUP_RESULT!

:bootstrap_error
echo.
echo Nao foi possivel preparar a instalacao. Verifique a conexao com a internet e tente novamente.
rmdir /S /Q "%TEMP_DIR%" >nul 2>&1
pause
exit /b 1

:setup
cd /d "%~dp0"
echo Preparando %APP_NAME%...
call :locate_python
if not defined PYTHON_EXE call :install_python
if not defined PYTHON_EXE call :locate_python
if not defined PYTHON_EXE goto :python_error

if not exist ".venv\Scripts\python.exe" "%PYTHON_EXE%" -m venv .venv
if errorlevel 1 goto :setup_error
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :setup_error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :setup_error

if not exist ".env" copy /Y ".env.example" ".env" >nul
findstr /B /C:"APPS_SCRIPT_URL=" ".env" >nul 2>&1 || echo APPS_SCRIPT_URL=>> ".env"
findstr /B /C:"SYNC_SHARED_SECRET=" ".env" >nul 2>&1 || echo SYNC_SHARED_SECRET=>> ".env"
for %%D in ("data" "data\temporary_uploads" "logs" "backups") do if not exist %%~D mkdir %%~D
if not exist "logs\.gitkeep" type nul > "logs\.gitkeep"
if not exist "backups\.gitkeep" type nul > "backups\.gitkeep"

".venv\Scripts\python.exe" -c "from conferencia.infrastructure.database import SQLiteDatabase; SQLiteDatabase().initialize()"
if errorlevel 1 goto :setup_error
call :create_shortcut

echo.
echo Instalacao concluida. Use o icone CONFERENCIA VOLUMETRIA na Area de Trabalho.
findstr /B /R /C:"APPS_SCRIPT_URL= *$" /C:"SYNC_SHARED_SECRET= *$" ".env" >nul 2>&1 && echo Sincronizacao Google pendente de configuracao. Consulte README.md.
pause
exit /b 0

:locate_python
set "PYTHON_EXE="
rem A comparacao abaixo usa ^> escapado pelo cmd: sys.version_info >= (3, 11)
rem Em uma falha, a mensagem operacional segue o formato: Python %PYTHON_VERSION% encontrado
for /f "usebackq delims=" %%P in (`py -3 -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3,11) else 1); print(sys.executable)" 2^>nul`) do set "PYTHON_EXE=%%P"
if defined PYTHON_EXE exit /b 0
for %%P in ("%LocalAppData%\Programs\Python\Python311\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python311\python.exe") do (
  if exist "%%~P" (
    "%%~P" -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=%%~P"
  )
)
exit /b 0

:install_python
echo Python 3.11 nao foi encontrado. Instalando automaticamente...
set "PYTHON_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile '%PYTHON_INSTALLER%'"
if errorlevel 1 exit /b 1
start "" /wait "%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1
del /Q "%PYTHON_INSTALLER%" >nul 2>&1
call :locate_python
exit /b 0

:create_shortcut
set "CV_SHORTCUT_TARGET=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$target=$env:CV_SHORTCUT_TARGET; $desktop=[Environment]::GetFolderPath('Desktop'); $shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'CONFERENCIA VOLUMETRIA.lnk')); $shortcut.TargetPath=(Join-Path $target 'iniciar.bat'); $shortcut.WorkingDirectory=$target; $icon=(Join-Path $target 'static\alert.ico'); if(-not (Test-Path -LiteralPath $icon)){$icon=(Join-Path $env:SystemRoot 'System32\imageres.dll') + ',78'}; $shortcut.IconLocation=$icon; $shortcut.Description='Conferencia Volumetria'; $shortcut.Save()" >nul
exit /b 0

:python_error
echo.
echo Nao foi possivel instalar ou localizar Python 3.11 ou superior.
pause
exit /b 1

:setup_error
echo.
echo A instalacao nao foi concluida. Nenhum dado local foi removido.
pause
exit /b 1
