param(
  [string]$Name = "DroidPilot-MCP",
  [string]$Python = $(if ($env:PYTHON_CMD) { $env:PYTHON_CMD } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }),
  [string]$Codex = $(if ($env:CODEX_CMD) { $env:CODEX_CMD } else { "codex" }),
  [string]$VenvDir = "",
  [switch]$Force,
  [switch]$SkipDeps,
  [switch]$Help
)

$ErrorActionPreference = "Stop"

function Show-Usage {
  @"
Uso:
  .\dev\register-local-mcp.ps1 [-Force]

Opcoes:
  -Name NAME       Nome do servidor MCP no Codex. Padrao: DroidPilot-MCP
  -Python CMD      Python usado para criar o venv. Padrao: py, se existir; senao python
  -Codex CMD       Binario do Codex CLI. Padrao: codex
  -VenvDir PATH    Diretorio do venv local. Padrao: <repo>\.venv-dev
  -Force           Remove registro MCP existente antes de registrar novamente
  -SkipDeps        Nao instala dependencies; apenas registra o MCP
  -Help            Mostra esta ajuda

Variaveis equivalentes:
  PYTHON_CMD, CODEX_CMD
"@
}

function Test-CommandExists {
  param([string]$Command)
  return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Invoke-PythonCommand {
  param([string[]]$Arguments)
  $extraArgs = @()
  if ($Python -eq "py") {
    $extraArgs += "-3"
  }
  & $Python @extraArgs @Arguments
}

if ($Help) {
  Show-Usage
  exit 0
}

if ([string]::IsNullOrWhiteSpace($Name)) {
  throw "-Name nao pode ser vazio."
}

if (-not (Test-CommandExists $Python)) {
  throw "Python nao encontrado: $Python. Instale Python ou informe outro comando com -Python."
}

if (-not (Test-CommandExists $Codex)) {
  throw "Codex CLI nao encontrado: $Codex. Instale o Codex CLI ou informe outro caminho com -Codex."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
$serverFile = Join-Path $projectRoot "droidpilot_mcp_server.py"
$requirementsFile = Join-Path $projectRoot "requirements.txt"

if ([string]::IsNullOrWhiteSpace($VenvDir)) {
  $VenvDir = Join-Path $projectRoot ".venv-dev"
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $serverFile)) {
  throw "Servidor MCP nao encontrado em $serverFile"
}

if (-not (Test-Path -LiteralPath $requirementsFile)) {
  throw "requirements nao encontrado em $requirementsFile"
}

Write-Host "Projeto local: $projectRoot"
Write-Host "Servidor MCP: $serverFile"
Write-Host "Venv local: $VenvDir"

if (-not (Test-Path -LiteralPath $venvPython)) {
  Write-Host "Criando venv local"
  Invoke-PythonCommand @("-m", "venv", $VenvDir)
}

if (-not $SkipDeps) {
  Write-Host "Instalando dependencias Python"
  & $venvPython -m pip install --upgrade pip
  & $venvPython -m pip install -r $requirementsFile
}

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

Write-Host "Registrando MCP local no Codex: $Name"
& $Codex mcp add $Name -- $venvPython $serverFile

Write-Host ""
Write-Host "MCP local registrado: $Name"
Write-Host "Branch/checkout atual sera usado diretamente."
Write-Host "Verifique com:"
Write-Host "  $Codex mcp get $Name"
