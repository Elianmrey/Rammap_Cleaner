@echo off
setlocal
chcp 65001 >nul

set "PS_SCRIPT=%~dp0Executar_RAMMAP_CLEANER.ps1"

if not exist "%PS_SCRIPT%" (
    echo [ERRO] Nao foi encontrado Executar_RAMMAP_CLEANER.ps1.
    echo Mantenha os dois arquivos na mesma pasta do projeto.
    pause
    exit /b 1
)

REM O script PowerShell solicita elevacao pelo UAC quando necessario.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%"

if errorlevel 1 (
    echo.
    echo [ERRO] O launcher terminou com falha.
    pause
)

endlocal
