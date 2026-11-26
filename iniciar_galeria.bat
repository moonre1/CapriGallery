@echo off
setlocal
cd /d "%~dp0"

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Python 3.10 ou mais recente nao foi encontrado.
    echo Instale Python para Windows e marque a opcao de adicionar o Python ao PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Preparando o ambiente do aplicativo...
    py -3 -m venv .venv
    if errorlevel 1 goto :error
)

if not exist ".venv\deps.ready" (
    echo Instalando as bibliotecas da galeria. Isso pode levar alguns minutos na primeira vez.
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
    echo pronto>.venv\deps.ready
)

".venv\Scripts\python.exe" galeria_app.py
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo O aplicativo nao conseguiu iniciar. Confira a mensagem acima e tente novamente.
pause
exit /b 1
