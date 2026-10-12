@echo off
setlocal enableextensions
cd /d "%~dp0"

echo [GROK6] Working directory: %CD%

if not exist ".env" (
  if exist ".env.example" (
    echo [GROK6] Creating local .env from .env.example...
    copy /Y ".env.example" ".env" >nul || (echo [GROK6] Could not create .env & exit /b 1)
    echo [GROK6] .env created with blank API-key fields.
    echo [GROK6] Add your API keys in Notepad, save, and close Notepad to continue.
    start "" /wait notepad ".env"
  ) else (
    echo [GROK6] ERROR: .env and .env.example are both missing.
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [GROK6] Creating virtual environment...
  python -m venv .venv || (echo [GROK6] venv creation failed & exit /b 1)
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul
echo [GROK6] Installing dependencies...
python -m pip install -r requirements.txt || (echo [GROK6] pip install failed & exit /b 1)

echo [GROK6] Checking API configuration (secret values are never printed)...
python env_check.py || (echo [GROK6] Environment check failed & exit /b 1)

echo [GROK6] Running smoke check...
python -m solana_app --smoke --provider auto
if errorlevel 1 (
  echo [GROK6] Smoke check failed. See traceback above.
  exit /b 1
)

echo [GROK6] Running tests...
python -m pytest -q
if errorlevel 1 (
  echo [GROK6] Tests failed. Aborting server start.
  exit /b 1
)

echo [GROK6] Starting server on http://127.0.0.1:8080
start "" http://127.0.0.1:8080
python -m solana_app
endlocal
