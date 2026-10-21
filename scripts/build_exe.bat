@echo off
setlocal
echo ========================================================
echo   Compilazione RemoteClient.exe Standalone (PyInstaller)
echo ========================================================

cd /d "%~dp0\.."

echo Pulizia directory build e dist precedenti...
if exist "build" rmdir /s /q "build"
if exist "dist\RemoteClient.exe" del /f /q "dist\RemoteClient.exe"

echo Avvio compilazione con PyInstaller...
pyinstaller --onefile --noconsole --name "RemoteClient" ^
    --add-data "src;src" ^
    --hidden-import "PIL" ^
    --hidden-import "PIL.Image" ^
    --hidden-import "PIL.ImageTk" ^
    --hidden-import "cv2" ^
    --hidden-import "cryptography" ^
    --hidden-import "websockets" ^
    --hidden-import "win32clipboard" ^
    --hidden-import "win32api" ^
    --hidden-import "win32gui" ^
    --clean "src/client_viewer.py"

if %errorLevel% equ 0 (
    echo.
    echo ========================================================
    echo [OK] Compilazione completata con successo!
    echo L'eseguibile portatile e' pronto in: dist\RemoteClient.exe
    echo ========================================================
) else (
    echo.
    echo [ERRORE] Compilazione fallita.
)

pause
