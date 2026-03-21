#!/usr/bin/env python3
"""
wire MCP server v2.0.0 - AI interface for wire VPN

All logic lives in wire_client.py — this file is just an MCP wrapper.
CLI and MCP call the SAME functions: cmd_status, cmd_up, cmd_down, cmd_peers, cmd_ping, cmd_install.

Run: python3 wire_mcp_server.py
"""

import json
import os
import sys
import socket

# ── Import core from wire_client ──────────────────────────────────────
# wire_mcp_server.py sits next to wire_client.py
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from wire_client import (
        cmd_status,
        cmd_up,
        cmd_down,
        cmd_peers,
        cmd_ping,
        cmd_install,
        load_config,
        generate_node_id,
        generate_vpn_ip,
        VERSION as CLIENT_VERSION,
    )
    _CLIENT_OK = True
except ImportError as e:
    _CLIENT_OK = False
    _IMPORT_ERR = str(e)

VERSION = "2.0.0"

# ── MCP protocol ──────────────────────────────────────────────────────

def send_response(resp):
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


def read_request():
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


def _ok(data: dict) -> dict:
    return {"type": "text", "text": json.dumps(data, indent=2)}


def _err(msg: str) -> dict:
    return {"type": "text", "text": json.dumps({"ok": False, "error": msg}, indent=2)}


# ── Tool implementations ──────────────────────────────────────────────

def tool_wire_status(args: dict) -> dict:
    """
    Query coordination server for tailscale-style network status.
    Shows all nodes (online + offline), VPN IPs, last seen.
    """
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")

    server = args.get("server_url") or None
    data   = cmd_status(server)
    return _ok(data)


def tool_wire_up(args: dict) -> dict:
    """Bring up WireGuard VPN tunnel and register with coordination server."""
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")
    if os.geteuid() != 0:
        return _err("wire up requires root privileges")

    result = cmd_up(
        name=args.get("node_name"),
        server=args.get("server_url"),
        port=args.get("port", 51820),
    )
    return _ok(result)


def tool_wire_down(args: dict) -> dict:
    """Tear down WireGuard VPN tunnel."""
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")
    if os.geteuid() != 0:
        return _err("wire down requires root privileges")

    result = cmd_down()
    return _ok(result)


def tool_wire_peers(args: dict) -> dict:
    """List all peers currently registered on the coordination server."""
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")

    server = args.get("server_url") or None
    result = cmd_peers(server)
    return _ok(result)


def tool_wire_ping(args: dict) -> dict:
    """Ping a peer by node name or VPN IP."""
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")

    target = args.get("target", "")
    if not target:
        return _err("target required (node name or VPN IP)")

    result = cmd_ping(
        target=target,
        server_url=args.get("server_url") or None,
        count=args.get("count", 4),
    )
    return _ok(result)


def tool_wire_install(args: dict) -> dict:
    """Check WireGuard installation and return install instructions if needed."""
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")

    result = cmd_install()
    return _ok(result)


def tool_wire_diagnose(args: dict) -> dict:
    """Diagnose VPN issues: WireGuard install, connectivity, server reachability."""
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")

    import subprocess
    issues      = []
    suggestions = []
    checks      = {}

    # WireGuard installed?
    from wire_client import find_bin, _run
    wg = find_bin("wg")
    out, _, rc = _run(f"{wg} --version 2>/dev/null || {wg} version 2>/dev/null")
    checks["wireguard"] = "ok" if rc == 0 else "not_found"
    if rc != 0:
        issues.append("WireGuard not installed")
        if sys.platform == "darwin":
            suggestions.append("brew install wireguard-tools wireguard-go")
        else:
            suggestions.append("apt install wireguard-tools  OR  dnf install wireguard-tools")

    # Config + server reachable?
    cfg = load_config()
    server_url = args.get("server_url") or cfg.get("server_url", "")
    if server_url:
        from wire_client import api_get
        health = api_get(server_url, "/health", timeout=3)
        checks["server"] = "ok" if health.get("ok") else f"unreachable ({health.get('error','')})"
        if not health.get("ok"):
            issues.append(f"Cannot reach coordination server: {server_url}")
            suggestions.append("Check wire server is running: python3 wire_server.py")
    else:
        checks["server"] = "no server configured"
        issues.append("No server URL configured")
        suggestions.append("Run: wire up --server http://IP:8787 --name MYNAME")

    # Interface up?
    from wire_client import _wg_iface
    iface = _wg_iface()
    checks["interface"] = iface if iface else "not running"
    if not iface:
        issues.append("WireGuard interface not running")
        suggestions.append("Run: sudo wire up")

    # Node config
    node_id   = cfg.get("node_id", generate_node_id())
    node_name = cfg.get("node_name", socket.gethostname())
    vpn_ip    = cfg.get("vpn_ip", generate_vpn_ip(node_id))
    checks["node"] = {"name": node_name, "vpn_ip": vpn_ip}

    return _ok({
        "ok":          len(issues) == 0,
        "checks":      checks,
        "issues":      issues,
        "suggestions": suggestions,
    })


def tool_wire_watchdog(args: dict) -> dict:
    """
    Show watchdog/health status: last peer handshakes, stale connections,
    daemon running, service status.
    """
    if not _CLIENT_OK:
        return _err(f"wire_client import failed: {_IMPORT_ERR}")

    from wire_client import find_bin, _wg_iface, _run
    import time

    wg    = find_bin("wg")
    iface = _wg_iface()
    now   = time.time()

    result = {
        "interface":   iface or "not running",
        "handshakes":  [],
        "stale_peers": [],
        "service":     "unknown",
        "recommendations": [],
    }

    # Service status
    if sys.platform == "darwin":
        out, _, _ = _run("launchctl list 2>/dev/null | grep -i wire")
        result["service"] = "launchd: running" if "wire" in out else "launchd: not found"
    else:
        out, _, rc = _run("systemctl is-active wire-client 2>/dev/null")
        result["service"] = f"systemd: {out.strip() or 'not found'}"

    # Peer handshakes
    if iface:
        out, _, _ = _run(f"sudo -n {wg} show {iface} dump 2>/dev/null")
        if out:
            for line in out.strip().split("\n")[1:]:
                parts = line.split("\t")
                if len(parts) >= 5:
                    allowed_ips = parts[3]
                    last_hs     = int(parts[4]) if parts[4] != "0" else 0
                    ago         = int(now - last_hs) if last_hs > 0 else -1
                    entry       = {"allowed_ips": allowed_ips, "last_handshake_ago": ago}
                    result["handshakes"].append(entry)
                    if last_hs == 0 or ago > 180:
                        result["stale_peers"].append(allowed_ips)

    if result["stale_peers"]:
        result["recommendations"].append(
            f"{len(result['stale_peers'])} stale peers — check network or restart"
        )
    if not iface:
        result["recommendations"].append("Interface not running — run: sudo wire up")
    if not result["recommendations"]:
        result["recommendations"].append("All healthy")

    return _ok(result)


# ── Tool definitions ──────────────────────────────────────────────────

TOOLS = [
    {
        "name": "wire_status",
        "description": (
            "Show wire VPN network status — queries the coordination server and displays "
            "all nodes (online + offline) with their VPN IPs, public IPs, and last-seen time. "
            "Like `tailscale status`. Works even if local WireGuard is down."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {"type": "string", "description": "Override server URL (uses config if omitted)"}
            },
            "required": []
        }
    },
    {
        "name": "wire_up",
        "description": "Bring up WireGuard VPN tunnel. Requires root. Registers with server, syncs peers, starts daemon.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {"type": "string", "description": "Coordination server URL (e.g. http://v1.example.com:8787)"},
                "node_name":  {"type": "string", "description": "Node name (e.g. g1, mypc). Defaults to hostname."},
                "port":       {"type": "integer", "description": "WireGuard listen port (default 51820)", "default": 51820}
            },
            "required": []
        }
    },
    {
        "name": "wire_down",
        "description": "Tear down WireGuard VPN tunnel and stop daemon. Requires root.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "wire_peers",
        "description": "List all peers registered on the coordination server (not just local WireGuard peers).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {"type": "string", "description": "Override server URL"}
            },
            "required": []
        }
    },
    {
        "name": "wire_ping",
        "description": "Ping a VPN peer by node name (e.g. 'g1') or VPN IP (e.g. '10.99.1.5').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target":     {"type": "string", "description": "Node name or VPN IP"},
                "server_url": {"type": "string", "description": "Override server URL"},
                "count":      {"type": "integer", "description": "Ping count (default 4)", "default": 4}
            },
            "required": ["target"]
        }
    },
    {
        "name": "wire_install",
        "description": "Check if WireGuard is installed. Returns install instructions if not.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "wire_diagnose",
        "description": "Diagnose wire VPN issues: WireGuard install, server connectivity, interface state, node config.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server_url": {"type": "string", "description": "Override server URL"}
            },
            "required": []
        }
    },
    {
        "name": "wire_watchdog",
        "description": "Show VPN health: peer handshakes, stale connections, daemon/service status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
]

TOOL_MAP = {
    "wire_status":   tool_wire_status,
    "wire_up":       tool_wire_up,
    "wire_down":     tool_wire_down,
    "wire_peers":    tool_wire_peers,
    "wire_ping":     tool_wire_ping,
    "wire_install":  tool_wire_install,
    "wire_diagnose": tool_wire_diagnose,
    "wire_watchdog": tool_wire_watchdog,
}

# ── MCP request handler ───────────────────────────────────────────────

def handle(request: dict):
    method  = request.get("method", "")
    params  = request.get("params", {})
    req_id  = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wire-mcp", "version": VERSION},
            }
        }

    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        fn   = TOOL_MAP.get(name)
        if fn:
            content = fn(args)
        else:
            content = _err(f"Unknown tool: {name}")

        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [content]}
        }

    elif method == "notifications/initialized":
        return None

    else:
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }


def main():
    while True:
        try:
            req = read_request()
            if req is None:
                break
            resp = handle(req)
            if resp:
                send_response(resp)
        except Exception as e:
            sys.stderr.write(f"Error: {e}\n")
            break


if __name__ == "__main__":
    main()
