#!/usr/bin/env bash
set -euo pipefail

MCP_NAME="DroidPilot-MCP"
PYTHON_CMD="${PYTHON_CMD:-python3}"
CODEX_CMD="${CODEX_CMD:-codex}"
FORCE=0

usage() {
  cat <<'EOF'
Uso:
  ./scripts/install_codex_mcp.sh [opcoes]

Opcoes:
  --name NAME              Nome do servidor MCP no Codex. Padrao: DroidPilot-MCP
  --python CMD             Python usado para criar o venv. Padrao: python3
  --codex CMD              Binario do Codex CLI. Padrao: codex
  --force                  Remove registro MCP existente antes de registrar novamente
  -h, --help               Mostra esta ajuda

Variaveis equivalentes:
  PYTHON_CMD, CODEX_CMD
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
    --force)
      FORCE=1
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
  echo "Informe outro binario com --python ou PYTHON_CMD." >&2
  exit 1
fi

if ! command -v "$CODEX_CMD" >/dev/null 2>&1; then
  echo "Erro: Codex CLI nao encontrado: $CODEX_CMD" >&2
  echo "Instale o Codex CLI ou informe o caminho com --codex ou CODEX_CMD." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MCP_DIR="$PROJECT_ROOT"
VENV_DIR="$PROJECT_ROOT/.venv"
SERVER_FILE="$MCP_DIR/droidpilot_mcp_server.py"
REQUIREMENTS_FILE="$MCP_DIR/requirements.txt"

if [ ! -f "$SERVER_FILE" ]; then
  echo "Erro: servidor MCP nao encontrado em $SERVER_FILE" >&2
  exit 1
fi

if [ ! -f "$REQUIREMENTS_FILE" ]; then
  echo "Erro: requirements nao encontrado em $REQUIREMENTS_FILE" >&2
  exit 1
fi

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
    ;;
  *)
    VENV_PYTHON="$VENV_DIR/bin/python"
    ;;
esac

echo "Projeto: $PROJECT_ROOT"
echo "MCP: $MCP_DIR"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "Criando venv em $VENV_DIR"
  "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

echo "Instalando dependencias Python"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$REQUIREMENTS_FILE"

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

echo "Registrando MCP no Codex: $MCP_NAME"
"$CODEX_CMD" mcp add "$MCP_NAME" -- "$VENV_PYTHON" "$SERVER_FILE"

echo "Instalacao concluida."
echo "O servidor tenta autodetectar adb no startup."
echo "Opcionalmente configure adbPath e adbDeviceSerial no android-agent.config.json do projeto que carrega o MCP."
echo "A tool android_set_adb_config cria/atualiza esse arquivo no projeto ativo por padrao."
echo "Verifique com:"
echo "  $CODEX_CMD mcp get $MCP_NAME"
