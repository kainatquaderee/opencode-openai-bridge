@echo off
rem ============================================================================
rem opencode-openai-bridge v0.7.0 - Windows launcher
rem ============================================================================
rem Usage:
rem   start.bat              full auto: install deps if needed, verify, run
rem   start.bat --check      verify + smoke test only, do not start the server
rem   start.bat --reinstall  wipe .venv and reinstall before verify
rem   start.bat --help       show this help
rem ============================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VERSION=0.7.0"
set "MODE=run"
set "REINSTALL=0"

for %%A in (%*) do (
    if /i "%%~A"=="--check"     set "MODE=check"
    if /i "%%~A"=="--reinstall" set "REINSTALL=1"
    if /i "%%~A"=="--help"      goto :usage
    if /i "%%~A"=="-h"          goto :usage
)

echo ==^> start.bat v%VERSION%
echo ==^> mode: %MODE%

if "%REINSTALL%"=="1" (
    if exist .venv rmdir /s /q .venv
    echo ==^> removed .venv ^(reinstall^)
)

rem --- Optional .env loading (shell env wins over .env) ---
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "KEY=%%A"
        set "VAL=%%B"
        if not "!KEY!"=="" if not "!KEY:~0,1!"=="#" (
            set "KEY=!KEY: =!"
            set "VAL=!VAL:"=!"
            set "VAL=!VAL:'=!"
            if not defined !KEY! set "!KEY!=!VAL!"
        )
    )
    echo ==^> loaded .env
) else (
    echo ==^> no .env file ^(using environment defaults^)
)

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python not found on PATH. Install Python 3.9+.
    exit /b 1
)
echo ==^> python:
python --version

if not exist .venv (
    echo ==^> creating .venv
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

call .venv\Scripts\activate.bat

rem --- Dependencies: reinstall only when requirements.txt changes ---
python -c "import hashlib,os,sys;h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest();p=os.path.join('.venv','.deps-hash');c=open(p).read() if os.path.exists(p) else '';sys.exit(0 if h==c else 1)"
if errorlevel 1 (
    echo ==^> installing deps ^(hash changed^)
    python -m pip install -q --upgrade pip
    if errorlevel 1 exit /b 1
    python -m pip install -q -r requirements.txt
    if errorlevel 1 exit /b 1
    python -c "import hashlib,os;h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest();open(os.path.join('.venv','.deps-hash'),'w').write(h)"
) else (
    echo ==^> deps up to date
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

rem --- Bind host/port (BRIDGE_* only; never inherit generic PORT) ---
if "%BRIDGE_HOST%"=="" set "BRIDGE_HOST=0.0.0.0"
if "%BRIDGE_PORT%"=="" set "BRIDGE_PORT=4097"

if not "%PORT%"=="" if not "%PORT%"=="%BRIDGE_PORT%" (
    echo NOTE: shell has PORT=%PORT% ^(ignored^). Using BRIDGE_PORT=%BRIDGE_PORT%.
)

rem --- Optional: verify opencode binary ---
if "%BRIDGE_OPENCODE_BIN%"=="" set "BRIDGE_OPENCODE_BIN=opencode"
where "%BRIDGE_OPENCODE_BIN%" >nul 2>nul
if errorlevel 1 (
    echo WARN: opencode not on PATH. Bridge will start but /v1/chat/completions returns 503.
) else (
    echo ==^> opencode: %BRIDGE_OPENCODE_BIN%
)

echo.
echo ==^> starting server on %BRIDGE_HOST%:%BRIDGE_PORT%
echo     OmniRoute base URL: http://localhost:%BRIDGE_PORT%/v1
echo     Quick check:        curl -s http://localhost:%BRIDGE_PORT%/health
echo.
python -m uvicorn server:app --host %BRIDGE_HOST% --port %BRIDGE_PORT%
exit /b %errorlevel%

:usage
echo opencode-openai-bridge v%VERSION% - Windows launcher
echo.
echo Usage:
echo   start.bat              full auto: install deps if needed, verify, run
echo   start.bat --check      verify + smoke test only, do not start the server
echo   start.bat --reinstall  wipe .venv and reinstall before verify
echo   start.bat --help       show this help
echo.
echo Environment:
echo   BRIDGE_HOST            bind host (default 0.0.0.0)
echo   BRIDGE_PORT            bind port (default 4097)
echo   BRIDGE_OPENCODE_BIN    opencode CLI path (default "opencode")
echo.
echo This script never reads the generic PORT variable.
exit /b 0
