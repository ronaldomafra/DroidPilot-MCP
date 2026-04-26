param(
  [string]$Dir = $(if ($env:DROIDPILOT_INSTALL_DIR) { $env:DROIDPILOT_INSTALL_DIR } else { Join-Path $HOME ".droidpilot-mcp" }),
  [string]$Repo = $(if ($env:DROIDPILOT_REPO_URL) { $env:DROIDPILOT_REPO_URL } else { "https://github.com/ronaldomafra/DroidPilot-MCP.git" }),
  [string]$Branch = $(if ($env:DROIDPILOT_BRANCH) { $env:DROIDPILOT_BRANCH } else { "main" }),
  [switch]$SkipUpdate,
  [string]$Name = "androidAgent",
  [string]$Python = $(if ($env:PYTHON_CMD) { $env:PYTHON_CMD } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }),
  [string]$Codex = $(if ($env:CODEX_CMD) { $env:CODEX_CMD } else { "codex" }),
  [switch]$Force,
  [switch]$Help
)

$ErrorActionPreference = "Stop"
$DefaultRepo = "https://github.com/ronaldomafra/DroidPilot-MCP.git"

function Show-Usage {
  @"
Uso:
  irm https://raw.githubusercontent.com/ronaldomafra/DroidPilot-MCP/main/scripts/install.ps1 | iex
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ronaldomafra/DroidPilot-MCP/main/scripts/install.ps1))) [opcoes]

Opcoes:
  -Dir PATH                Diretorio de instalacao. Padrao: `$HOME\.droidpilot-mcp
  -Repo URL                Repositorio Git. Padrao: https://github.com/ronaldomafra/DroidPilot-MCP.git
  -Branch NAME             Branch/tag para instalar. Padrao: main
  -SkipUpdate              Nao atualiza o checkout se o diretorio ja existir
  -Name NAME               Nome do servidor MCP no Codex. Padrao: androidAgent
  -Python CMD              Python usado para criar o venv. Padrao: py, se existir; senao python
  -Codex CMD               Binario do Codex CLI. Padrao: codex
  -Force                   Recria registro MCP existente
  -Help                    Mostra esta ajuda

Variaveis equivalentes:
  DROIDPILOT_INSTALL_DIR, DROIDPILOT_REPO_URL, DROIDPILOT_BRANCH, PYTHON_CMD, CODEX_CMD
"@
}

function Test-CommandExists {
  param([string]$Command)
  return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Test-DirectoryNotEmpty {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return $false
  }
  return [bool](Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Invoke-PythonCommand {
  param([string[]]$Arguments)
  $extraArgs = @()
  if ($Python -eq "py") {
    $extraArgs += "-3"
  }
  & $Python @extraArgs @Arguments
}

function Install-WithGit {
  if (Test-Path -LiteralPath (Join-Path $Dir ".git")) {
    Write-Host "Atualizando DroidPilot MCP em $Dir"
    git -C $Dir remote set-url origin $Repo
    if (-not $SkipUpdate) {
      git -C $Dir fetch origin $Branch
      git -C $Dir checkout $Branch
      git -C $Dir pull --ff-only origin $Branch
    }
    return
  }

  if ((Test-Path -LiteralPath $Dir) -and (Test-DirectoryNotEmpty $Dir)) {
    throw "$Dir ja existe e nao eh um checkout Git do DroidPilot MCP. Use -Dir para escolher outro diretorio ou remova o diretorio existente."
  }

  Write-Host "Clonando DroidPilot MCP em $Dir"
  $parent = Split-Path -Parent $Dir
  if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  git clone --branch $Branch $Repo $Dir
}

function Install-WithZip {
  if ($Repo -ne $DefaultRepo) {
    throw "-Repo customizado requer git instalado."
  }

  if ((Test-Path -LiteralPath $Dir) -and $SkipUpdate) {
    Write-Host "Usando instalacao existente em $Dir"
    return
  }

  $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("droidpilot-mcp-" + [System.Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
  try {
    $zipPath = Join-Path $tmpDir "droidpilot.zip"
    $zipUrl = "https://github.com/ronaldomafra/DroidPilot-MCP/archive/refs/heads/$Branch.zip"
    Write-Host "Baixando DroidPilot MCP de $zipUrl"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force
    $extracted = Get-ChildItem -LiteralPath $tmpDir -Directory | Where-Object { $_.Name -like "DroidPilot-MCP-*" } | Select-Object -First 1
    if (-not $extracted) {
      throw "Arquivo baixado nao contem o projeto DroidPilot MCP."
    }
    if (Test-Path -LiteralPath $Dir) {
      Remove-Item -LiteralPath $Dir -Recurse -Force
    }
    $parent = Split-Path -Parent $Dir
    if ($parent) {
      New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    Move-Item -LiteralPath $extracted.FullName -Destination $Dir
  } finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
  }
}

if ($Help) {
  Show-Usage
  exit 0
}

if ([string]::IsNullOrWhiteSpace($Dir)) {
  throw "-Dir nao pode ser vazio."
}

if ([string]::IsNullOrWhiteSpace($Repo)) {
  throw "-Repo nao pode ser vazio."
}

if ([string]::IsNullOrWhiteSpace($Branch)) {
  throw "-Branch nao pode ser vazio."
}

if (-not (Test-CommandExists $Python)) {
  throw "Python nao encontrado: $Python. Instale Python ou informe outro comando com -Python."
}

if (-not (Test-CommandExists $Codex)) {
  throw "Codex CLI nao encontrado: $Codex. Instale o Codex CLI ou informe outro caminho com -Codex."
}

if (Test-CommandExists "git") {
  Install-WithGit
} else {
  Install-WithZip
}

$venvDir = Join-Path $Dir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$serverFile = Join-Path $Dir "droidpilot_mcp_server.py"
$requirementsFile = Join-Path $Dir "requirements.txt"

if (-not (Test-Path -LiteralPath $serverFile)) {
  throw "Servidor MCP nao encontrado em $serverFile"
}

if (-not (Test-Path -LiteralPath $requirementsFile)) {
  throw "requirements nao encontrado em $requirementsFile"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
  Write-Host "Criando venv em $venvDir"
  Invoke-PythonCommand @("-m", "venv", $venvDir)
}

Write-Host "Instalando dependencias Python"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r $requirementsFile

Write-Host "Validando import do SDK MCP"
& $venvPython -c "from mcp.server.fastmcp import FastMCP; print('SDK MCP OK')"

$existing = $false
try {
  & $Codex mcp get $Name *> $null
  $existing = $true
} catch {
  $existing = $false
}

if ($existing) {
  if ($Force) {
    Write-Host "Removendo registro MCP existente: $Name"
    try {
      & $Codex mcp remove $Name *> $null
    } catch {
      Write-Host "Aviso: falha ao remover registro existente; tentando registrar novamente."
    }
  } else {
    Write-Host "Registro MCP '$Name' ja existe. Use -Force para recriar."
    Write-Host "Dependencias/config foram atualizadas, mas o registro Codex nao foi alterado."
    exit 0
  }
}

Write-Host "Registrando MCP no Codex: $Name"
& $Codex mcp add $Name -- $venvPython $serverFile

Write-Host ""
Write-Host "DroidPilot MCP instalado em: $Dir"
Write-Host "O servidor tenta autodetectar adb no startup."
Write-Host "Opcionalmente configure adbPath e adbDeviceSerial no android-agent.config.json do projeto que carrega o MCP."
Write-Host "A tool android_set_adb_config cria/atualiza esse arquivo no projeto ativo por padrao."
Write-Host "Verifique com:"
Write-Host "  $Codex mcp get $Name"
