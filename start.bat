@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set MODE=run
if "%~1"=="--check" set MODE=check
if "%~1"=="--reinstall" (
    if exist .venv rmdir /s /q .venv
)

if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "KEY=%%A"
        set "VAL=%%B"
        if not "!KEY!"=="" if not "!KEY:~0,1!"=="#" (
            set "!KEY!=!VAL!"
        )
    )
    echo ==^> loaded .env
)

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python not found on PATH.
    exit /b 1
)

if not exist .venv (
    echo ==^> creating .venv
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

call .venv\Scripts\activate.bat

python -c "import hashlib,sys,os; h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest(); p='.venv/.deps-hash'; c=open(p).read() if os.path.exists(p) else ''; sys.exit(0 if h==c else 1)"
if errorlevel 1 (
    echo ==^> installing deps
    python -m pip install -q --upgrade pip
    python -m pip install -q -r requirements.txt
    python -c "import hashlib,open; h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest(); open('.venv/.deps-hash','w').write(h)"
)

echo ==^> verifying files
python verify_files.py
if errorlevel 1 exit /b 1

echo ==^> running smoke test
python test_imports.py
if errorlevel 1 exit /b 1

if "%MODE%"=="check" (
    echo.
    echo ==^> check only; not starting server
    exit /b 0
)

if "%BRIDGE_HOST%"=="" set BRIDGE_HOST=0.0.0.0
if "%BRIDGE_PORT%"=="" set BRIDGE_PORT=4097

if not "%PORT%"=="" if not "%PORT%"=="%BRIDGE_PORT%" (
    echo NOTE: shell has PORT=%PORT% ^(ignored^). Using BRIDGE_PORT=%BRIDGE_PORT%.
)

echo.
echo ==^> starting server on %BRIDGE_HOST%:%BRIDGE_PORT%
uvicorn server:app --host %BRIDGE_HOST% --port %BRIDGE_PORT%
