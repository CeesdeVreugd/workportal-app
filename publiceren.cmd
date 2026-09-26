@echo off
REM WorkPortal - publiceren
REM 1) Zorgt eenmalig dat de vaste projectmap een git-kopie van GitHub is
REM 2) Kopieert de bestanden uit deze map naar de vaste projectmap (en
REM    verwijdert daar verouderde bestanden die hier niet meer bestaan)
REM 3) Commit en pusht de wijziging naar GitHub
REM
REM De VM (Portainer) haalt de wijziging van GitHub op. De container regelt
REM bij het opstarten zelf de database-updates (zonder dataverlies).

setlocal

set "DOEL=C:\1 Automatiserings projecten\DeVreugdProductietechniek\WorkPortal-App\WorkPortal"
set "REPO=https://github.com/CeesdeVreugd/workportal-app.git"
set "OMSCHRIJVING=WorkPortal V22: mailtekst zwart, werkvoorbereiding@ in handtekening"

echo.
echo ============================================
echo  Stap 1/3: projectmap controleren
echo ============================================
echo Doel: %DOEL%
if not exist "%DOEL%" mkdir "%DOEL%"
if not exist "%DOEL%\.git" (
  echo Nog geen git-repository in de projectmap: eerste keer ophalen van GitHub...
  git clone "%REPO%" "%DOEL%"
  if errorlevel 1 (
    echo.
    echo FOUT: Ophalen van %REPO% is mislukt.
    echo Controleer of de map "%DOEL%" leeg is en of je bent ingelogd bij GitHub.
    pause
    exit /b 1
  )
)

echo.
echo ============================================
echo  Stap 2/3: bestanden bijwerken
echo ============================================
REM /MIR zorgt dat verouderde/verwijderde bestanden ook in de doelmap
REM verdwijnen. .git, data en .env blijven met rust.
robocopy "%~dp0." "%DOEL%" /MIR /XD .git data __pycache__ /XF .env .env.local *.pyc /NFL /NDL /NJH /NJS

if %errorlevel% GEQ 8 (
  echo.
  echo FOUT: Er ging iets mis bij het bijwerken van de bestanden.
  pause
  exit /b 1
)

cd /d "%DOEL%"

echo.
echo ============================================
echo  Stap 3/3: publiceren naar GitHub
echo ============================================
git add .
git commit -m "%OMSCHRIJVING%"
git branch -M main
git push -u origin main
if errorlevel 1 (
  echo.
  echo FOUT: Pushen naar GitHub is mislukt. Zie de melding hierboven.
  pause
  exit /b 1
)

echo.
echo ============================================
echo  Klaar!
echo ============================================
echo De VM (via Portainer) haalt deze wijziging nu zelf op.
echo Ga naar de "workportal"-stack in Portainer en klik op
echo "Pull and redeploy" (met "Re-pull image and redeploy" aan) om 'm
echo daadwerkelijk uit te rollen.
pause
