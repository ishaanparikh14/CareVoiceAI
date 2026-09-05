@echo off
REM Opens Windows Firewall TCP port 8000 for the CareVoice server.
REM Run as Administrator.
echo Opening Windows Firewall for CareVoice Server (TCP 8000)...
netsh advfirewall firewall delete rule name="CareVoice Server" >nul 2>&1
netsh advfirewall firewall add rule name="CareVoice Server" dir=in action=allow protocol=TCP localport=8000
if %ERRORLEVEL% EQU 0 (
    echo SUCCESS: Firewall rule added. Your phone can now reach the server on port 8000.
    echo Find this PC's LAN IP with 'ipconfig' ^(look for the Wi-Fi IPv4 address^).
) else (
    echo ERROR: Failed to add firewall rule. Run this file as Administrator.
)
pause
