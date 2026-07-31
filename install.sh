#!/usr/bin/env bash
#
# LocalLink installer
#
# Usage:
#   curl -sSL https://locallink.dev/install.sh | bash
#
# Or with a custom repo URL:
#   curl -sSL https://locallink.dev/install.sh | LOCAL_LINK_REPO=https://github.com/you/locallink bash
#
# Or from a local checkout (for testing):
#   LOCAL_LINK_REPO=/path/to/LocalLink bash install.sh
#

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_INSTALL_DIR="${HOME}/.locallink"
DEFAULT_BIN_DIR="${HOME}/.local/bin"

# Repository to clone. Override with LOCAL_LINK_REPO env var.
# Default points to the official LocalLink repo on GitHub.
DEFAULT_REPO="https://github.com/anomalyco/LocalLink.git"
REPO="${LOCAL_LINK_REPO:-$DEFAULT_REPO}"
BRANCH="${LOCAL_LINK_BRANCH:-main}"
INSTALL_DIR="${LOCAL_LINK_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
BIN_DIR="${LOCAL_LINK_BIN_DIR:-$DEFAULT_BIN_DIR}"

# Helper: resolve a raw install.sh URL for self-referencing in the
# docs page and README. Override with LOCAL_LINK_RAW_URL if you fork
# the repo or mirror it elsewhere.
RAW_URL_DEFAULT="https://raw.githubusercontent.com/anomalyco/LocalLink/main/install.sh"
RAW_URL="${LOCAL_LINK_RAW_URL:-$RAW_URL_DEFAULT}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { echo -e "\033[1;36m[locallink]\033[0m $*"; }
warn() { echo -e "\033[1;33m[locallink]\033[0m $*"; }
error() { echo -e "\033[1;31m[locallink]\033[0m $*" >&2; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# Ensure Python 3.10+ is available.
ensure_python() {
    local py_cmd=""
    for cmd in python3.12 python3.11 python3.10 python3; do
        if command_exists "$cmd"; then
            local version
            version=$("$cmd" --version 2>&1 | awk '{print $2}')
            local major minor
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; }; then
                py_cmd=$cmd
                break
            fi
        fi
    done
    if [ -z "$py_cmd" ]; then
        error "Python 3.10+ is required but not found. Install it first: https://www.python.org/downloads/"
    fi
    echo "$py_cmd"
}

# Add a line to a shell rc file if it's not already there.
add_path_to_rc() {
    local rc_file="$1"
    local line="export PATH=\"${BIN_DIR}:\$PATH\""
    if [ -f "$rc_file" ] && grep -qF "$BIN_DIR" "$rc_file"; then
        return
    fi
    log "Adding ${BIN_DIR} to PATH in ${rc_file}"
    echo "" >> "$rc_file"
    echo "# Added by LocalLink installer" >> "$rc_file"
    echo "$line" >> "$rc_file"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FORCE_INSTALL=0
if [ "${LOCAL_LINK_FORCE:-0}" = "1" ] || [ "${LOCAL_LINK_YES:-0}" = "1" ]; then
    FORCE_INSTALL=1
fi

# Parse simple flags.
for arg in "$@"; do
    case "$arg" in
        -y|--yes) FORCE_INSTALL=1 ;;
        -h|--help)
            echo "Usage: $0 [-y]"
            echo ""
            echo "Environment variables:"
            echo "  LOCAL_LINK_REPO          Git repo or local checkout to install"
            echo "  LOCAL_LINK_BRANCH        Git branch (default: main)"
            echo "  LOCAL_LINK_INSTALL_DIR   Install directory (default: ~/.locallink)"
            echo "  LOCAL_LINK_BIN_DIR       Directory for the launcher (default: ~/.local/bin)"
            echo "  LOCAL_LINK_FORCE=1       Reinstall without prompting"
            exit 0
            ;;
    esac
done

main() {
    log "LocalLink installer"
    log "Install dir: ${INSTALL_DIR}"
    log "Bin dir:     ${BIN_DIR}"

    PYTHON=$(ensure_python)
    log "Using Python: ${PYTHON} ($($PYTHON --version 2>&1))"

    # Create bin directory.
    mkdir -p "$BIN_DIR"

    # Get the source.
    if [ -d "$INSTALL_DIR" ]; then
        if [ "$FORCE_INSTALL" = "1" ]; then
            warn "Reinstalling into ${INSTALL_DIR}..."
            rm -rf "$INSTALL_DIR"
        else
            warn "Install directory already exists: ${INSTALL_DIR}"
            read -rp "Remove and reinstall? [y/N] " confirm
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                rm -rf "$INSTALL_DIR"
            else
                log "Aborted."
                exit 0
            fi
        fi
    fi

    if [ -d "$REPO" ]; then
        log "Installing from local checkout: ${REPO}"
        cp -R "$REPO" "$INSTALL_DIR"
    else
        log "Cloning repository: ${REPO} (branch ${BRANCH})"
        if ! command_exists git; then
            error "git is required to clone the repository. Install git or set LOCAL_LINK_REPO to a local directory."
        fi
        git clone --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
    fi

    # Create virtual environment.
    log "Creating Python virtual environment..."
    "$PYTHON" -m venv "${INSTALL_DIR}/venv"

    # Install dependencies.
    log "Installing dependencies..."
    "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip >/dev/null
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" >/dev/null

    # Create the launcher wrapper.
    log "Creating locallink launcher..."
    cat > "${BIN_DIR}/locallink" <<EOF
#!/usr/bin/env bash
# LocalLink launcher — generated by install.sh
set -euo pipefail
export LOCALLINK_INSTALL_DIR="${INSTALL_DIR}"
cd "${INSTALL_DIR}"
exec "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/run.py" "\$@"
EOF
    chmod +x "${BIN_DIR}/locallink"

    # Ensure PATH.
    case "${SHELL##*/}" in
        bash) add_path_to_rc "$HOME/.bashrc" ;;
        zsh)  add_path_to_rc "$HOME/.zshrc" ;;
        fish)
            fish_dir="${HOME}/.config/fish"
            mkdir -p "$fish_dir"
            echo "set -gx PATH ${BIN_DIR} \$PATH" >> "${fish_dir}/config.fish"
            ;;
    esac

    # Immediate PATH availability for this shell session.
    export PATH="${BIN_DIR}:${PATH}"

    log "Installation complete!"
    log "Run: locallink"
    log ""
    log "If 'locallink' is not found, restart your terminal or run:"
    log "  export PATH=\"${BIN_DIR}:\$PATH\""
    log ""
    log "Configuration:"
    log "  cd ${INSTALL_DIR}"
    log "  cp .env.example .env"
    log "  nano .env"
}

main "$@"
