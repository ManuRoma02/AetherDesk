@echo off
setlocal
echo ========================================================
echo   Compilazione AetherDesk.exe Standalone (PyInstaller)
echo ========================================================

cd /d "%~dp0\.."

echo Pulizia directory build precedenti...
if exist "build" rmdir /s /q "build"

echo Avvio compilazione con PyInstaller (Icona: assets\icon.ico)...
pyinstaller --clean "RemoteClient.spec"

if %errorLevel% equ 0 (
    echo.
    echo Creazione alias di retrocompatibilita' dist\RemoteClient.exe...
    copy /y "dist\AetherDesk.exe" "dist\RemoteClient.exe" >nul 2>&1
    echo ========================================================
    echo [OK] Compilazione completata con successo!
    echo L'eseguibile portatile e' pronto in: dist\AetherDesk.exe
    echo ========================================================
) else (
    echo.
    echo [ERRORE] Compilazione fallita.
)

pause
