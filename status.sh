#!/bin/bash
# wire status
# Usage: ./status.sh or: wire status

# Auto-detect WireGuard binary
if [[ "$OSTYPE" == "darwin"* ]]; then
    WG="${WG_PATH:-/opt/homebrew/bin/wg}"
    INTERFACE="${WIRE_INTERFACE:-utun9}"
else
    WG="${WG_PATH:-wg}"
    INTERFACE="${WIRE_INTERFACE:-wire0}"
fi

# Load server URL from config
CONFIG_FILE="${WIRE_CONFIG:-/etc/meshpop/wire.json}"
if [ -f "$CONFIG_FILE" ]; then
    SERVER=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('server_urls',[''])[0])" 2>/dev/null)
fi
SERVER="${SERVER:-${WIRE_SERVER_URL:-}}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}╔═══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     wire status                       ║${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════╝${NC}"
echo

# 1. Service status
if [[ "$OSTYPE" == "darwin"* ]]; then
    if launchctl list | grep -q "com.meshpop.wire"; then
        echo -e "${GREEN}● Service: running (launchd)${NC}"
    else
        echo -e "${RED}○ Service: stopped${NC}"
    fi
else
    if systemctl is-active --quiet wire 2>/dev/null; then
        echo -e "${GREEN}● Service: running (systemd)${NC}"
    else
        echo -e "${RED}○ Service: stopped${NC}"
    fi
fi

# 2. Interface status
echo
if ifconfig $INTERFACE >/dev/null 2>&1 || ip addr show $INTERFACE >/dev/null 2>&1; then
    VPN_IP=$(ifconfig $INTERFACE 2>/dev/null | grep "inet " | awk '{print $2}')
    [ -z "$VPN_IP" ] && VPN_IP=$(ip addr show $INTERFACE 2>/dev/null | grep "inet " | awk '{print $2}' | cut -d/ -f1)
    echo -e "${GREEN}● Interface: $INTERFACE up${NC}"
    echo -e "  VPN IP: ${YELLOW}$VPN_IP${NC}"
else
    echo -e "${RED}○ Interface: $INTERFACE down${NC}"
    exit 1
fi

# 3. Server connection
if [ -n "$SERVER" ]; then
    if curl -s --connect-timeout 2 "$SERVER/peers" >/dev/null 2>&1; then
        PEER_COUNT=$(curl -s "$SERVER/peers" 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('peers',[])))" 2>/dev/null || echo "?")
        echo -e "${GREEN}● Server: connected ($PEER_COUNT peers registered)${NC}"
    else
        echo -e "${RED}○ Server: unreachable ($SERVER)${NC}"
    fi
else
    echo -e "${YELLOW}○ Server: not configured (set WIRE_SERVER_URL or wire.json)${NC}"
fi

# 4. Peer handshake status
echo
echo -e "${CYAN}Peers:${NC}"

WG_CMD="$WG"
if [[ "$OSTYPE" == "darwin"* ]] && [[ $EUID -ne 0 ]]; then
    WG_CMD="sudo $WG"
fi

$WG_CMD show $INTERFACE 2>/dev/null | while read line; do
    if [[ "$line" == peer:* ]]; then
        PEER_KEY=$(echo "$line" | awk '{print $2}')
        PEER_KEY_SHORT="${PEER_KEY:0:8}..."
    elif [[ "$line" == *"endpoint:"* ]]; then
        ENDPOINT=$(echo "$line" | awk '{print $2}')
    elif [[ "$line" == *"allowed ips:"* ]]; then
        ALLOWED=$(echo "$line" | awk '{print $3}')
        VPN=$(echo "$ALLOWED" | cut -d'/' -f1)
    elif [[ "$line" == *"latest handshake:"* ]]; then
        HS=$(echo "$line" | sed 's/.*latest handshake: //')
        if [[ "$HS" == *"second"* ]] || [[ "$HS" == *"minute"* ]]; then
            echo -e "  ${GREEN}●${NC} $VPN ($PEER_KEY_SHORT) - ${GREEN}$HS${NC}"
        else
            echo -e "  ${RED}○${NC} $VPN ($PEER_KEY_SHORT) - ${RED}no handshake${NC}"
        fi
    fi
done

# 5. Ping test
if [ -n "$SERVER" ]; then
    echo
    echo -e "${CYAN}Connectivity test:${NC}"

    PEERS=$(curl -s "$SERVER/peers" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for p in data.get('peers', [])[:5]:
        print(p.get('vpn_ip', ''))
except:
    pass
" 2>/dev/null)

    for IP in $PEERS; do
        if [[ -n "$IP" ]] && [[ "$IP" != "$VPN_IP" ]]; then
            if ping -c 1 -W 1 "$IP" >/dev/null 2>&1; then
                echo -e "  ${GREEN}●${NC} $IP - reachable"
            else
                echo -e "  ${RED}○${NC} $IP - unreachable"
            fi
        fi
    done
fi

echo
