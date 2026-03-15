#!/bin/bash
#
# wire one-liner install (VPN + monitoring agent)
# curl -sSL http://<server>:8786/install.sh | sudo bash -s -- http://<server>:8786
#
set -e

SERVER="${1:-http://localhost:8786}"
DASHBOARD="${2:-http://localhost:8800}"
INSTALL_DIR="/opt/wire"
AGENT_DIR="/opt/server-agent"
SERVICE_NAME="wire"

echo "╔═══════════════════════════════════════╗"
echo "║   wire + agent install        ║"
echo "╚═══════════════════════════════════════╝"

# Detect OS
if [[ "$OSTYPE" == "darwin"* ]]; then
    IS_MAC=true
    echo "[*] macOS detected"
else
    IS_MAC=false
    echo "[*] Linux detected"
fi

# 1. Install WireGuard
echo "[1/5] Checking WireGuard..."
if ! command -v wg &> /dev/null; then
    if [ "$IS_MAC" = true ]; then
        echo "  → Installing wireguard-tools via brew"
        brew install wireguard-tools 2>/dev/null || echo "brew not found, manual install needed"
    elif [ -f /etc/debian_version ]; then
        apt-get update -qq && apt-get install -y wireguard >/dev/null
    elif [ -f /etc/redhat-release ]; then
        yum install -y wireguard-tools >/dev/null
    fi
fi
echo "  ✓ WireGuard ready"

# 2. Download VPN client
echo "[2/5] Downloading VPN client..."
mkdir -p $INSTALL_DIR
curl -sSL $SERVER/client.py -o $INSTALL_DIR/client.py
chmod +x $INSTALL_DIR/client.py
echo "  ✓ $INSTALL_DIR/client.py"

# 3. Download monitoring agent
echo "[3/5] Downloading monitoring agent..."
mkdir -p $AGENT_DIR
# Download agent from dashboard (available after VPN connect)
# Use placeholder until VPN is connected
curl -sSL $SERVER/agent.py -o $AGENT_DIR/agent-reporter.py 2>/dev/null || \
    echo '#!/usr/bin/env python3
# Placeholder - updated after VPN connect
import time
while True:
    time.sleep(60)
' > $AGENT_DIR/agent-reporter.py
chmod +x $AGENT_DIR/agent-reporter.py
echo "  ✓ $AGENT_DIR/agent-reporter.py"

# 4. Determine port
if [ -f /etc/wire/port ]; then
    PORT=$(cat /etc/wire/port)
else
    PORT=$((51820 + RANDOM % 100))
    mkdir -p /etc/wire
    echo $PORT > /etc/wire/port
fi
echo "[4/5] WireGuard port: $PORT"

# 5. Register services
echo "[5/5] Registering services..."

if [ "$IS_MAC" = true ]; then
    # macOS: launchd plist
    cat > /Library/LaunchDaemons/com.meshpop.wire.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.meshpop.wire</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$INSTALL_DIR/client.py</string>
        <string>--server</string>
        <string>$SERVER</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/wire.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/wire.log</string>
</dict>
</plist>
EOF
    # Agent plist
    cat > /Library/LaunchDaemons/com.server-agent.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.server-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$AGENT_DIR/agent-reporter.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/server-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/server-agent.log</string>
</dict>
</plist>
EOF
    launchctl load /Library/LaunchDaemons/com.meshpop.wire.plist 2>/dev/null || true
    launchctl load /Library/LaunchDaemons/com.server-agent.plist 2>/dev/null || true
    echo "  ✓ launchd services registered"
else
    # Linux: systemd
    # VPN service
    cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=wire VPN
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/client.py --server $SERVER --port $PORT --config /etc/wire
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    # Agent service
    cat > /etc/systemd/system/server-agent.service << EOF
[Unit]
Description=Server Monitoring Agent
After=network.target $SERVICE_NAME.service
Wants=$SERVICE_NAME.service

[Service]
Type=simple
ExecStartPre=/bin/sleep 10
ExecStart=/usr/bin/python3 $AGENT_DIR/agent-reporter.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

    # IP forwarding (for relay)
    echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-wire.conf
    sysctl -w net.ipv4.ip_forward=1 > /dev/null

    # iptables rules
    if command -v iptables &> /dev/null; then
        iptables -C FORWARD -i wire0 -o wire0 -j ACCEPT 2>/dev/null || \
        iptables -I FORWARD -i wire0 -o wire0 -j ACCEPT
    fi

    systemctl daemon-reload
    systemctl enable $SERVICE_NAME server-agent
    systemctl restart $SERVICE_NAME
    sleep 5
    systemctl restart server-agent
    echo "  ✓ systemd services registered"
fi

# Wait for VPN and update agent
echo ""
echo "[*] Waiting for VPN connection..."
sleep 10

# Try downloading latest agent after VPN connect
if curl -sSL --connect-timeout 5 $DASHBOARD/agent.py -o $AGENT_DIR/agent-reporter.py.new 2>/dev/null; then
    mv $AGENT_DIR/agent-reporter.py.new $AGENT_DIR/agent-reporter.py
    chmod +x $AGENT_DIR/agent-reporter.py
    echo "  ✓ Latest agent downloaded"
    if [ "$IS_MAC" = false ]; then
        systemctl restart server-agent
    fi
fi

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║         Install Complete!             ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# Check VPN IP
if [ "$IS_MAC" = true ]; then
    VPN_IP=$(ifconfig 2>/dev/null | grep 'inet 10.99' | awk '{print $2}' | head -1)
else
    VPN_IP=$(ip addr show wire0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
fi

if [ -n "$VPN_IP" ]; then
    echo "VPN IP: $VPN_IP"
    echo "Dashboard: http://<dashboard-ip>:8800/"
else
    echo "VPN connecting... check shortly:"
    echo "  systemctl status $SERVICE_NAME"
fi
echo ""
echo "Check status:"
echo "  wg show"
echo "  systemctl status $SERVICE_NAME server-agent"
