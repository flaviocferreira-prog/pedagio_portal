@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Instalador PEDAGIO PORTAL

set "ROOT=%~dp0"
pushd "%ROOT%" >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Nao foi possivel acessar a pasta do projeto.
  exit /b 1
)
set "ROOT=%CD%"
set "VENV=%ROOT%\.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "REQUIREMENTS=%ROOT%\requirements.txt"
set "LOG_DIR=%ROOT%\logs"
set "LOG_FILE=%LOG_DIR%\instalacao_pedagio.log"
set "REPAIR=0"
set "PORTABLE_EXE=%ROOT%\PEDAGIO_PORTAL.exe"

if /I "%~1"=="--repair" set "REPAIR=1"
if not "%~1"=="" if /I not "%~1"=="--repair" (
  echo Uso: INSTALAR_PEDAGIO.bat [--repair]
  popd
  exit /b 2
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
call :log "Inicio do instalador."

if exist "%PORTABLE_EXE%" goto :portable

if not exist "%ROOT%\app.py" (
  call :fail "app.py nao foi encontrado. Execute o instalador na pasta do projeto."
  goto :end
)
if not exist "%REQUIREMENTS%" (
  call :fail "requirements.txt nao foi encontrado."
  goto :end
)

if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 (
    set "PY=%VENV_PY%"
    call :log "Ambiente virtual existente encontrado."
    goto :ensure_pip
  )
)

if exist "%VENV%" (
  if "%REPAIR%"=="1" goto :backup_venv
  call :fail "A .venv existente nao esta funcional. Execute INSTALAR_PEDAGIO.bat --repair para preserva-la em backup e recria-la."
  goto :end
)

:create_venv
call :select_python
if not defined PYTHON_CMD (
  call :fail "Python 3.11 ou superior nao foi encontrado. Solicite a instalacao por um meio autorizado pela TI e execute este instalador novamente."
  goto :end
)
call :log "Criando ambiente virtual com %PYTHON_CMD%."
call %PYTHON_CMD% -m venv "%VENV%"
if errorlevel 1 (
  call :fail "Falha ao criar a .venv. Verifique se o Python inclui o modulo venv e se voce tem permissao de escrita na pasta do projeto."
  goto :end
)
if not exist "%VENV_PY%" (
  call :fail "A criacao da .venv terminou sem o executavel Python esperado."
  goto :end
)
set "PY=%VENV_PY%"
goto :ensure_pip

:portable
if not exist "%ROOT%\_internal" (
  call :fail "O diretorio _internal do pacote portatil nao foi encontrado. Extraia o ZIP completo antes de executar o instalador."
  goto :end
)
if not exist "%ROOT%\alerta_logistica.ico" (
  call :fail "O icone alerta_logistica.ico nao foi encontrado no pacote portatil."
  goto :end
)
for %%D in ("%ROOT%\data" "%ROOT%\data\temporary_uploads" "%ROOT%\logs" "%ROOT%\backups") do if not exist "%%~D" mkdir "%%~D" >nul 2>&1
if not exist "%ROOT%\.env" if exist "%ROOT%\.env.example" (
  copy /Y "%ROOT%\.env.example" "%ROOT%\.env" >nul
  if errorlevel 1 (
    call :fail "Nao foi possivel criar .env a partir de .env.example."
    goto :end
  )
  call :log "Arquivo .env criado a partir do modelo, sem credenciais."
)
call :log "Pacote portatil validado; nenhuma biblioteca sera instalada."
goto :shortcut

:backup_venv
set "VENV_BACKUP=%ROOT%\.venv.backup"
set /a BACKUP_INDEX=0
:backup_name
if not exist "%VENV_BACKUP%" goto :move_venv
set /a BACKUP_INDEX+=1
set "VENV_BACKUP=%ROOT%\.venv.backup.%BACKUP_INDEX%"
goto :backup_name

:move_venv
call :log "Modo de reparo: preservando a .venv anterior em %VENV_BACKUP%."
move "%VENV%" "%VENV_BACKUP%" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :fail "Nao foi possivel preservar a .venv anterior. Feche processos que possam estar usando-a e tente novamente."
  goto :end
)
goto :create_venv

:ensure_pip
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
  call :log "pip ausente na .venv; tentando habilita-lo pelo ensurepip local."
  "%PY%" -m ensurepip --upgrade
  if errorlevel 1 (
    call :fail "pip nao esta disponivel nesta .venv. Repare o Python por um meio autorizado pela TI."
    goto :end
  )
)

call :log "Verificando dependencias declaradas."
"%PY%" -m pip install --disable-pip-version-check --requirement "%REQUIREMENTS%"
if errorlevel 1 (
  call :fail "A instalacao de dependencias falhou. Verifique rede, proxy, certificados e politicas corporativas. Nenhuma configuracao de seguranca foi alterada."
  goto :end
)
"%PY%" -m pip check
if errorlevel 1 (
  call :fail "pip detectou dependencias incompativeis na .venv. Execute novamente com --repair se o problema persistir."
  goto :end
)
"%PY%" -c "import openpyxl, tzdata, dotenv; from zoneinfo import ZoneInfo; ZoneInfo('America/Sao_Paulo'); print('Dependencias validadas.')"
if errorlevel 1 (
  call :fail "Os imports obrigatorios nao foram validados. Execute novamente com --repair se necessario."
  goto :end
)

if not exist "%ROOT%\.env" if exist "%ROOT%\.env.example" (
  copy /Y "%ROOT%\.env.example" "%ROOT%\.env" >nul
  if errorlevel 1 (
    call :fail "Nao foi possivel criar .env a partir de .env.example."
    goto :end
  )
  call :log "Arquivo .env criado a partir do modelo, sem credenciais."
)
for %%D in ("%ROOT%\data" "%ROOT%\data\temporary_uploads" "%ROOT%\logs" "%ROOT%\backups") do if not exist "%%~D" mkdir "%%~D" >nul 2>&1

:shortcut
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $desktop=[Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory); if([string]::IsNullOrWhiteSpace($desktop)){throw 'Area de Trabalho indisponivel.'}; $launcher=Join-Path $env:ROOT 'INICIAR_PEDAGIO.bat'; $icon=Join-Path $env:ROOT 'alerta_logistica.ico'; if(!(Test-Path -LiteralPath $launcher) -or !(Test-Path -LiteralPath $icon)){throw 'Arquivos do atalho nao encontrados.'}; $shell=New-Object -ComObject WScript.Shell; $name='Alerta Log' + [char]0x00ED + 'stica.lnk'; $shortcut=$shell.CreateShortcut((Join-Path $desktop $name)); $shortcut.TargetPath=$launcher; $shortcut.Arguments=''; $shortcut.WorkingDirectory=$env:ROOT; $shortcut.IconLocation=$icon; $shortcut.Description='Iniciar PEDAGIO PORTAL'; $shortcut.Save()" >nul 2>&1
if errorlevel 1 (
  call :log "Instalacao concluida, mas o atalho nao foi criado. Consulte a mensagem exibida."
  echo.
  echo [AVISO] A instalacao foi concluida, mas nao foi possivel criar o atalho.
  echo Execute INICIAR_PEDAGIO.bat diretamente ou solicite suporte da TI.
) else (
  call :log "Atalho criado ou atualizado na Area de Trabalho do usuario."
  echo.
  echo [OK] Instalacao concluida. Use o atalho "Alerta Logistica" na Area de Trabalho.
)
call :log "Instalacao concluida com sucesso."
set "RESULT=0"
goto :end

:select_python
set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 (
  for %%V in (3.13 3.12 3.11 3.14) do (
    py -%%V -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_CMD=py -%%V"
      goto :eof
    )
  )
)
where python >nul 2>&1
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=python"
)
exit /b 0

:log
echo [%DATE% %TIME%] %~1
>> "%LOG_FILE%" echo [%DATE% %TIME%] %~1
exit /b 0

:fail
call :log "ERRO: %~1"
echo.
echo [ERRO] %~1
set "RESULT=1"
exit /b 0

:end
if not defined RESULT set "RESULT=1"
echo.
echo Log de instalacao: "%LOG_FILE%"
if not "%RESULT%"=="0" pause
popd
exit /b %RESULT%
