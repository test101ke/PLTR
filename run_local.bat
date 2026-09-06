@echo off
REM PLTR Signal Desk - local launcher (Windows). Open http://localhost:8000
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8000
python -m pip install -q -r requirements.txt
echo.
echo ==================================================
echo   PLTR Signal Desk starting...
echo   On this PC:       http://localhost:%PORT%
echo   On your network:  http://YOUR-PC-IP:%PORT%
echo   Stop with Ctrl+C
echo ==================================================
echo.
python -m uvicorn main:app --host 0.0.0.0 --port %PORT%
