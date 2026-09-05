#!/usr/bin/env bash
# setup-cachyos.sh — reproduce this pi instance on CachyOS (Arch-based).
#
# Installs, pinned to the versions proven in the lab:
#   pi-coding-agent / pi-server / pi-client @ 0.85.1  (server+client are
#       REQUIRED: background subagent children fail without them)
#   pi-subagents @ 0.65.1  (custom addon, via ~/.pi/agent/npm project)
#   android-tools (adb + fastboot, official Arch repos, already on PATH)
#   node 24.x + npm (Arch repos), base-devel, git
#   python sim deps: capstone, kaitaistruct, hexdump (pip)
#
# Explicitly NOT carried over (secrets / machine state — recreate by hand):
#   ~/.pi/agent/auth.json, sessions/, settings.json, trust.json, missions/,
#   models-store.json. Run `pi auth` yourself after install.
#
# Usage: ./setup-cachyos.sh [--yes] [--no-upgrade] [--with-opencode]
#                            [--with-java] [--no-python-deps]
#   --yes            pass --noconfirm to pacman
#   --no-upgrade     only -Sy + install (skip full -Syu; Arch-purists object)
#   --with-opencode  also install opencode-ai@1.18.29 (separate CLI, optional)
#   --with-java      also install jdk21-openjdk (Ghidra plugin builds only)
#   --no-python-deps skip pip sim packages
set -euo pipefail

PI_VER="0.85.1"
SUBAGENTS_VER="0.65.1"
OPENCODE_VER="1.18.29"
NPM_PREFIX="${NPM_PREFIX:-$HOME/.npm-global}"

YES=""; UPGRADE="-Syu"; OPENCODE=0; JAVA=0; PYDEPS=1
for a in "$@"; do
  case "$a" in
    --yes) YES="--noconfirm" ;;
    --no-upgrade) UPGRADE="-Sy" ;;
    --with-opencode) OPENCODE=1 ;;
    --with-java) JAVA=1 ;;
    --no-python-deps) PYDEPS=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

# ---- 0. OS gate ---------------------------------------------------------
if ! command -v pacman >/dev/null 2>&1; then
  echo "REFUSED: pacman not found — this script targets CachyOS/Arch." >&2
  exit 2
fi
if ! grep -qiE 'arch|cachyos|endeavouros|manjaro' /etc/os-release 2>/dev/null; then
  echo "WARNING: /etc/os-release does not look Arch-based; continuing anyway."
fi
if ! sudo -n true 2>/dev/null; then
  echo "sudo will prompt for your password when needed."
fi

# ---- 1. system packages ---------------------------------------------------
PKGS=(base-devel git nodejs npm android-tools python python-pip)
[ "$JAVA" = "1" ] && PKGS+=(jdk21-openjdk)
echo "==> pacman $UPGRADE ${PKGS[*]}"
# shellcheck disable=SC2086
sudo pacman $UPGRADE --needed $YES "${PKGS[@]}"
command -v adb fastboot node npm git python3 >/dev/null

# ---- 2. npm user prefix + PATH (idempotent, bash/zsh/fish) ----------------
if [ "$(npm config get prefix)" != "$NPM_PREFIX" ]; then
  npm config set prefix "$NPM_PREFIX"
fi
mkdir -p "$NPM_PREFIX/bin"
add_path_once() {  # $1 = rc file, $2.. = line to ensure
  local rc="$1"; shift
  [ -f "$rc" ] || return 0
  grep -qF "kansas-modem-unlock pi PATH" "$rc" 2>/dev/null && return 0
  printf '\n# kansas-modem-unlock pi PATH\n%s\n' "$*" >> "$rc"
  echo "PATH wired in $rc (restart shell or: source $rc)"
}
add_path_once "$HOME/.bashrc" "export PATH=\"\$HOME/.npm-global/bin:\$PATH\""
add_path_once "$HOME/.zshrc" "export PATH=\"\$HOME/.npm-global/bin:\$PATH\""
if [ -f "$HOME/.config/fish/config.fish" ]; then
  grep -qF "kansas-modem-unlock pi PATH" "$HOME/.config/fish/config.fish" 2>/dev/null \
    || printf '\n# kansas-modem-unlock pi PATH\nfish_add_path $HOME/.npm-global/bin\n' \
      >> "$HOME/.config/fish/config.fish"
fi
export PATH="$NPM_PREFIX/bin:$PATH"

# ---- 3. pi trio (pinned; server+client are load-bearing) -------------------
echo "==> npm i -g pi trio @$PI_VER"
npm install -g \
  "@earendil-works/pi-coding-agent@$PI_VER" \
  "@earendil-works/pi-server@$PI_VER" \
  "@earendil-works/pi-client@$PI_VER"
if [ "$OPENCODE" = "1" ]; then
  npm install -g "opencode-ai@$OPENCODE_VER"
fi

# ---- 4. custom addon: ~/.pi/agent/npm project (mirrors this machine) ------
echo "==> pi-subagents addon @$SUBAGENTS_VER"
mkdir -p "$HOME/.pi/agent/npm"
if [ ! -f "$HOME/.pi/agent/npm/package.json" ]; then
  cat > "$HOME/.pi/agent/npm/package.json" <<EOF
{
  "name": "pi-extensions",
  "private": true,
  "dependencies": {
    "pi-subagents": "$SUBAGENTS_VER"
  }
}
EOF
fi
(cd "$HOME/.pi/agent/npm" && npm install)

# ---- 5. python sim deps (optional) ------------------------------------------
if [ "$PYDEPS" = "1" ]; then
  echo "==> pip sim packages"
  python3 -m pip install --user --upgrade capstone kaitaistruct hexdump
fi

# ---- 6. verify --------------------------------------------------------------
echo "==> verify"
node --version
npm --version
pi --version
adb version | head -n 1
fastboot --version 2>&1 | head -n 1
node -e "console.log('pi-subagents:', JSON.parse(require('fs').readFileSync(process.env.HOME + '/.pi/agent/npm/node_modules/pi-subagents/package.json')).version)"
test -x "$NPM_PREFIX/bin/pi" && echo "pi on npm-global PATH: OK"

cat <<'EOF'

DONE. Next steps (manual, by design):
  1. Restart your shell (or source its rc) so ~/.npm-global/bin is on PATH.
  2. `pi auth` — log in yourself. NEVER copy auth.json/sessions/keys
     from another machine.
  3. Clone your repos (val-protocol lab, this tool) separately.
  4. Ghidra + MTK loader + nanomips plugin remain manual installs
     (needs JDK only if rebuilding the plugin: re-run with --with-java).
  5. Native Linux bonus: no WSL layer — vendor MTK QEMU/GAS/GDBsim run
     directly if you install that toolchain later.
EOF
