@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1

rem ================================================================
rem PORTAL PEDAGIO - INSTALADOR PARA WINDOWS 10 E 11
rem Nao verifica a versao do Python.
rem ================================================================

set "REPO_ZIP=https://github.com/flaviocferreira-prog/pedagio_portal/archive/refs/heads/main.zip"
set "PASTA_PROJETO=Portal_Pedagio"
set "SENIOR_URL=https://platform.senior.com.br/login/?redirectTo=https%%3A%%2F%%2Fplatform.senior.com.br%%2Fsenior-x%%2F&tenant=gruposbf.com.br"

if /I "%~1"=="--iniciar" (
    set "DIRETORIO_INSTALACAO=%~dp0"
    goto INICIAR
)

if /I "%~1"=="--servidor" (
    set "DIRETORIO_INSTALACAO=%~dp0"
    goto SERVIDOR
)

if /I "%~1"=="--atualizar" (
    set "DIRETORIO_INSTALACAO=%~dp0"
    goto ATUALIZAR
)

goto INSTALAR


:INSTALAR
title Instalacao - Portal Pedagio
cls
echo ================================================================
echo                    PORTAL PEDAGIO
echo ================================================================
echo.
echo Preparando a instalacao. Aguarde...
echo.

for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('MyDocuments')"`) do set "DOCUMENTOS=%%D"
if not defined DOCUMENTOS set "DOCUMENTOS=%USERPROFILE%\Documents"
set "DIRETORIO_INSTALACAO=%DOCUMENTOS%\%PASTA_PROJETO%"
set "LOG_TEMP=%TEMP%\Portal_Pedagio_instalacao.log"

>"%LOG_TEMP%" echo [%date% %time%] Inicio da instalacao.
call :REGISTRAR "Diretorio de instalacao: %DIRETORIO_INSTALACAO%"

if exist "%DIRETORIO_INSTALACAO%\.portal_pedagio_instalado" (
    echo A ferramenta ja esta instalada.
    echo Reparando o atalho sem apagar dados...
    call :PREPARAR_ARQUIVOS_DE_ACESSO
    if errorlevel 1 goto ERRO_GERAL
    call :CRIAR_ATALHO
    if errorlevel 1 goto ERRO_GERAL
    echo.
    echo Instalacao ja existente. Nenhum dado foi apagado.
    echo Atalho verificado com sucesso.
    echo.
    pause
    exit /b 0
)

call :LOCALIZAR_PYTHON
if not defined PYTHON_CMD (
    call :REGISTRAR "ERRO: Python nao encontrado."
    echo [ERRO] Python nao foi encontrado neste computador.
    echo Instale o Python e execute este instalador novamente.
    echo Nenhuma verificacao de versao foi realizada.
    goto ERRO_GERAL
)

set "TEMP_INSTALACAO=%TEMP%\PortalPedagio_%RANDOM%_%RANDOM%"
set "ARQUIVO_ZIP=%TEMP_INSTALACAO%\portal_pedagio.zip"
set "PASTA_EXTRAIDA=%TEMP_INSTALACAO%\extraido"

md "%TEMP_INSTALACAO%" >nul 2>&1
md "%PASTA_EXTRAIDA%" >nul 2>&1

echo [1/6] Baixando os arquivos do sistema...
call :REGISTRAR "Baixando: %REPO_ZIP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; try { Invoke-WebRequest -UseBasicParsing -Uri $env:REPO_ZIP -OutFile $env:ARQUIVO_ZIP } catch { Write-Error $_; exit 1 }" >>"%LOG_TEMP%" 2>&1
if errorlevel 1 (
    call :REGISTRAR "ERRO: Nao foi possivel baixar o projeto."
    echo [ERRO] Nao foi possivel baixar a ferramenta.
    echo Verifique a conexao com a internet e tente novamente.
    goto LIMPAR_E_FALHAR
)

echo [2/6] Descompactando os arquivos...
call :REGISTRAR "Descompactando o projeto."
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; try { Expand-Archive -LiteralPath $env:ARQUIVO_ZIP -DestinationPath $env:PASTA_EXTRAIDA -Force } catch { Write-Error $_; exit 1 }" >>"%LOG_TEMP%" 2>&1
if errorlevel 1 (
    call :REGISTRAR "ERRO: Falha ao descompactar."
    echo [ERRO] O arquivo baixado nao pode ser descompactado.
    goto LIMPAR_E_FALHAR
)

set "ORIGEM_PROJETO="
for /d %%D in ("%PASTA_EXTRAIDA%\*") do if not defined ORIGEM_PROJETO set "ORIGEM_PROJETO=%%~fD"
if not defined ORIGEM_PROJETO (
    call :REGISTRAR "ERRO: Pasta principal do projeto nao encontrada."
    echo [ERRO] A estrutura do projeto baixado nao foi reconhecida.
    goto LIMPAR_E_FALHAR
)

echo [3/6] Copiando para a pasta Documentos...
md "%DIRETORIO_INSTALACAO%" >nul 2>&1
robocopy "%ORIGEM_PROJETO%" "%DIRETORIO_INSTALACAO%" /E /R:2 /W:2 /NFL /NDL /NJH /NJS /NP >>"%LOG_TEMP%" 2>&1
set "RESULTADO_COPIA=!ERRORLEVEL!"
if !RESULTADO_COPIA! GEQ 8 (
    call :REGISTRAR "ERRO: Robocopy retornou !RESULTADO_COPIA!."
    echo [ERRO] Nao foi possivel copiar os arquivos para Documentos.
    goto LIMPAR_E_FALHAR
)

echo [4/6] Criando o ambiente virtual...
call :LOCALIZAR_DIRETORIO_APP
if not defined DIRETORIO_APP (
    call :REGISTRAR "ERRO: requirements.txt nao encontrado na estrutura do projeto."
    echo [ERRO] O arquivo requirements.txt nao foi encontrado no projeto.
    goto LIMPAR_E_FALHAR
)
call :REGISTRAR "Pasta da aplicacao: %DIRETORIO_APP%"

if not exist "%DIRETORIO_APP%\.venv\Scripts\python.exe" (
    call :REGISTRAR "Criando o ambiente virtual."
    call %PYTHON_CMD% -m venv "%DIRETORIO_APP%\.venv" >>"%LOG_TEMP%" 2>&1
    if errorlevel 1 (
        call :REGISTRAR "ERRO: Falha ao criar o ambiente virtual."
        echo [ERRO] Nao foi possivel criar o ambiente virtual do Python.
        goto LIMPAR_E_FALHAR
    )
) else (
    call :REGISTRAR "Ambiente virtual existente foi preservado."
)

echo [5/6] Instalando as dependencias...
"%DIRETORIO_APP%\.venv\Scripts\python.exe" -m pip install -r "%DIRETORIO_APP%\requirements.txt" >>"%LOG_TEMP%" 2>&1
if errorlevel 1 (
    call :REGISTRAR "ERRO: Falha ao instalar requirements.txt."
    echo [ERRO] Nao foi possivel instalar as dependencias.
    echo Consulte o arquivo de log para identificar o motivo.
    goto LIMPAR_E_FALHAR
)

echo [6/6] Criando o atalho na Area de Trabalho...
call :PREPARAR_ARQUIVOS_DE_ACESSO
if errorlevel 1 goto LIMPAR_E_FALHAR
call :CRIAR_ATALHO
if errorlevel 1 goto LIMPAR_E_FALHAR

if not exist "%DIRETORIO_INSTALACAO%\logs" md "%DIRETORIO_INSTALACAO%\logs" >nul 2>&1
copy /Y "%LOG_TEMP%" "%DIRETORIO_INSTALACAO%\logs\instalacao.log" >nul 2>&1
>"%DIRETORIO_INSTALACAO%\.portal_pedagio_instalado" echo Instalado em %date% %time%

if exist "%TEMP_INSTALACAO%" rd /s /q "%TEMP_INSTALACAO%" >nul 2>&1

echo.
echo ================================================================
echo                 INSTALACAO CONCLUIDA
echo ================================================================
echo.
echo O atalho PORTAL PEDAGIO foi criado na Area de Trabalho.
echo Clique no atalho para abrir a ferramenta e o WMS Senior.
echo.
pause
exit /b 0


:ATUALIZAR
title Atualizacao - Portal Pedagio
cls
echo ================================================================
echo              ATUALIZACAO DO PORTAL PEDAGIO
echo ================================================================
echo.

if not exist "%DIRETORIO_INSTALACAO%\logs" md "%DIRETORIO_INSTALACAO%\logs" >nul 2>&1
set "LOG_TEMP=%DIRETORIO_INSTALACAO%\logs\atualizacao.log"
>"%LOG_TEMP%" echo [%date% %time%] Inicio da atualizacao.

if not exist "%DIRETORIO_INSTALACAO%\.portal_pedagio_instalado" (
    echo [ERRO] A instalacao do Portal Pedagio nao foi encontrada.
    echo Execute primeiro o instalar.bat.
    goto ERRO_GERAL
)

call :LOCALIZAR_PYTHON
if not defined PYTHON_CMD (
    call :REGISTRAR "ERRO: Python nao encontrado."
    echo [ERRO] Python nao foi encontrado neste computador.
    echo Nenhuma verificacao de versao foi realizada.
    goto ERRO_GERAL
)

set "TEMP_INSTALACAO=%TEMP%\PortalPedagio_Atualizacao_%RANDOM%_%RANDOM%"
set "ARQUIVO_ZIP=%TEMP_INSTALACAO%\portal_pedagio.zip"
set "PASTA_EXTRAIDA=%TEMP_INSTALACAO%\extraido"
md "%TEMP_INSTALACAO%" >nul 2>&1
md "%PASTA_EXTRAIDA%" >nul 2>&1

echo [1/5] Criando uma copia de seguranca dos dados...
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"`) do set "DATA_BACKUP=%%D"
set "PASTA_BACKUP=%DIRETORIO_INSTALACAO%\backups\antes_atualizacao_!DATA_BACKUP!"
md "!PASTA_BACKUP!" >nul 2>&1
robocopy "%DIRETORIO_INSTALACAO%" "!PASTA_BACKUP!" *.db *.sqlite *.sqlite3 .env /S /R:1 /W:1 /XD .venv backups logs /NFL /NDL /NJH /NJS /NP >>"%LOG_TEMP%" 2>&1
set "RESULTADO_BACKUP=!ERRORLEVEL!"
if !RESULTADO_BACKUP! GEQ 8 (
    call :REGISTRAR "ERRO: Nao foi possivel criar o backup."
    echo [ERRO] A copia de seguranca nao pode ser criada.
    echo A atualizacao foi cancelada e nenhum arquivo do sistema foi alterado.
    goto LIMPAR_E_FALHAR
)

echo [2/5] Baixando a versao mais recente...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; try { Invoke-WebRequest -UseBasicParsing -Uri $env:REPO_ZIP -OutFile $env:ARQUIVO_ZIP } catch { Write-Error $_; exit 1 }" >>"%LOG_TEMP%" 2>&1
if errorlevel 1 (
    call :REGISTRAR "ERRO: Falha no download da atualizacao."
    echo [ERRO] Nao foi possivel baixar a atualizacao.
    echo Verifique a conexao com a internet e tente novamente.
    goto LIMPAR_E_FALHAR
)

echo [3/5] Descompactando a atualizacao...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; try { Expand-Archive -LiteralPath $env:ARQUIVO_ZIP -DestinationPath $env:PASTA_EXTRAIDA -Force } catch { Write-Error $_; exit 1 }" >>"%LOG_TEMP%" 2>&1
if errorlevel 1 (
    call :REGISTRAR "ERRO: Falha ao descompactar a atualizacao."
    echo [ERRO] Nao foi possivel descompactar a atualizacao.
    goto LIMPAR_E_FALHAR
)

set "ORIGEM_PROJETO="
for /d %%D in ("%PASTA_EXTRAIDA%\*") do if not defined ORIGEM_PROJETO set "ORIGEM_PROJETO=%%~fD"
if not defined ORIGEM_PROJETO (
    call :REGISTRAR "ERRO: Estrutura da atualizacao nao reconhecida."
    echo [ERRO] A estrutura do projeto baixado nao foi reconhecida.
    goto LIMPAR_E_FALHAR
)

echo [4/5] Atualizando o sistema sem apagar os dados...
robocopy "%ORIGEM_PROJETO%" "%DIRETORIO_INSTALACAO%" /E /R:2 /W:2 /XF .env *.db *.sqlite *.sqlite3 portal_pedagio.bat iniciar.bat atualizar.bat servidor.bat alerta.ico /XD .venv data dados uploads logs backups /NFL /NDL /NJH /NJS /NP >>"%LOG_TEMP%" 2>&1
set "RESULTADO_COPIA=!ERRORLEVEL!"
if !RESULTADO_COPIA! GEQ 8 (
    call :REGISTRAR "ERRO: Robocopy retornou !RESULTADO_COPIA!."
    echo [ERRO] Nao foi possivel copiar todos os arquivos da atualizacao.
    echo Seus dados continuam preservados na pasta de backup.
    goto LIMPAR_E_FALHAR
)

echo [5/5] Atualizando as dependencias...
call :LOCALIZAR_DIRETORIO_APP
if not defined DIRETORIO_APP (
    call :REGISTRAR "ERRO: Pasta da aplicacao nao encontrada apos a atualizacao."
    echo [ERRO] O requirements.txt nao foi encontrado apos a atualizacao.
    goto LIMPAR_E_FALHAR
)
"%DIRETORIO_APP%\.venv\Scripts\python.exe" -m pip install -r "%DIRETORIO_APP%\requirements.txt" >>"%LOG_TEMP%" 2>&1
if errorlevel 1 (
    call :REGISTRAR "ERRO: Falha ao atualizar dependencias."
    echo [ERRO] Os arquivos foram atualizados, mas houve erro nas dependencias.
    echo Consulte: %LOG_TEMP%
    goto LIMPAR_E_FALHAR
)

call :PREPARAR_ARQUIVOS_DE_ACESSO
if errorlevel 1 goto LIMPAR_E_FALHAR
call :CRIAR_ATALHO
if errorlevel 1 goto LIMPAR_E_FALHAR

if exist "%TEMP_INSTALACAO%" rd /s /q "%TEMP_INSTALACAO%" >nul 2>&1

echo.
echo ================================================================
echo                 ATUALIZACAO CONCLUIDA
echo ================================================================
echo.
echo Banco de dados, arquivo .env e dados locais foram preservados.
echo Uma copia de seguranca foi criada antes da atualizacao.
echo.
pause
exit /b 0


:INICIAR
title Portal Pedagio
cd /d "%DIRETORIO_INSTALACAO%"
if not exist "%DIRETORIO_INSTALACAO%\logs" md "%DIRETORIO_INSTALACAO%\logs" >nul 2>&1
set "LOG_INICIO=%DIRETORIO_INSTALACAO%\logs\inicializacao.log"
set "PORTAL_URL="
call :LOCALIZAR_DIRETORIO_APP

call :LOCALIZAR_CHROME
if not defined CHROME_EXE (
    >>"%LOG_INICIO%" echo [%date% %time%] ERRO: Google Chrome nao encontrado.
    cls
    echo [ERRO] O Google Chrome nao foi encontrado.
    echo Instale o Google Chrome e tente novamente.
    echo.
    pause
    exit /b 1
)

call :DETECTAR_PORTAL
if defined PORTAL_URL goto ABRIR_NAVEGADOR

if not defined DIRETORIO_APP (
    >>"%LOG_INICIO%" echo [%date% %time%] ERRO: Pasta da aplicacao nao encontrada.
    cls
    echo [ERRO] A pasta principal da aplicacao nao foi encontrada.
    echo Execute novamente o instalar.bat.
    echo.
    pause
    exit /b 1
)

if not exist "%DIRETORIO_APP%\.venv\Scripts\python.exe" (
    >>"%LOG_INICIO%" echo [%date% %time%] ERRO: Ambiente virtual nao encontrado.
    cls
    echo [ERRO] A instalacao do sistema esta incompleta.
    echo Execute novamente o instalar.bat.
    echo.
    pause
    exit /b 1
)

if exist "%DIRETORIO_INSTALACAO%\.servidor_iniciando.lock" (
    echo O servidor ja esta iniciando. Aguarde...
    call :AGUARDAR_PORTAL 20
    if defined PORTAL_URL goto ABRIR_NAVEGADOR
    rd "%DIRETORIO_INSTALACAO%\.servidor_iniciando.lock" >nul 2>&1
)

md "%DIRETORIO_INSTALACAO%\.servidor_iniciando.lock" >nul 2>&1
echo Iniciando o Portal Pedagio. Aguarde...
start "PORTAL PEDAGIO - SERVIDOR" /min "%DIRETORIO_INSTALACAO%\servidor.bat"

call :AGUARDAR_PORTAL 60
if not defined PORTAL_URL (
    >>"%LOG_INICIO%" echo [%date% %time%] ERRO: Servidor nao respondeu nas portas esperadas.
    cls
    echo [ERRO] O servidor nao iniciou corretamente.
    echo Consulte o log em:
    echo %DIRETORIO_INSTALACAO%\logs\servidor.log
    echo.
    pause
    exit /b 1
)

:ABRIR_NAVEGADOR
>>"%LOG_INICIO%" echo [%date% %time%] Abrindo %PORTAL_URL% e WMS Senior.
start "" "%CHROME_EXE%" --incognito --new-window "%PORTAL_URL%" "%SENIOR_URL%"
exit /b 0


:SERVIDOR
title Portal Pedagio - Servidor
if not exist "%DIRETORIO_INSTALACAO%\logs" md "%DIRETORIO_INSTALACAO%\logs" >nul 2>&1
set "LOG_SERVIDOR=%DIRETORIO_INSTALACAO%\logs\servidor.log"
call :LOCALIZAR_DIRETORIO_APP
if not defined DIRETORIO_APP (
    >>"%LOG_SERVIDOR%" echo [%date% %time%] ERRO: Pasta da aplicacao nao encontrada.
    rd "%DIRETORIO_INSTALACAO%\.servidor_iniciando.lock" >nul 2>&1
    exit /b 1
)
cd /d "%DIRETORIO_APP%"
set "PYTHON_VENV=%DIRETORIO_APP%\.venv\Scripts\python.exe"

>>"%LOG_SERVIDOR%" echo.
>>"%LOG_SERVIDOR%" echo [%date% %time%] Iniciando servidor.

if not exist "%PYTHON_VENV%" (
    >>"%LOG_SERVIDOR%" echo [%date% %time%] ERRO: Python da .venv nao encontrado.
    rd "%DIRETORIO_INSTALACAO%\.servidor_iniciando.lock" >nul 2>&1
    exit /b 1
)

set "ENTRADA_ENCONTRADA="
if exist "%DIRETORIO_APP%\conferencia\web.py" (
    set "ENTRADA_ENCONTRADA=1"
    "%PYTHON_VENV%" -m conferencia.web >>"%LOG_SERVIDOR%" 2>&1
) else if exist "%DIRETORIO_APP%\app.py" (
    set "ENTRADA_ENCONTRADA=1"
    "%PYTHON_VENV%" "%DIRETORIO_APP%\app.py" >>"%LOG_SERVIDOR%" 2>&1
) else if exist "%DIRETORIO_APP%\run.py" (
    set "ENTRADA_ENCONTRADA=1"
    "%PYTHON_VENV%" "%DIRETORIO_APP%\run.py" >>"%LOG_SERVIDOR%" 2>&1
) else if exist "%DIRETORIO_APP%\main.py" (
    set "ENTRADA_ENCONTRADA=1"
    "%PYTHON_VENV%" "%DIRETORIO_APP%\main.py" >>"%LOG_SERVIDOR%" 2>&1
) else if exist "%DIRETORIO_APP%\manage.py" (
    set "ENTRADA_ENCONTRADA=1"
    "%PYTHON_VENV%" "%DIRETORIO_APP%\manage.py" runserver 127.0.0.1:8000 >>"%LOG_SERVIDOR%" 2>&1
)

if not defined ENTRADA_ENCONTRADA (
    >>"%LOG_SERVIDOR%" echo [%date% %time%] ERRO: Arquivo de inicializacao nao encontrado.
)

rd "%DIRETORIO_INSTALACAO%\.servidor_iniciando.lock" >nul 2>&1
exit /b 0


:LOCALIZAR_PYTHON
set "PYTHON_CMD="
where py.exe >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    where python.exe >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
exit /b 0


:LOCALIZAR_DIRETORIO_APP
set "DIRETORIO_APP="
if exist "%DIRETORIO_INSTALACAO%\requirements.txt" set "DIRETORIO_APP=%DIRETORIO_INSTALACAO%"
if not defined DIRETORIO_APP if exist "%DIRETORIO_INSTALACAO%\conferencia_volumetria\requirements.txt" set "DIRETORIO_APP=%DIRETORIO_INSTALACAO%\conferencia_volumetria"
if not defined DIRETORIO_APP (
    for /f "usebackq delims=" %%F in (`dir /b /s /a-d "%DIRETORIO_INSTALACAO%\requirements.txt" 2^>nul ^| findstr /V /I "\\.venv\\"`) do if not defined DIRETORIO_APP set "DIRETORIO_APP=%%~dpF"
)
if defined DIRETORIO_APP for %%D in ("%DIRETORIO_APP%\.") do set "DIRETORIO_APP=%%~fD"
exit /b 0


:LOCALIZAR_CHROME
set "CHROME_EXE="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE (
    for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| find /I "REG_SZ"') do set "CHROME_EXE=%%B"
)
if not defined CHROME_EXE (
    for /f "tokens=2,*" %%A in ('reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| find /I "REG_SZ"') do set "CHROME_EXE=%%B"
)
exit /b 0


:DETECTAR_PORTAL
set "PORTAL_URL="
for /f "usebackq delims=" %%U in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$urls=@('http://127.0.0.1:5000','http://127.0.0.1:8000','http://127.0.0.1:8080'); foreach($u in $urls){ try { $r=Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 2; if($r.StatusCode -ge 200 -and $r.StatusCode -lt 500){ Write-Output $u; break } } catch {} }"`) do set "PORTAL_URL=%%U"
exit /b 0


:AGUARDAR_PORTAL
set /a "TENTATIVAS=%~1"
:LOOP_AGUARDAR
call :DETECTAR_PORTAL
if defined PORTAL_URL exit /b 0
set /a "TENTATIVAS-=1"
if !TENTATIVAS! LEQ 0 exit /b 1
timeout /t 1 /nobreak >nul
goto LOOP_AGUARDAR


:PREPARAR_ARQUIVOS_DE_ACESSO
if not exist "%DIRETORIO_INSTALACAO%" exit /b 1
set "COPIAR_CONTROLADOR=1"
if /I "%~nx0"=="portal_pedagio.bat" if /I "%~dp0"=="%DIRETORIO_INSTALACAO%" set "COPIAR_CONTROLADOR=0"
if "%COPIAR_CONTROLADOR%"=="1" (
    copy /Y "%~f0" "%DIRETORIO_INSTALACAO%\portal_pedagio.bat" >nul 2>&1
    if errorlevel 1 exit /b 1
)

>"%DIRETORIO_INSTALACAO%\iniciar.bat" echo @echo off
>>"%DIRETORIO_INSTALACAO%\iniciar.bat" echo call "%DIRETORIO_INSTALACAO%\portal_pedagio.bat" --iniciar

>"%DIRETORIO_INSTALACAO%\atualizar.bat" echo @echo off
>>"%DIRETORIO_INSTALACAO%\atualizar.bat" echo call "%DIRETORIO_INSTALACAO%\portal_pedagio.bat" --atualizar

>"%DIRETORIO_INSTALACAO%\servidor.bat" echo @echo off
>>"%DIRETORIO_INSTALACAO%\servidor.bat" echo call "%DIRETORIO_INSTALACAO%\portal_pedagio.bat" --servidor

exit /b 0


:CRIAR_ATALHO
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "AREA_TRABALHO=%%D"
if not defined AREA_TRABALHO set "AREA_TRABALHO=%USERPROFILE%\Desktop"

set "PORTAL_ICON_PATH=%DIRETORIO_INSTALACAO%\alerta.ico"
set "PORTAL_SHORTCUT=%AREA_TRABALHO%\PORTAL PEDAGIO.lnk"
set "PORTAL_TARGET=%DIRETORIO_INSTALACAO%\iniciar.bat"
set "PORTAL_WORKDIR=%DIRETORIO_INSTALACAO%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; try { Add-Type -AssemblyName System.Drawing; $f=[IO.File]::Create($env:PORTAL_ICON_PATH); [System.Drawing.SystemIcons]::Warning.Save($f); $f.Dispose(); $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($env:PORTAL_SHORTCUT); $s.TargetPath=$env:PORTAL_TARGET; $s.WorkingDirectory=$env:PORTAL_WORKDIR; $s.IconLocation=$env:PORTAL_ICON_PATH; $s.Description='Abrir Portal Pedagio e WMS Senior em modo anonimo'; $s.Save() } catch { Write-Error $_; exit 1 }" >>"%LOG_TEMP%" 2>&1
if errorlevel 1 (
    call :REGISTRAR "ERRO: Nao foi possivel criar o atalho."
    echo [ERRO] Nao foi possivel criar o atalho na Area de Trabalho.
    exit /b 1
)
exit /b 0


:REGISTRAR
if defined LOG_TEMP >>"%LOG_TEMP%" echo [%date% %time%] %~1
exit /b 0


:LIMPAR_E_FALHAR
if defined TEMP_INSTALACAO if exist "%TEMP_INSTALACAO%" rd /s /q "%TEMP_INSTALACAO%" >nul 2>&1

:ERRO_GERAL
echo.
echo A operacao nao foi concluida.
echo Log tecnico:
echo %LOG_TEMP%
echo.
pause
exit /b 1
