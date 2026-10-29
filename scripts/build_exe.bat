@echo off
setlocal enabledelayedexpansion
echo ========================================================
echo   Compilazione Deterministica AetherDesk.exe Standalone
echo   (Modulo Nativo Rust + Packaging PyInstaller)
echo ========================================================

cd /d "%~dp0\.."

echo [1/3] Verifica toolchain Rust (cargo)...
where cargo >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERRORE BLOCCANTE] Toolchain Rust (cargo) non trovato nel PATH.
    echo La modalita' nativa ad alte prestazioni H.264/WebRTC richiede il compilatore Rust.
    echo Installare Rust da https://rustup.rs prima di procedere.
    exit /b 1
)

echo Compilazione crate nativo Rust (native\aether_core)...
cargo build --release --manifest-path "native\aether_core\Cargo.toml"
if errorlevel 1 (
    echo.
    echo [ERRORE BLOCCANTE] Compilazione Cargo di native\aether_core fallita.
    exit /b 1
)

if not exist "native\aether_core\target\release\aether_core.dll" (
    echo.
    echo [ERRORE BLOCCANTE] Artefatto native\aether_core\target\release\aether_core.dll non trovato.
    exit /b 1
)

if not exist "dist" mkdir "dist"
copy /y "native\aether_core\target\release\aether_core.dll" "dist\aether_core.dll" >nul 2>&1

echo.
echo [2/3] Pulizia directory build precedenti...
if exist "build" rmdir /s /q "build"

echo.
echo [3/3] Packaging PyInstaller con RemoteClient.spec...
pyinstaller --clean --workpath "%TEMP%\aether_build" --distpath "%TEMP%\aether_dist" "RemoteClient.spec"

if not errorlevel 1 (
    copy /y "%TEMP%\aether_dist\AetherDesk.exe" "dist\AetherDesk.exe" >nul 2>&1
    copy /y "%TEMP%\aether_dist\AetherDesk.exe" "dist\RemoteClient.exe" >nul 2>&1
    copy /y "%TEMP%\aether_dist\AetherDesk.exe" "%USERPROFILE%\Downloads\AetherDesk.exe" >nul 2>&1
    copy /y "dist\aether_core.dll" "%USERPROFILE%\Downloads\aether_core.dll" >nul 2>&1
    echo.
    echo ========================================================
    echo [OK] Compilazione e packaging completati con successo!
    echo L'eseguibile e aether_core.dll sono pronti in: dist\
    echo Copiati automaticamente in: %USERPROFILE%\Downloads\
    echo ========================================================
) else (
    echo.
    echo [ERRORE BLOCCANTE] Packaging PyInstaller fallito.
    exit /b 1
)
