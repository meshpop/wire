#!/bin/bash
# wire — Deploy to all servers
# Usage: ./deploy-all.sh [--servers "g1 g2 d2 v1"] [--relay g2]
#
# Prerequisites:
#   - SSH key-based auth to all servers
#   - sshpass (optional, for password-based auth)
#
# Environment variables:
#   WIRE_SSH_USER       SSH username (default: current user)
#   WIRE_RELAY_PORT     Relay server port (default: 8788)
#   WIRE_RELAY_PUBLIC   Relay public IP (auto-detected if empty)

set -e

# ==================== Configuration ====================
SERVERS="${WIRE_SERVERS:-g1 g2 d2 v1}"
RELAY_SERVER="${WIRE_RELAY:-g2}"
RELAY_PORT="${WIRE_RELAY_PORT:-8788}"
SSH_USER="${WIRE_SSH_USER:-$(whoami)}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --servers) SERVERS="$2"; shift 2 ;;
        --relay) RELAY_SERVER="$2"; shift 2 ;;
        --user) SSH_USER="$2"; shift 2 ;;
        --port) RELAY_PORT="$2"; shift 2 ;;
        *) shift ;;
    esac
done

echo "╔═══════════════════════════════════════╗"
echo "║   wire — Full Deployment              ║"
echo "╚═══════════════════════════════════════╝"
echo ""
echo "  Servers: $SERVERS"
echo "  Relay:   $RELAY_SERVER (port $RELAY_PORT)"
echo "  SSH user: $SSH_USER"
echo ""

run_ssh() {
    local host=$1; shift
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "$SSH_USER@$host" "$@"
}

copy_file() {
    local host=$1 src=$2 dst=$3
    scp -o StrictHostKeyChecking=no "$src" "$SSH_USER@$host:$dst"
}

# 1. Copy files
echo "[1/4] Copying files..."
for host in $SERVERS; do
    echo "  -> $host"
    copy_file "$host" "$SCRIPT_DIR/server.py" "/tmp/server.py"
    copy_file "$host" "$SCRIPT_DIR/client.py" "/tmp/client.py"
    run_ssh "$host" "sudo mkdir -p /opt/wire && sudo cp /tmp/server.py /tmp/client.py /opt/wire/ && sudo chmod +x /opt/wire/*.py"
done
echo ""

# 2. Configure relay server
echo "[2/4] Setting up relay ($RELAY_SERVER)..."

# Auto-detect relay public IP
if [ -z "$WIRE_RELAY_PUBLIC" ]; then
    RELAY_PUBLIC=$(run_ssh "$RELAY_SERVER" "curl -s ifconfig.me 2>/dev/null || curl -s icanhazip.com" || echo "")
    echo "  Auto-detected public IP: $RELAY_PUBLIC"
else
    RELAY_PUBLIC="$WIRE_RELAY_PUBLIC"
fi

run_ssh "$RELAY_SERVER" "sudo tee /etc/systemd/system/wire.service > /dev/null << 'SVCEOF'
[Unit]
Description=wire VPN Server
After=network.target

[Service]
Type=simple
Environment=WIRE_RELAY_PUBLIC_IP=$RELAY_PUBLIC
ExecStart=/usr/bin/python3 /opt/wire/server.py $RELAY_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF"

run_ssh "$RELAY_SERVER" "sudo systemctl daemon-reload && sudo systemctl enable wire && sudo systemctl restart wire"
echo "  Relay started: $RELAY_SERVER:$RELAY_PORT"
echo ""

# 3. Configure clients (skip relay server)
echo "[3/4] Setting up clients..."
PORT=51830
for host in $SERVERS; do
    if [ "$host" = "$RELAY_SERVER" ]; then
        continue
    fi

    echo "  -> $host (port $PORT)"

    # Determine server URL (use VPN IP if available, else public IP)
    SERVER_URL="http://${RELAY_PUBLIC}:${RELAY_PORT}"

    run_ssh "$host" "sudo tee /etc/systemd/system/wire.service > /dev/null << SVCEOF
[Unit]
Description=wire VPN Client
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/wire/client.py --server $SERVER_URL --port $PORT --config /etc/wire
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF"

    run_ssh "$host" "sudo mkdir -p /etc/wire && sudo systemctl daemon-reload && sudo systemctl enable wire && sudo systemctl restart wire"
    PORT=$((PORT + 1))
done
echo ""

# 4. SSH key exchange
echo "[4/4] SSH key exchange..."
for host in $SERVERS; do
    echo "  -> $host"
    run_ssh "$host" "test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''"

    PUB_KEY=$(run_ssh "$host" "cat ~/.ssh/id_ed25519.pub")

    for target in $SERVERS; do
        if [ "$target" != "$host" ]; then
            run_ssh "$target" "grep -q '${PUB_KEY}' ~/.ssh/authorized_keys 2>/dev/null || echo '${PUB_KEY}' >> ~/.ssh/authorized_keys"
        fi
    done
done
echo ""

# 5. Status check
echo "═══════════════════════════════════════"
echo "Deployment complete! Checking status..."
echo ""
sleep 3

for host in $SERVERS; do
    STATUS=$(run_ssh "$host" "sudo systemctl is-active wire" 2>/dev/null || echo "unknown")
    VPN_IP=$(run_ssh "$host" "ip addr show wire0 2>/dev/null | grep 'inet ' | awk '{print \$2}'" || echo "-")
    echo "  $host: $STATUS ($VPN_IP)"
done
echo ""
