#!/usr/bin/env python3
"""
wire server - Simple WireGuard mesh VPN coordination server
Usage: python3 server.py [PORT]
"""

import json
import hashlib
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Configuration
PEER_TTL = 120  # Expire after 2 minutes
PASSWORD = ""   # Empty = no auth
PUNCH_TIMEOUT = 10  # Seconds to wait after hole-punch attempt
PUNCH_MAX_ATTEMPTS = 3  # Max hole-punch attempts before relay fallback

# VPN subnet prefix (configurable)
VPN_SUBNET = os.environ.get("WIRE_VPN_SUBNET", "10.99")

# Relay server info (auto-detected from first registered relay, or set via env)
RELAY_VPN_IP = os.environ.get("WIRE_RELAY_VPN_IP", "")
RELAY_PUBLIC_IP = os.environ.get("WIRE_RELAY_PUBLIC_IP", "")
RELAY_PORT = int(os.environ.get("WIRE_RELAY_PORT", "51820"))

# State
peers = {}  # {network: {node_id: peer_info}}
punch_requests = {}  # {(from_ip, to_ip): {"time": timestamp, "attempts": count}}
lock = threading.Lock()


def generate_vpn_ip(node_id: str) -> str:
    """Generate VPN IP from node_id hash"""
    h = hashlib.sha256(node_id.encode()).digest()
    return f"{VPN_SUBNET}.{h[0]}.{h[1]}"


def cleanup():
    """Remove expired peers"""
    now = time.time()
    with lock:
        for network in list(peers.keys()):
            for nid in list(peers[network].keys()):
                if peers[network][nid]["expires"] < now:
                    del peers[network][nid]
            if not peers[network]:
                del peers[network]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def get_client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def send_file(self, path: str, content_type: str = "text/plain"):
        """Serve a local file"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, path)
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_json({"error": f"file not found: {path}"}, 404)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        cleanup()

        if parsed.path == "/health":
            with lock:
                total = sum(len(p) for p in peers.values())
            self.send_json({"ok": True, "peers": total})

        elif parsed.path == "/peers":
            network = params.get("network", ["default"])[0]
            with lock:
                result = list(peers.get(network, {}).values())
            self.send_json({"peers": result})

        elif parsed.path == "/install.sh":
            self.send_file("install.sh", "text/x-shellscript")

        elif parsed.path == "/install":
            self.send_file("meshpop-install.sh", "text/x-shellscript")

        elif parsed.path == "/client.py":
            self.send_file("client.py", "text/x-python")

        elif parsed.path == "/agent.py":
            self.send_file("agent.py", "text/x-python")

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        cleanup()

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self.send_json({"error": "invalid json"}, 400)
            return

        if parsed.path == "/register":
            network = body.get("network", "default")
            node_id = body.get("node_id", "")
            port = body.get("port", 51820)
            wg_key = body.get("wg_public_key", "")
            lan_ip = body.get("lan_ip", "")

            if not node_id or not wg_key:
                self.send_json({"error": "node_id and wg_public_key required"}, 400)
                return

            public_ip = self.get_client_ip()
            vpn_ip = generate_vpn_ip(node_id)
            now = time.time()

            peer_info = {
                "node_id": node_id,
                "public_ip": public_ip,
                "port": port,
                "wg_public_key": wg_key,
                "lan_ip": lan_ip,
                "vpn_ip": vpn_ip,
                "registered": now,
                "expires": now + PEER_TTL,
            }

            with lock:
                peers.setdefault(network, {})[node_id] = peer_info

            print(f"[+] {node_id[:12]}... @ {public_ip}:{port} (LAN: {lan_ip or '-'}) VPN: {vpn_ip}")
            self.send_json({"ok": True, "your_ip": public_ip, "vpn_ip": vpn_ip})

        elif parsed.path == "/punch":
            from_ip = body.get("from_vpn_ip", "")
            to_ip = body.get("to_vpn_ip", "")

            if not from_ip or not to_ip:
                self.send_json({"error": "from_vpn_ip and to_vpn_ip required"}, 400)
                return

            now = time.time()
            key = tuple(sorted([from_ip, to_ip]))

            with lock:
                if key not in punch_requests:
                    punch_requests[key] = {"time": now, "attempts": 1}
                else:
                    pr = punch_requests[key]
                    if now - pr["time"] > PUNCH_TIMEOUT:
                        pr["attempts"] += 1
                        pr["time"] = now

                    # Max attempts exceeded — use relay
                    if pr["attempts"] >= PUNCH_MAX_ATTEMPTS:
                        if RELAY_PUBLIC_IP:
                            print(f"[R] {from_ip} -> {to_ip} via relay (after {pr['attempts']} attempts)")
                            self.send_json({
                                "ok": True,
                                "use_relay": True,
                                "relay_endpoint": f"{RELAY_PUBLIC_IP}:{RELAY_PORT}",
                                "relay_vpn_ip": RELAY_VPN_IP,
                            })
                        else:
                            self.send_json({
                                "ok": False,
                                "error": "No relay configured. Set WIRE_RELAY_PUBLIC_IP env var.",
                            })
                        return

            print(f"[P] {from_ip} -> {to_ip} punch #{punch_requests[key]['attempts']}")
            self.send_json({"ok": True, "use_relay": False, "attempt": punch_requests[key]["attempts"]})

        else:
            self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"""
╔═══════════════════════════════════════╗
║     wire server                       ║
║     Simple WireGuard Mesh VPN         ║
╚═══════════════════════════════════════╝

Listening on port {port}

API:
  POST /register  - Register peer (node_id, wg_public_key, port, lan_ip)
  GET  /peers     - List peers
  GET  /health    - Health check
  GET  /install   - Download install script

Environment variables:
  WIRE_RELAY_PUBLIC_IP  - Relay server public IP
  WIRE_RELAY_VPN_IP     - Relay server VPN IP
  WIRE_RELAY_PORT       - Relay WireGuard port (default: 51820)
  WIRE_VPN_SUBNET       - VPN subnet prefix (default: 10.99)
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown")


if __name__ == "__main__":
    main()
