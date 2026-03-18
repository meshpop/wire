#!/usr/bin/env python3
"""
Wire MCP Server - AI-powered VPN management
Enables Claude to help users set up and manage WireGuard mesh VPN

Run: python3 wire-mcp-server.py
"""

import json
import subprocess
import sys
import os
import socket
import urllib.request
import signal
import atexit

# === Singleton: Only one instance allowed ===
# PID_FILE removed — Claude Desktop manages process lifecycle

# cleanup_pid removed — singleton pattern disabled

# ensure_singleton removed — Claude Desktop manages process lifecycle

# ensure_singleton() disabled

# MCP Protocol implementation (line-delimited JSON)
def send_response(response):
    """Send JSON-RPC response - line-delimited JSON"""
    output = json.dumps(response)
    sys.stdout.write(output + "\n")
    sys.stdout.flush()

def read_request():
    """Read JSON-RPC request - line-delimited JSON"""
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None

def run_cmd(cmd: str, timeout: int = 30) -> str:
    """Run shell command"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return "Error: Command timed out"
    except Exception as e:
        return f"Error: {e}"

def get_public_ip() -> str:
    """Get public IP address"""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:
        return "unknown"

def get_local_ip() -> str:
    """Get local IP address"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def _find_wg_bin() -> str:
    """Find wg binary — checks multiple locations."""
    candidates = [
        "/opt/homebrew/bin/wg",   # macOS arm (Apple Silicon)
        "/usr/local/bin/wg",      # macOS x86 / manual install
        "/usr/bin/wg",            # Linux
        "wg",                     # PATH
    ]
    for c in candidates:
        r = subprocess.run(["which", c.split("/")[-1]] if c == "wg" else ["test", "-x", c],
                           capture_output=True)
        if r.returncode == 0:
            return c
    return "wg"  # fallback — let shell find it

def _find_wire_interface() -> str:
    """
    Detect the active WireGuard interface — no sudo required.

    macOS/Linux: /var/run/wireguard/*.sock  (wireguard-go userspace)
                 The socket filename IS the interface name.
    Linux kernel: ip link show | grep wg / wire
    Fallback:     wg show interfaces (may need sudo)
    Returns "" if WireGuard is not running at all.
    """
    import glob

    # ── Method 1: /var/run/wireguard/*.sock (most reliable, no sudo) ──
    sock_files = glob.glob("/var/run/wireguard/*.sock")
    if sock_files:
        # pick the first one; sort for determinism
        iface = os.path.basename(sorted(sock_files)[0]).replace(".sock", "")
        return iface

    # ── Method 2: wg show interfaces (sudo -n, non-interactive) ──────
    wg = _find_wg_bin()
    out = run_cmd(f"sudo -n {wg} show interfaces 2>/dev/null").strip()
    if out and "sudo" not in out.lower() and "password" not in out.lower() and out:
        ifaces = out.split()
        if ifaces:
            return ifaces[0]

    # ── Method 3: Linux — ip link (no sudo) ──────────────────────────
    if sys.platform != "darwin":
        ip_out = run_cmd("ip link show 2>/dev/null")
        for token in ip_out.split():
            name = token.rstrip(":")
            if name.startswith(("wg", "wire")):
                return name

    # ── Nothing found — WireGuard not running ────────────────────────
    return ""

def _wg_show(subcmd: str = "") -> str:
    """Run 'wg show <interface> [subcmd]', auto-detecting wg path and interface."""
    wg    = _find_wg_bin()
    iface = _find_wire_interface()
    cmd   = f"sudo -n {wg} show {iface}"
    if subcmd:
        cmd += f" {subcmd}"
    out = run_cmd(cmd + " 2>/dev/null")
    # If sudo -n fails (needs password), try without sudo
    if "sudo" in out.lower() or "password" in out.lower():
        out = run_cmd(f"{wg} show {iface}" + (f" {subcmd}" if subcmd else "") + " 2>/dev/null")
    return out


# Tool implementations
def wire_status():
    """Get Wire VPN status"""
    is_macos = sys.platform == "darwin"
    interface = _find_wire_interface()

    # Check if interface exists
    if is_macos:
        iface_check = run_cmd(f"ifconfig {interface} 2>/dev/null")
    else:
        iface_check = run_cmd(f"ip addr show {interface} 2>/dev/null")

    if "does not exist" in iface_check or not iface_check.strip():
        return {
            "status": "not_running",
            "message": f"Wire VPN is not running (checked interface: {interface})",
            "suggestion": "Run 'sudo python3 /opt/wire/client.py --server http://SERVER:8786' to start"
        }

    wg_show = _wg_show()
    peer_count = wg_show.count("peer:")

    # Get VPN IP
    if is_macos:
        vpn_ip = run_cmd(f"ifconfig {interface} | grep 'inet ' | awk '{{print $2}}'").strip()
    else:
        vpn_ip = run_cmd(f"ip addr show {interface} | grep 'inet ' | awk '{{print $2}}' | cut -d/ -f1").strip()

    return {
        "status": "running",
        "interface": interface,
        "vpn_ip": vpn_ip,
        "peer_count": peer_count,
        "public_ip": get_public_ip(),
        "local_ip": get_local_ip()
    }

def wire_peers():
    """List connected peers"""
    interface = _find_wire_interface()
    if not interface:
        return {"error": "Wire VPN is not running",
                "hint": "Check /var/run/wireguard/ or run: sudo wg show interfaces"}

    output = _wg_show("dump")
    if not output.strip() or "Unable" in output or "error" in output.lower():
        return {"error": f"Wire VPN not responding on interface {interface}",
                "hint": "Try: sudo wg show " + interface}

    peers = []
    lines = output.strip().split("\n")
    for line in lines[1:]:  # Skip header
        parts = line.split("\t")
        if len(parts) >= 5:
            peers.append({
                "public_key": parts[0][:16] + "...",
                "endpoint": parts[2] if parts[2] != "(none)" else "no endpoint",
                "allowed_ips": parts[3],
                "last_handshake": int(parts[4]) if parts[4] != "0" else 0
            })

    return {"peers": peers, "count": len(peers)}

def wire_install(server_url: str):
    """Install Wire VPN"""
    # Check if already installed
    if os.path.exists("/opt/wire/client.py"):
        return {"status": "already_installed", "path": "/opt/wire"}

    instructions = f"""
To install Wire VPN:

1. Download the client:
   curl -sSL {server_url}/client.py -o /tmp/client.py

2. Create directory:
   sudo mkdir -p /opt/wire
   sudo cp /tmp/client.py /opt/wire/

3. Start the VPN:
   sudo python3 /opt/wire/client.py --server {server_url}

Or use the one-liner:
   curl -sSL {server_url}/install.sh | sudo bash -s -- {server_url}
"""
    return {"status": "not_installed", "instructions": instructions}

def wire_diagnose():
    """Diagnose Wire VPN issues"""
    issues = []
    suggestions = []

    is_macos = sys.platform == "darwin"

    # Check if running as root
    if os.geteuid() != 0:
        issues.append("Not running as root")
        suggestions.append("Wire VPN requires root privileges. Run with sudo.")

    # Check WireGuard installation
    wg_path = _find_wg_bin()
    wg_check = run_cmd(f"which {wg_path} 2>/dev/null || which wg 2>/dev/null")
    if not wg_check.strip():
        issues.append("WireGuard not installed")
        if is_macos:
            suggestions.append("Install: brew install wireguard-tools wireguard-go")
        else:
            suggestions.append("Install: apt install wireguard-tools (Debian/Ubuntu)")

    # Check network connectivity
    ping_result = run_cmd("ping -c 1 -W 2 8.8.8.8 2>/dev/null")
    if "1 packets transmitted, 1" not in ping_result and "1 received" not in ping_result:
        issues.append("No internet connectivity")
        suggestions.append("Check your network connection")

    # Check if service is running
    if is_macos:
        service_check = run_cmd("launchctl list | grep wire")
    else:
        service_check = run_cmd("systemctl is-active wire 2>/dev/null")

    if not service_check.strip() or "inactive" in service_check:
        issues.append("Wire service not running")
        suggestions.append("Start with: sudo python3 /opt/wire/client.py --server http://SERVER:8786")

    if not issues:
        return {"status": "healthy", "message": "No issues found"}

    return {
        "status": "issues_found",
        "issues": issues,
        "suggestions": suggestions
    }

def wire_connect(server_url: str, port: int = 51820):
    """Connect to Wire VPN network"""
    # Check prerequisites
    diagnose = wire_diagnose()
    if "WireGuard not installed" in str(diagnose.get("issues", [])):
        return {"error": "WireGuard not installed", "diagnose": diagnose}

    # Generate command
    cmd = f"sudo python3 /opt/wire/client.py --server {server_url} --port {port}"

    return {
        "status": "ready",
        "command": cmd,
        "message": "Run the command above to connect. The VPN will run in foreground.",
        "background_tip": "To run as service, use: sudo systemctl start wire (Linux) or launchctl (macOS)"
    }

def wire_add_node(server_url: str, node_name: str, public_ip: str = "", port: int = 51820):
    """Add a new node to the Wire VPN network"""
    is_macos = sys.platform == "darwin"

    # Generate install command for the new node
    install_cmd = f"curl -sSL {server_url}/install.sh | sudo bash -s -- {server_url}"

    # If public IP provided, this is a relay node
    if public_ip:
        node_type = "relay"
        setup_notes = f"""
Relay Node Setup for {node_name}:

1. SSH to the server:
   ssh root@{public_ip}

2. Install Wire:
   {install_cmd}

3. The node will auto-register with the server.

4. Verify connection:
   wg show wire0
"""
    else:
        node_type = "client"
        setup_notes = f"""
Client Node Setup for {node_name}:

1. On the target machine, run:
   {install_cmd}

2. The node will auto-connect via relay.

3. Verify:
   ping 10.99.x.x (other nodes)
"""

    return {
        "node_name": node_name,
        "node_type": node_type,
        "server_url": server_url,
        "install_command": install_cmd,
        "setup_notes": setup_notes
    }

def wire_remove_node(node_name: str, vpn_ip: str = ""):
    """Remove a node from the Wire VPN network"""
    interface = _find_wire_interface()
    wg_path  = _find_wg_bin()

    if not interface:
        return {"error": "Wire VPN is not running — cannot remove node"}

    # Get current peers
    output = _wg_show("dump")
    if not output.strip() or "error" in output.lower():
        return {"error": f"Wire VPN not responding on interface {interface}"}

    # Find peer by VPN IP in allowed_ips
    removed = False
    lines = output.strip().split("\n")
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) >= 4:
            pub_key = parts[0]
            allowed_ips = parts[3]
            if vpn_ip and vpn_ip in allowed_ips:
                # Remove this peer
                run_cmd(f"sudo {wg_path} set {interface} peer {pub_key} remove")
                removed = True
                break

    if removed:
        return {
            "status": "removed",
            "node_name": node_name,
            "vpn_ip": vpn_ip,
            "note": "Node removed from local peer list. It will re-register on next server sync unless removed from server."
        }
    else:
        return {
            "status": "not_found",
            "node_name": node_name,
            "vpn_ip": vpn_ip,
            "note": "Node not found in current peer list"
        }

def wire_watchdog():
    """Check Wire VPN watchdog/auto-recovery status"""
    is_macos = sys.platform == "darwin"
    interface = _find_wire_interface()
    wg_path  = _find_wg_bin()

    result = {
        "watchdog_enabled": False,
        "service_status": "unknown",
        "interface": interface,
        "last_handshakes": [],
        "stale_peers": [],
        "recommendations": []
    }

    # Check service status
    if is_macos:
        service_check = run_cmd("launchctl list | grep wire")
        if "wire" in service_check:
            result["service_status"] = "running (launchd)"
            result["watchdog_enabled"] = True
    else:
        service_check = run_cmd("systemctl is-active wire 2>/dev/null").strip()
        if service_check == "active":
            result["service_status"] = "running (systemd)"
            result["watchdog_enabled"] = True
        else:
            result["service_status"] = service_check or "not running"

    # Check peer handshakes
    import time
    now = time.time()
    output = run_cmd(f"sudo {wg_path} show {interface} dump 2>/dev/null")

    if output.strip():
        lines = output.strip().split("\n")
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 5:
                allowed_ips = parts[3]
                last_hs = int(parts[4]) if parts[4] != "0" else 0

                peer_info = {
                    "allowed_ips": allowed_ips,
                    "last_handshake": last_hs,
                    "seconds_ago": int(now - last_hs) if last_hs > 0 else -1
                }
                result["last_handshakes"].append(peer_info)

                # Stale if no handshake in 3 minutes
                if last_hs == 0 or (now - last_hs) > 180:
                    result["stale_peers"].append(allowed_ips)

    # Recommendations
    if not result["watchdog_enabled"]:
        result["recommendations"].append("Enable systemd/launchd service for auto-restart")

    if result["stale_peers"]:
        result["recommendations"].append(f"{len(result['stale_peers'])} peers have stale connections - check network or restart")

    if not result["recommendations"]:
        result["recommendations"].append("All systems healthy")

    return result

# MCP Tool definitions
TOOLS = [
    {
        "name": "wire_status",
        "description": "Get Wire VPN status - shows if VPN is running, connected peers, and network info",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "wire_peers",
        "description": "List all connected VPN peers with their endpoints and connection status",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "wire_install",
        "description": "Get instructions to install Wire VPN on this machine",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {
                    "type": "string",
                    "description": "Wire server URL (e.g., http://your-server:8786)"
                }
            },
            "required": ["server_url"]
        }
    },
    {
        "name": "wire_diagnose",
        "description": "Diagnose Wire VPN issues - checks WireGuard installation, connectivity, and service status",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "wire_connect",
        "description": "Get command to connect to a Wire VPN network",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {
                    "type": "string",
                    "description": "Wire server URL (e.g., http://your-server:8786)"
                },
                "port": {
                    "type": "integer",
                    "description": "WireGuard listen port (default: 51820)",
                    "default": 51820
                }
            },
            "required": ["server_url"]
        }
    },
    {
        "name": "wire_add_node",
        "description": "Add a new node to the Wire VPN network. Returns setup instructions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {
                    "type": "string",
                    "description": "Wire server URL"
                },
                "node_name": {
                    "type": "string",
                    "description": "Name for the new node (e.g., web1, db1)"
                },
                "public_ip": {
                    "type": "string",
                    "description": "Public IP if this is a relay node (optional)"
                },
                "port": {
                    "type": "integer",
                    "description": "WireGuard port (default: 51820)",
                    "default": 51820
                }
            },
            "required": ["server_url", "node_name"]
        }
    },
    {
        "name": "wire_remove_node",
        "description": "Remove a node from the Wire VPN network",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_name": {
                    "type": "string",
                    "description": "Name of the node to remove"
                },
                "vpn_ip": {
                    "type": "string",
                    "description": "VPN IP of the node (e.g., 10.99.x.x)"
                }
            },
            "required": ["node_name"]
        }
    },
    {
        "name": "wire_watchdog",
        "description": "Check Wire VPN watchdog and auto-recovery status. Shows service health, stale peers, and recommendations.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

def handle_request(request):
    """Handle MCP request"""
    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "wire-mcp",
                    "version": "1.0.0"
                }
            }
        }

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS}
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})

        result = None
        if tool_name == "wire_status":
            result = wire_status()
        elif tool_name == "wire_peers":
            result = wire_peers()
        elif tool_name == "wire_install":
            result = wire_install(args.get("server_url", "http://localhost:8786"))
        elif tool_name == "wire_diagnose":
            result = wire_diagnose()
        elif tool_name == "wire_connect":
            result = wire_connect(
                args.get("server_url", "http://localhost:8786"),
                args.get("port", 51820)
            )
        elif tool_name == "wire_add_node":
            result = wire_add_node(
                args.get("server_url", "http://localhost:8786"),
                args.get("node_name", ""),
                args.get("public_ip", ""),
                args.get("port", 51820)
            )
        elif tool_name == "wire_remove_node":
            result = wire_remove_node(
                args.get("node_name", ""),
                args.get("vpn_ip", "")
            )
        elif tool_name == "wire_watchdog":
            result = wire_watchdog()
        else:
            result = {"error": f"Unknown tool: {tool_name}"}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
            }
        }

    elif method == "notifications/initialized":
        return None  # No response for notifications

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }

def main():
    """Main MCP server loop"""
    while True:
        try:
            request = read_request()
            if request is None:
                break

            response = handle_request(request)
            if response:
                send_response(response)

        except Exception as e:
            sys.stderr.write(f"Error: {e}\n")
            break

if __name__ == "__main__":
    main()
