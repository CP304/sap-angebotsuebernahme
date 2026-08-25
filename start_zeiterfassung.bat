@echo off
rem ---------------------------------------------------------------------
rem  Zeiterfassung starten
rem
rem  Beim ersten Start werden fehlende Pakete installiert und der Autostart
rem  eingerichtet.  Danach laeuft das Werkzeug nach jedem Anmelden von
rem  selbst -- dieses Skript braucht man dann nicht mehr anzuklicken.
rem ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Python wurde nicht gefunden.
    echo   Bitte Python 3.12 oder neuer installieren: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

python -c "import PySide6, openpyxl" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Erststart: benoetigte Pakete werden installiert ...
    echo.
    python -m pip install --upgrade pip
    python -m pip install PySide6 openpyxl
    if errorlevel 1 (
        echo.
        echo   Die Installation ist fehlgeschlagen.
        echo.
        pause
        exit /b 1
    )
)

start "" pythonw -m zeiterfassung.main %*
endlocal
