#!/bin/bash
# Link the plugin into every KiCad 9/10 user plugin directory on this machine
# so edits here show up after "Tools > External Plugins > Refresh Plugins".
#
#   ./scripts/install-dev.sh          link
#   ./scripts/install-dev.sh --remove unlink
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="$PWD/plugins/autorouter"

case "$(uname -s)" in
  Darwin) BASES=("$HOME/Documents/KiCad") ;;
  Linux)  BASES=("$HOME/.local/share/kicad") ;;
  *)      BASES=("$HOME/Documents/KiCad") ;;
esac

linked=0
for base in "${BASES[@]}"; do
  for verdir in "$base"/9.0 "$base"/10.0; do
    [ -d "$verdir" ] || continue
    dest="$verdir/scripting/plugins/autorouter"
    if [ "${1:-}" = "--remove" ]; then
      [ -L "$dest" ] && rm "$dest" && echo "removed $dest"
      continue
    fi
    mkdir -p "$(dirname "$dest")"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      echo "skip: $dest exists and is not a symlink" >&2
      continue
    fi
    ln -sfn "$SRC" "$dest"
    echo "linked $dest"
    linked=$((linked + 1))
  done
done
[ "${1:-}" = "--remove" ] || [ "$linked" -gt 0 ] || echo "no KiCad 9/10 user directory found" >&2
