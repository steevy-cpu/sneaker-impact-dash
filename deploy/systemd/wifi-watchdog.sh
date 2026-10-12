#!/bin/bash
# Station-link watchdog: the internal Wi-Fi (wlp129s0f0 = 192.168.1.164, the
# scanning station's bookmark) can die after a mesh-AP roam kick: the 4-way
# handshake races (WRONG_KEY), NM asks for a key it then refuses to reuse
# ("no secrets: connection modified since activation"), and after a no-secrets
# failure NM BLOCKS autoconnect for the profile -- autoconnect-retries=0 never
# applies (dead 2.5 days on 2026-09-11..14; also 2026-08-29). A manual
# `nmcli con up` clears that block, so that is exactly what this does.
IFACE=wlp129s0f0
PROFILE=CoastalExport
STATE=$(nmcli -g GENERAL.STATE device show "$IFACE" 2>/dev/null | cut -d' ' -f1)
if [ "$STATE" = "100" ]; then
    exit 0                                    # connected -- nothing to do
fi
echo "wifi-watchdog: $IFACE state=$STATE -> re-activating $PROFILE"
nmcli con up "$PROFILE" ifname "$IFACE" 2>&1 | tail -1
