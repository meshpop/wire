#!/usr/bin/env python3
"""
wire server v2.2.0 - WireGuard mesh VPN coordination server
Like Tailscale's coordination plane: nodes register, get peers, see network status.

Usage: python3 wire_server.py [HTTP_PORT]
  Default HTTP port : 8787
  UDP STUN port     : HTTP_PORT + 1  (default 8788)

API (HTTP):
  POST /register   - Register/heartbeat. Client supplies nat_port discovered via UDP STUN.
  GET  /peers      - Online peer list for WireGuard config sync
  GET  /status     - Tailscale-style full network view (online + offline)
  GET  /health     - Health check
  GET  /ip         - Return caller's public IP (TCP, quick check only)
  POST /punch      - NAT hole-punch coordination signal

UDP STUN (port HTTP_PORT+1):
  Client sends any UDP packet from its WireGuard port (51820).
  Server responds with JSON {"ip": "x.x.x.x", "port": N} — the NAT-mapped external IP:port.
  This is the only way to discover the real external UDP port.
"""

import json
import hashlib
import os
import socket as _socket
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

VERSION = "2.2.0"

# ── Configuration ─────────────────────────────────────────────────────
PEER_TTL_OFFLINE = 300      # Mark offline after 5 minutes
PEER_TTL_PURGE   = 86400    # Remove from list after 24 hours
VPN_SUBNET       = os.environ.get("WIRE_VPN_SUBNET", "10.99")
STATE_FILE       = os.environ.get("WIRE_STATE_FILE", "/etc/wire/state.json")
PORT_DEFAULT     = int(os.environ.get("WIRE_PORT", "8787"))

# ── State ─────────────────────────────────────────────────────────────
peers = {}
lock  = threading.Lock()
punch_requests = {}


# ── Helpers ───────────────────────────────────────────────────────────

def generate_vpn_ip(node_id: str) -> str:
    h = hashlib.sha256(node_id.encode()).digest()
    return f"{VPN_SUBNET}.{h[0]}.{h[1]}"


def load_state():
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
        print(f"[state] loaded {len(loaded)} peers from {STATE_FILE}")
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        pass


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with lock:
            data = dict(peers)
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except (PermissionError, OSError):
        pass


def cleanup():
    now = time.time()
    with lock:
        for nid in list(peers.keys()):
            if now - peers[nid].get("last_seen", 0) > PEER_TTL_PURGE:
                del peers[nid]


def peer_online(p: dict) -> bool:
    return time.time() - p.get("last_seen", 0) < PEER_TTL_OFFLINE


# ── UDP STUN server ───────────────────────────────────────────────────

def _run_udp_stun(stun_port: int):
    """
    Listen on UDP stun_port.
    Each incoming packet → reply with {"ip": sender_ip, "port": sender_port}.

    The client MUST send from the same port as its WireGuard listen port (e.g. 51820),
    BEFORE WireGuard starts. This gives the true NAT-mapped external UDP port.
    """
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", stun_port))
    except OSError as e:
        print(f"[stun] Cannot bind UDP :{stun_port} — {e}")
        return

    print(f"[stun] UDP STUN listening on :{stun_port}")
    while True:
        try:
            data, addr = sock.recvfrom(256)
            response = json.dumps({"ip": addr[0], "port": addr[1]}).encode()
            sock.sendto(response, addr)
        except Exception:
            pass


# ── HTTP Handler ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

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
        n = int(self.headers.get("Content-Length", 0))
        if n:
            try:
                return json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                pass
        return {}

    # ── GET ───────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        cleanup()

        if path == "/health":
            with lock:
                total  = len(peers)
                online = sum(1 for p in peers.values() if peer_online(p))
            self.send_json({"ok": True, "version": VERSION,
                            "total": total, "online": online})

        elif path == "/ip":
            # TCP public IP only — NOT for WireGuard port discovery.
            # Use UDP STUN port for that.
            self.send_json({"ip": self.client_ip()})

        elif path == "/peers":
            now = time.time()
            with lock:
                result = [p for p in peers.values() if peer_online(p)]
            self.send_json({"peers": result, "count": len(result)})

        elif path == "/status":
            now = time.time()
            with lock:
                node_list = list(peers.values())

            rows = []
            for p in sorted(node_list, key=lambda x: x.get("node_name", x["node_id"])):
                age    = now - p.get("last_seen", 0)
                status = "online" if age < PEER_TTL_OFFLINE else "offline"
                rows.append({
                    "node_name":     p.get("node_name") or p["node_id"][:12],
                    "node_id":       p["node_id"],
                    "vpn_ip":        p.get("vpn_ip", ""),
                    "public_ip":     p.get("public_ip", ""),
                    "nat_port":      p.get("nat_port", p.get("port", 51820)),
                    "lan_ip":        p.get("lan_ip", ""),
                    "wg_public_key": p.get("wg_public_key", ""),
                    "status":        status,
                    "last_seen":     p.get("last_seen", 0),
                    "last_seen_ago": int(age),
                    "registered":    p.get("registered", 0),
                })

            online_count = sum(1 for r in rows if r["status"] == "online")
            self.send_json({
                "version":   VERSION,
                "total":     len(rows),
                "online":    online_count,
                "offline":   len(rows) - online_count,
                "nodes":     rows,
                "timestamp": now,
            })

        else:
            self.send_json({"error": "not found"}, 404)

    # ── POST ──────────────────────────────────────────────────────────

    def do_POST(self):
        path = urlparse(self.path).path
        cleanup()
        body = self.read_body()

        if path == "/register":
            node_id   = body.get("node_id", "")
            node_name = body.get("node_name", "")
            wg_key    = body.get("wg_public_key", "")
            port      = int(body.get("port", 51820))
            lan_ip    = body.get("lan_ip", "")
            # nat_port: client-supplied, discovered via UDP STUN before WG started.
            # If not supplied (old client / VPS with no NAT), fall back to port.
            nat_port  = int(body.get("nat_port", port))

            if not node_id or not wg_key:
                self.send_json({"error": "node_id and wg_public_key required"}, 400)
                return

            # Optional: client can request a specific vpn_ip (e.g. to fix mis-registered IP)
            requested_vpn_ip = body.get("vpn_ip", "").strip()

            public_ip = self.client_ip()
            now       = time.time()

            with lock:
                existing = peers.get(node_id, {})
                # Priority: client-requested > existing > auto-generated
                vpn_ip   = requested_vpn_ip or existing.get("vpn_ip") or generate_vpn_ip(node_id)
                peers[node_id] = {
                    "node_id":       node_id,
                    "node_name":     node_name or existing.get("node_name", ""),
                    "vpn_ip":        vpn_ip,
                    "public_ip":     public_ip,
                    "nat_port":      nat_port,
                    "lan_ip":        lan_ip,
                    "port":          port,
                    "wg_public_key": wg_key,
                    "registered":    existing.get("registered", now),
                    "last_seen":     now,
                }

            name_tag = node_name or node_id[:12]
            print(f"[+] {name_tag:<16} vpn={vpn_ip}  pub={public_ip}  "
                  f"nat={nat_port}  lan={lan_ip or '-'}")
            save_state()

            self.send_json({
                "ok":       True,
                "vpn_ip":   vpn_ip,
                "your_ip":  public_ip,
            })

        elif path == "/punch":
            """
            NAT hole-punch coordination.
            Both nodes call /punch simultaneously; the server just counts attempts
            and flags use_relay after PUNCH_MAX_ATTEMPTS.
            """
            from_vpn = body.get("from_vpn_ip", "")
            to_vpn   = body.get("to_vpn_ip", "")
            if not from_vpn or not to_vpn:
                self.send_json({"error": "from_vpn_ip and to_vpn_ip required"}, 400)
                return

            PUNCH_TIMEOUT      = 10   # seconds
            PUNCH_MAX_ATTEMPTS = 3

            now = time.time()
            key = tuple(sorted([from_vpn, to_vpn]))
            with lock:
                pr = punch_requests.setdefault(key, {"time": now, "attempts": 0})
                if now - pr["time"] > PUNCH_TIMEOUT:
                    pr["attempts"] += 1
                    pr["time"] = now

            attempts  = punch_requests[key]["attempts"]
            use_relay = attempts >= PUNCH_MAX_ATTEMPTS
            if use_relay:
                print(f"[punch] {from_vpn} ↔ {to_vpn}  RELAY (after {attempts} attempts)")
            self.send_json({"ok": True, "use_relay": use_relay, "attempts": attempts})

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
    http_port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT_DEFAULT
    stun_port = http_port + 1

    load_state()

    # Start UDP STUN thread
    t = threading.Thread(target=_run_udp_stun, args=(stun_port,), daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", http_port), Handler)
    print(f"wire server v{VERSION}")
    print(f"  HTTP  :{http_port}  — register / peers / status / health / ip / punch")
    print(f"  UDP   :{stun_port}  — STUN (NAT port discovery)")
    print(f"  state — {STATE_FILE}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        save_state()
        print("Shutdown")


if __name__ == "__main__":
    main()
