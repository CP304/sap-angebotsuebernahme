@echo off
rem ---------------------------------------------------------------------
rem  SAP-Angebotsuebernahme starten
rem
rem  Beim ersten Start werden fehlende Abhaengigkeiten installiert.
rem  Die Anwendung startet standardmaessig im Testsystem (Mock-SAP) und im
rem  Dry Run -- es wird also nichts in SAP geschrieben.
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

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo.
    echo   Erststart: benoetigte Pakete werden installiert ...
    echo.
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   Die Installation ist fehlgeschlagen.
        echo.
        pause
        exit /b 1
    )
)

python -m app.main %*
if errorlevel 1 (
    echo.
    echo   Die Anwendung wurde mit einem Fehler beendet.
    echo   Einzelheiten stehen in der Logdatei unter:
    echo   %%APPDATA%%\SAP-Angebotsuebernahme\logs
    echo.
    pause
)
endlocal
