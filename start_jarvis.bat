@echo off
rem Jarvis inditasa dupla kattintassal (a .venv kornyezetbol)
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Nincs telepitve a virtualis kornyezet. Lasd a README.md telepitesi lepeseit.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" jarvis.py %*
rem Ha a Jarvis mar fut (pl. keszenletben), csak megnyitja az ablakat: akkor nem kell varni.
if errorlevel 1 pause
