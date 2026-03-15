#!/bin/bash
# wire macOS setup script
# Usage: sudo ./setup-mac.sh <server-url> [--port PORT]
#
# Example: sudo ./setup-mac.sh http://your-server:8786

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Error: Run with sudo"
    echo "  sudo $0 <server-url>"
    exit 1
fi

SERVER_URL="${1:-}"
WG_PORT="${WIRE_PORT:-51820}"

# Parse arguments
shift 2>/dev/null || true
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) WG_PORT="$2"; shift 2 ;;
        *) shift ;;
    esac
done

if [ -z "$SERVER_URL" ]; then
    echo "Error: Server URL required"
    echo "  sudo $0 http://<server>:8786"
    exit 1
fi

INSTALL_DIR="/opt/wire"
CONFIG_DIR="/etc/wire"

echo "╔═══════════════════════════════════════╗"
echo "║   wire macOS Setup                    ║"
echo "╚═══════════════════════════════════════╝"
echo ""
echo "  Server: $SERVER_URL"
echo "  Port:   $WG_PORT"
echo ""

# Create directories
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" /etc/wireguard

# Copy client
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/client.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/client.py"

# Create launchd plist
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
        <string>/opt/wire/client.py</string>
        <string>--server</string>
        <string>${SERVER_URL}</string>
        <string>--port</string>
        <string>${WG_PORT}</string>
        <string>--config</string>
        <string>/etc/wire</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/var/log/wire.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/wire.log</string>
</dict>
</plist>
EOF

# Restart service
launchctl unload /Library/LaunchDaemons/com.meshpop.wire.plist 2>/dev/null || true
launchctl load /Library/LaunchDaemons/com.meshpop.wire.plist

echo ""
echo "Installation complete!"
echo ""
echo "  Status: sudo launchctl list | grep wire"
echo "  Logs:   tail -f /var/log/wire.log"
echo "  Stop:   sudo launchctl unload /Library/LaunchDaemons/com.meshpop.wire.plist"
