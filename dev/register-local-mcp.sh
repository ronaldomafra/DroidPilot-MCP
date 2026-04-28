#!/usr/bin/env bash
set -euo pipefail

MCP_NAME="${MCP_NAME:-DroidPilot-MCP}"
PYTHON_CMD="${PYTHON_CMD:-python3}"
CODEX_CMD="${CODEX_CMD:-codex}"
VENV_DIR=""
FORCE=0
SKIP_DEPS=0

usage() {
  cat <<'EOF'
Uso:
  ./dev/register-local-mcp.sh [opcoes]

Opcoes:
  --name NAME       Nome do servidor MCP no Codex. Padrao: DroidPilot-MCP
  --python CMD      Python usado para criar o venv. Padrao: python3
  --codex CMD       Binario do Codex CLI. Padrao: codex
  --venv-dir PATH   Diretorio do venv local. Padrao: <repo>/.venv-dev
  --force           Remove registro MCP existente antes de registrar novamente
  --skip-deps       Nao instala dependencies; apenas registra o MCP
  -h, --help        Mostra esta ajuda

Variaveis equivalentes:
  MCP_NAME, PYTHON_CMD, CODEX_CMD
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name)
      MCP_NAME="${2:-}"
      shift 2
      ;;
    --python)
      PYTHON_CMD="${2:-}"
      shift 2
      ;;
    --codex)
      CODEX_CMD="${2:-}"
      shift 2
      ;;
    --venv-dir)
      VENV_DIR="${2:-}"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --skip-deps)
      SKIP_DEPS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Opcao desconhecida: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ -z "$MCP_NAME" ]; then
  echo "Erro: --name nao pode ser vazio." >&2
  exit 1
fi

if ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
  echo "Erro: Python nao encontrado: $PYTHON_CMD" >&2
  exit 1
fi

if ! command -v "$CODEX_CMD" >/dev/null 2>&1; then
  echo "Erro: Codex CLI nao encontrado: $CODEX_CMD" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_FILE="$PROJECT_ROOT/droidpilot_mcp_server.py"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"

if [ -z "$VENV_DIR" ]; then
  VENV_DIR="$PROJECT_ROOT/.venv-dev"
fi

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
    ;;
  *)
    VENV_PYTHON="$VENV_DIR/bin/python"
    ;;
esac

if [ ! -f "$SERVER_FILE" ]; then
  echo "Erro: servidor MCP nao encontrado em $SERVER_FILE" >&2
  exit 1
fi

if [ ! -f "$REQUIREMENTS_FILE" ]; then
  echo "Erro: requirements nao encontrado em $REQUIREMENTS_FILE" >&2
  exit 1
fi

echo "Projeto local: $PROJECT_ROOT"
echo "Servidor MCP: $SERVER_FILE"
echo "Venv local: $VENV_DIR"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "Criando venv local"
  "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

if [ "$SKIP_DEPS" -eq 0 ]; then
  echo "Instalando dependencias Python"
  "$VENV_PYTHON" -m pip install --upgrade pip
  "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS_FILE"
fi

echo "Validando import do SDK MCP"
"$VENV_PYTHON" -c "from mcp.server.fastmcp import FastMCP; print('SDK MCP OK')"

if "$CODEX_CMD" mcp get "$MCP_NAME" >/dev/null 2>&1; then
  if [ "$FORCE" -eq 1 ]; then
    echo "Removendo registro MCP existente: $MCP_NAME"
    "$CODEX_CMD" mcp remove "$MCP_NAME" >/dev/null 2>&1 || true
  else
    echo "Registro MCP '$MCP_NAME' ja existe. Use --force para recriar."
    echo "Dependencias/config foram atualizadas, mas o registro Codex nao foi alterado."
    exit 0
  fi
fi

echo "Registrando MCP local no Codex: $MCP_NAME"
"$CODEX_CMD" mcp add "$MCP_NAME" -- "$VENV_PYTHON" "$SERVER_FILE"

echo
echo "MCP local registrado: $MCP_NAME"
echo "Branch/checkout atual sera usado diretamente."
echo "Verifique com:"
echo "  $CODEX_CMD mcp get $MCP_NAME"
