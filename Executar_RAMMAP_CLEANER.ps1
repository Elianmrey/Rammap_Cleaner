# RAMMAP CLEANER - launcher PowerShell
# Solicita UAC, instala dependencias ausentes, executa os testes e
# somente inicia a interface grafica se TODOS os testes passarem.

$ErrorActionPreference = 'Stop'

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[OK]   $Message" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[ERRO] $Message" -ForegroundColor Red
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Confirma que o launcher esta sendo executado no Windows.
if ($env:OS -ne 'Windows_NT') {
    Write-Fail 'Este launcher funciona somente no Windows.'
    exit 1
}

# Eleva o proprio script via UAC quando necessario.
if (-not (Test-IsAdministrator)) {
    Write-Info 'Solicitando privilegios de Administrador (UAC)...'
    try {
        $quotedScript = '"{0}"' -f $PSCommandPath
        $elevatedProcess = Start-Process `
            -FilePath (Join-Path $PSHOME 'powershell.exe') `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $quotedScript) `
            -WorkingDirectory $PSScriptRoot `
            -Verb RunAs `
            -PassThru
        exit 0
    }
    catch {
        Write-Fail 'A elevacao foi cancelada ou nao foi possivel iniciar o PowerShell como Administrador.'
        Write-Host $_.Exception.Message -ForegroundColor Yellow
        exit 1
    }
}

$ProjectRoot = $PSScriptRoot
$RequirementsFile = Join-Path $ProjectRoot 'requirements.txt'
$TestFile = Join-Path $ProjectRoot 'test_rammap_cleaner.py'
$AppFile = Join-Path $ProjectRoot 'rammap_cleaner_gui.py'

Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkCyan
Write-Host '              RAMMAP CLEANER - EXECUTOR' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor DarkCyan
Write-Host "Projeto: $ProjectRoot"
Write-Host 'Modo: Administrador'
Write-Host ''

# Verifica se todos os arquivos necessarios existem.
$requiredFiles = @($RequirementsFile, $TestFile, $AppFile)
foreach ($file in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        Write-Fail "Arquivo obrigatorio nao encontrado: $file"
        Write-Host 'Extraia o projeto completo e mantenha este launcher na mesma pasta dos arquivos Python.' -ForegroundColor Yellow
        Read-Host 'Pressione ENTER para sair'
        exit 1
    }
}

Set-Location -LiteralPath $ProjectRoot

# Descobre o interpretador Python. Prioriza o Python Launcher do Windows.
$PythonExe = $null
$PythonPrefix = @()
$pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
$pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue

if ($pyLauncher) {
    $PythonExe = $pyLauncher.Source
    $PythonPrefix = @('-3')
}
elseif ($pythonCommand) {
    $PythonExe = $pythonCommand.Source
}
else {
    Write-Fail 'Python nao foi encontrado. Instale Python 3.10 ou superior e marque a opcao Add Python to PATH.'
    Read-Host 'Pressione ENTER para sair'
    exit 1
}

Write-Info 'Verificando a versao do Python...'
& $PythonExe @PythonPrefix --version
if ($LASTEXITCODE -ne 0) {
    Write-Fail 'Nao foi possivel executar o Python selecionado.'
    Read-Host 'Pressione ENTER para sair'
    exit 1
}

# Confere a versao minima exigida pelo projeto (Python 3.10).
& $PythonExe @PythonPrefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Fail 'O projeto requer Python 3.10 ou superior.'
    Read-Host 'Pressione ENTER para sair'
    exit 1
}

# Instala somente as dependencias do projeto e o pytest, caso estejam ausentes.
Write-Info 'Verificando dependencias (psutil, pytest e tkinter)...'
& $PythonExe @PythonPrefix -c "import psutil, pytest, tkinter"
if ($LASTEXITCODE -ne 0) {
    Write-Info 'Uma ou mais dependencias estao ausentes. Tentando instala-las...'
    & $PythonExe @PythonPrefix -m pip install -r $RequirementsFile pytest
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'Falha ao instalar dependencias. Verifique a conexao com a internet e a instalacao do pip.'
        Read-Host 'Pressione ENTER para sair'
        exit 1
    }

    # Revalida imports depois da instalacao.
    & $PythonExe @PythonPrefix -c "import psutil, pytest, tkinter"
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'As dependencias continuam indisponiveis. Verifique Python, pip e tkinter.'
        Read-Host 'Pressione ENTER para sair'
        exit 1
    }
}
Write-Ok 'Dependencias verificadas.'

# TESTES: o programa NAO sera iniciado se o pytest retornar qualquer falha.
Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkCyan
Write-Host ' ETAPA 1/2 - EXECUTANDO TESTES AUTOMATIZADOS' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor DarkCyan
& $PythonExe @PythonPrefix -m pytest -v --tb=short $TestFile
$TestExitCode = $LASTEXITCODE

if ($TestExitCode -ne 0) {
    Write-Host ''
    Write-Fail "Os testes falharam (codigo de saida: $TestExitCode)."
    Write-Host 'Por seguranca, a interface do RAMMAP CLEANER NAO sera iniciada.' -ForegroundColor Yellow
    Read-Host 'Pressione ENTER para sair'
    exit $TestExitCode
}

Write-Host ''
Write-Ok 'Todos os testes automatizados passaram.'

# PROGRAMA: somente chega aqui se os testes passaram.
Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkCyan
Write-Host ' ETAPA 2/2 - INICIANDO RAMMAP CLEANER' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor DarkCyan
Write-Info 'A interface sera iniciada com os mesmos privilegios administrativos.'
Write-Host 'Feche a janela do RAMMAP CLEANER para retornar a este terminal.' -ForegroundColor Gray
Write-Host ''

& $PythonExe @PythonPrefix $AppFile
$AppExitCode = $LASTEXITCODE

if ($AppExitCode -eq 0) {
    Write-Ok 'RAMMAP CLEANER encerrado normalmente.'
}
else {
    Write-Fail "RAMMAP CLEANER encerrou com codigo $AppExitCode. Consulte o arquivo de log da aplicacao."
}

Write-Host ''
Read-Host 'Pressione ENTER para fechar este terminal'
exit $AppExitCode
