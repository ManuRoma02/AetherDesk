@echo off
setlocal
echo ========================================================
echo   Compilazione AetherDesk.exe Standalone (PyInstaller)
echo ========================================================

cd /d "%~dp0\.."

echo Pulizia directory build precedenti...
if exist "build" rmdir /s /q "build"

echo Avvio compilazione con PyInstaller (Icona: assets\icon.ico)...
pyinstaller --clean --workpath "%TEMP%\aether_build" --distpath "%TEMP%\aether_dist" "RemoteClient.spec"

if %errorLevel% equ 0 (
    if not exist "dist" mkdir "dist"
    copy /y "%TEMP%\aether_dist\AetherDesk.exe" "dist\AetherDesk.exe" >nul 2>&1
    copy /y "%TEMP%\aether_dist\AetherDesk.exe" "dist\RemoteClient.exe" >nul 2>&1
    copy /y "%TEMP%\aether_dist\AetherDesk.exe" "%USERPROFILE%\Downloads\AetherDesk.exe" >nul 2>&1
    echo.
    echo ========================================================
    echo [OK] Compilazione completata con successo!
    echo L'eseguibile portatile e' pronto in: dist\AetherDesk.exe
    echo Copiato automaticamente in: %USERPROFILE%\Downloads\AetherDesk.exe
    echo ========================================================
) else (
    echo.
    echo [ERRORE] Compilazione fallita.
)

pause
