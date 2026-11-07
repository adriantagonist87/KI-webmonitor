@echo off
if "%~1"=="" (
    echo Aufruf: open-webmonitor.cmd benutzer@host
    exit /b 2
)
title KI-Webmonitor - sicherer Zugang
echo Dieses Fenster waehrend der Benutzung geoeffnet lassen.
echo Nach der SSH-Anmeldung oeffnen: http://localhost:8765
echo Browser: http://localhost:8766/vnc.html?autoconnect=true
echo Testpostfach: http://localhost:8767
echo Portainer: http://localhost:8768
echo.
ssh -N -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=%~dp0known_hosts" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:8765:127.0.0.1:8765 -L 127.0.0.1:8766:127.0.0.1:8766 -L 127.0.0.1:8767:127.0.0.1:8767 -L 127.0.0.1:8768:127.0.0.1:9000 "%~1"
pause
