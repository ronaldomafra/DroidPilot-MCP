#!/usr/bin/env bash
set -euo pipefail

DEFAULT_REPO_URL="https://github.com/ronaldomafra/DroidPilot-MCP.git"
REPO_URL="${DROIDPILOT_REPO_URL:-$DEFAULT_REPO_URL}"
BRANCH="${DROIDPILOT_BRANCH:-main}"
INSTALL_DIR="${DROIDPILOT_INSTALL_DIR:-$HOME/.droidpilot-mcp}"
SKIP_UPDATE=0
FORWARD_ARGS=()

usage() {
  cat <<'EOF'
Uso:
  curl -fsSL https://raw.githubusercontent.com/ronaldomafra/DroidPilot-MCP/main/scripts/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/ronaldomafra/DroidPilot-MCP/main/scripts/install.sh | bash -s -- [opcoes]

Opcoes do bootstrap:
  --dir PATH               Diretorio de instalacao. Padrao: ~/.droidpilot-mcp
  --repo URL               Repositorio Git. Padrao: https://github.com/ronaldomafra/DroidPilot-MCP.git
  --branch NAME            Branch/tag para instalar. Padrao: main
  --skip-update            Nao atualiza o checkout se o diretorio ja existir
  -h, --help               Mostra esta ajuda

As demais opcoes sao repassadas para scripts/install_codex_mcp.sh:
  --name NAME              Nome do servidor MCP no Codex
  --python CMD             Python usado para criar o venv
  --codex CMD              Binario do Codex CLI
  --force                  Recria registro MCP existente

Variaveis equivalentes:
  DROIDPILOT_INSTALL_DIR, DROIDPILOT_REPO_URL, DROIDPILOT_BRANCH, PYTHON_CMD, CODEX_CMD
EOF
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)
      INSTALL_DIR="${2:-}"
      shift 2
      ;;
    --repo)
      REPO_URL="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    --skip-update)
      SKIP_UPDATE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ -z "$INSTALL_DIR" ]; then
  echo "Erro: --dir nao pode ser vazio." >&2
  exit 1
fi

if [ -z "$REPO_URL" ]; then
  echo "Erro: --repo nao pode ser vazio." >&2
  exit 1
fi

if [ -z "$BRANCH" ]; then
  echo "Erro: --branch nao pode ser vazio." >&2
  exit 1
fi

install_with_git() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Atualizando DroidPilot MCP em $INSTALL_DIR"
    git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
    if [ "$SKIP_UPDATE" -eq 0 ]; then
      git -C "$INSTALL_DIR" fetch origin "$BRANCH"
      git -C "$INSTALL_DIR" checkout "$BRANCH"
      git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
    fi
    return
  fi

  if [ -e "$INSTALL_DIR" ] && [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
    echo "Erro: $INSTALL_DIR ja existe e nao eh um checkout Git do DroidPilot MCP." >&2
    echo "Use --dir para escolher outro diretorio ou remova o diretorio existente." >&2
    exit 1
  fi

  echo "Clonando DroidPilot MCP em $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
}

install_with_tarball() {
  if [ "$REPO_URL" != "$DEFAULT_REPO_URL" ]; then
    echo "Erro: --repo customizado requer git instalado." >&2
    exit 1
  fi

  if [ -e "$INSTALL_DIR" ] && [ "$SKIP_UPDATE" -eq 1 ]; then
    echo "Usando instalacao existente em $INSTALL_DIR"
    return
  fi

  if ! command_exists curl || ! command_exists tar; then
    echo "Erro: instale git, ou curl e tar, para baixar o DroidPilot MCP." >&2
    exit 1
  fi

  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  tarball_url="https://github.com/ronaldomafra/DroidPilot-MCP/archive/refs/heads/$BRANCH.tar.gz"

  echo "Baixando DroidPilot MCP de $tarball_url"
  curl -fsSL "$tarball_url" -o "$tmp_dir/droidpilot.tar.gz"
  tar -xzf "$tmp_dir/droidpilot.tar.gz" -C "$tmp_dir"

  extracted_dir="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$extracted_dir" ]; then
    echo "Erro: arquivo baixado nao contem o projeto DroidPilot MCP." >&2
    exit 1
  fi

  rm -rf "$INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  mv "$extracted_dir" "$INSTALL_DIR"
}

if command_exists git; then
  install_with_git
else
  install_with_tarball
fi

installer="$INSTALL_DIR/scripts/install_codex_mcp.sh"
if [ ! -x "$installer" ]; then
  echo "Erro: instalador interno nao encontrado ou sem permissao de execucao: $installer" >&2
  exit 1
fi

echo "Executando instalador Codex do DroidPilot MCP"
"$installer" "${FORWARD_ARGS[@]}"

echo
echo "DroidPilot MCP instalado em: $INSTALL_DIR"
echo "Para atualizar depois, execute novamente o comando curl ou rode:"
echo "  $installer --force"
