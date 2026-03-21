#!/usr/bin/env python3
"""
wire server v2.0.0 - WireGuard mesh VPN coordination server
Like Tailscale's coordination plane: nodes register, get peers, see network status.

Usage: python3 wire_server.py [PORT]

API:
  POST /register        - Register/heartbeat (node_id, node_name, wg_public_key, port, lan_ip)
  GET  /peers           - Get peer list for WireGuard config sync
  GET  /status          - Tailscale-style network status (all nodes, online/offline)
  GET  /health          - Health check
  POST /punch           - NAT hole-punch coordination
"""

import json
import hashlib
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

VERSION = "2.0.0"

# ── Configuration ────────────────────────────────────────────────────
PEER_TTL_OFFLINE = 300      # Mark offline after 5 minutes
PEER_TTL_PURGE   = 86400    # Remove from list after 24 hours
VPN_SUBNET       = os.environ.get("WIRE_VPN_SUBNET", "10.99")
STATE_FILE       = os.environ.get("WIRE_STATE_FILE", "/etc/wire/state.json")
PORT_DEFAULT     = int(os.environ.get("WIRE_PORT", "8787"))

# ── State ─────────────────────────────────────────────────────────────
# peers[node_id] = { node_id, node_name, vpn_ip, public_ip, lan_ip,
#                    port, wg_public_key, registered, last_seen }
peers = {}
lock  = threading.Lock()
punch_requests = {}  # {(ip_a, ip_b): {time, attempts}}


# ── Helpers ───────────────────────────────────────────────────────────

def generate_vpn_ip(node_id: str) -> str:
    h = hashlib.sha256(node_id.encode()).digest()
    return f"{VPN_SUBNET}.{h[0]}.{h[1]}"


def load_state():
    """Load persisted state from disk."""
    global peers
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE) as f:
            data = json.load(f)
        now = time.time()
        loaded = {
            nid: p for nid, p in data.items()
            if now - p.get("last_seen", 0) < PEER_TTL_PURGE
        }
        peers.update(loaded)
        print(f"[state] Loaded {len(loaded)} peers from {STATE_FILE}")
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        pass


def save_state():
    """Persist state to disk."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with lock:
            data = dict(peers)
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except (PermissionError, OSError):
        pass  # /etc/wire may not be writable on dev machines


def cleanup():
    """Purge nodes not seen in 24h."""
    now = time.time()
    with lock:
        for nid in list(peers.keys()):
            if now - peers[nid].get("last_seen", 0) > PEER_TTL_PURGE:
                del peers[nid]


def peer_status(p: dict) -> str:
    """Return 'online' or 'offline' based on last_seen."""
    age = time.time() - p.get("last_seen", 0)
    return "online" if age < PEER_TTL_OFFLINE else "offline"


# ── HTTP Handler ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # quiet by default

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        return xff.split(",")[0].strip() if xff else self.client_address[0]

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                pass
        return {}

    # ── GET ───────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        cleanup()

        if parsed.path == "/health":
            with lock:
                total   = len(peers)
                online  = sum(1 for p in peers.values() if peer_status(p) == "online")
            self.send_json({"ok": True, "version": VERSION, "total": total, "online": online})

        elif parsed.path == "/peers":
            """Return peer list for WireGuard config sync (client daemon uses this)."""
            now = time.time()
            with lock:
                result = [
                    p for p in peers.values()
                    if now - p.get("last_seen", 0) < PEER_TTL_OFFLINE
                ]
            self.send_json({"peers": result, "count": len(result)})

        elif parsed.path == "/status":
            """Tailscale-style: all nodes, online + offline, sorted by name."""
            now = time.time()
            with lock:
                node_list = list(peers.values())

            rows = []
            for p in sorted(node_list, key=lambda x: x.get("node_name", x["node_id"])):
                age = now - p.get("last_seen", 0)
                status = "online" if age < PEER_TTL_OFFLINE else "offline"
                rows.append({
                    "node_name":    p.get("node_name") or p["node_id"][:12],
                    "node_id":      p["node_id"],
                    "vpn_ip":       p.get("vpn_ip", ""),
                    "public_ip":    p.get("public_ip", ""),
                    "lan_ip":       p.get("lan_ip", ""),
                    "port":         p.get("port", 51820),
                    "wg_public_key": p.get("wg_public_key", ""),
                    "status":       status,
                    "last_seen":    p.get("last_seen", 0),
                    "last_seen_ago": int(age),
                    "registered":   p.get("registered", 0),
                })

            online_count  = sum(1 for r in rows if r["status"] == "online")
            offline_count = len(rows) - online_count
            self.send_json({
                "version":       VERSION,
                "total":         len(rows),
                "online":        online_count,
                "offline":       offline_count,
                "nodes":         rows,
                "timestamp":     now,
            })

        else:
            self.send_json({"error": "not found"}, 404)

    # ── POST ──────────────────────────────────────────────────────────

    def do_POST(self):
        parsed = urlparse(self.path)
        cleanup()
        body = self.read_body()

        if parsed.path == "/register":
            node_id  = body.get("node_id", "")
            node_name = body.get("node_name", "")
            port     = body.get("port", 51820)
            wg_key   = body.get("wg_public_key", "")
            lan_ip   = body.get("lan_ip", "")

            if not node_id or not wg_key:
                self.send_json({"error": "node_id and wg_public_key required"}, 400)
                return

            public_ip = self.client_ip()
            now       = time.time()

            with lock:
                existing  = peers.get(node_id, {})
                vpn_ip    = existing.get("vpn_ip") or generate_vpn_ip(node_id)
                peers[node_id] = {
                    "node_id":       node_id,
                    "node_name":     node_name or existing.get("node_name", ""),
                    "vpn_ip":        vpn_ip,
                    "public_ip":     public_ip,
                    "lan_ip":        lan_ip,
                    "port":          port,
                    "wg_public_key": wg_key,
                    "registered":    existing.get("registered", now),
                    "last_seen":     now,
                }

            name_display = node_name or node_id[:12]
            print(f"[+] {name_display} {vpn_ip} {public_ip}:{port} (LAN:{lan_ip or '-'})")
            save_state()

            self.send_json({
                "ok":       True,
                "vpn_ip":   vpn_ip,
                "your_ip":  public_ip,
            })

        elif parsed.path == "/punch":
            """NAT hole-punch coordination."""
            from_ip = body.get("from_vpn_ip", "")
            to_ip   = body.get("to_vpn_ip", "")
            if not from_ip or not to_ip:
                self.send_json({"error": "from_vpn_ip and to_vpn_ip required"}, 400)
                return

            now = time.time()
            key = tuple(sorted([from_ip, to_ip]))
            PUNCH_TIMEOUT      = 10
            PUNCH_MAX_ATTEMPTS = 3

            with lock:
                pr = punch_requests.setdefault(key, {"time": now, "attempts": 0})
                if now - pr["time"] > PUNCH_TIMEOUT:
                    pr["attempts"] += 1
                    pr["time"] = now

            attempts = punch_requests[key]["attempts"]
            if attempts >= PUNCH_MAX_ATTEMPTS:
                print(f"[punch] {from_ip} → {to_ip} RELAY fallback (after {attempts} attempts)")
                self.send_json({"ok": True, "use_relay": True, "attempts": attempts})
            else:
                print(f"[punch] {from_ip} → {to_ip} attempt #{attempts}")
                self.send_json({"ok": True, "use_relay": False, "attempts": attempts})

        else:
            self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT_DEFAULT
    load_state()

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"wire server v{VERSION}  port={port}  state={STATE_FILE}")
    print(f"  POST /register  - node heartbeat")
    print(f"  GET  /peers     - peer list for WG sync")
    print(f"  GET  /status    - tailscale-style network view")
    print(f"  GET  /health    - health check")
    print(f"  POST /punch     - NAT hole-punch coordination")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        save_state()
        print("Shutdown")


if __name__ == "__main__":
    main()
