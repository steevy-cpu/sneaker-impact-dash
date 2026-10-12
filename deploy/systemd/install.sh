#!/usr/bin/env bash
# Install / verify the dash's systemd USER units from this directory.
#
#   deploy/systemd/install.sh --check   compare the repo copies with ~/.config/systemd/user (no changes)
#   deploy/systemd/install.sh           copy them over (+ wifi-watchdog.sh to ~/.local/bin) and daemon-reload
#
# @DASH_DIR@ in the unit files is replaced with this repo's actual location, so the
# units follow the repo if it is ever moved. This script NEVER starts, stops or
# restarts anything; after installing, apply with e.g.
#   systemctl --user enable --now sneaker-dash-backup.timer wifi-watchdog.timer   (first time)
#   systemctl --user restart sneaker-dash-http sneaker-dash-https sneaker-dash-preview
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASH_DIR="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"
UNITS=(sneaker-dash-http.service sneaker-dash-https.service sneaker-dash-preview.service
       sneaker-dash-backup.service sneaker-dash-backup.timer ollama.service
       wifi-watchdog.service wifi-watchdog.timer)

render() { sed "s#@DASH_DIR@#$DASH_DIR#g" "$HERE/$1"; }

if [[ "${1:-}" == "--check" ]]; then
    rc=0
    for u in "${UNITS[@]}"; do
        if diff -q <(render "$u") "$UNIT_DIR/$u" >/dev/null 2>&1; then echo "OK    $u"
        else echo "DIFF  $u"; rc=1; fi
    done
    if diff -q "$HERE/wifi-watchdog.sh" "$BIN_DIR/wifi-watchdog.sh" >/dev/null 2>&1; then echo "OK    wifi-watchdog.sh"
    else echo "DIFF  wifi-watchdog.sh"; rc=1; fi
    exit $rc
fi

mkdir -p "$UNIT_DIR" "$BIN_DIR"
for u in "${UNITS[@]}"; do render "$u" > "$UNIT_DIR/$u"; echo "installed $u"; done
install -m 0755 "$HERE/wifi-watchdog.sh" "$BIN_DIR/wifi-watchdog.sh"; echo "installed wifi-watchdog.sh"
systemctl --user daemon-reload
echo "daemon-reload done. Nothing was restarted — see the header of this script for the apply commands."
