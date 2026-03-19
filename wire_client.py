#!/usr/bin/env python3
"""
wire client - Autonomous WireGuard mesh VPN
Run: python3 client.py --server http://YOUR_SERVER:8787

Features:
  - Direct connection priority (P2P)
  - Multiple relay fallback
  - Nodes with direct connectivity can communicate without relay
  - Auto recovery
"""

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

INTERFACE = "wire0"
REFRESH_INTERVAL = 30
HANDSHAKE_TIMEOUT = 90  # Try relay if no handshake
RETRY_DIRECT_INTERVAL = 300  # 5min retry direct connection

# Relay candidates and server URLs loaded from config file
# Config: /etc/meshpop/wire.json or ~/.config/wire/config.json
# Format: {"relay_candidates": [...], "server_urls": ["http://host:port", ...]}
CONFIG_PATHS = [
    "/etc/meshpop/wire.json",
    os.path.expanduser("~/.config/wire/config.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "wire.json"),
]

def load_wire_config():
    """Load relay and server config from file"""
    for p in CONFIG_PATHS:
        try:
            with open(p) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {}

_config = load_wire_config()
RELAY_CANDIDATES = _config.get("relay_candidates", [])
SERVER_URLS = _config.get("server_urls", [])

# macOS WireGuard path (Homebrew)
WG_PATH = "/opt/homebrew/bin/wg" if sys.platform == "darwin" else "wg"


def run(cmd: str, check=True) -> str:
    """Run shell command"""
    if sys.platform == "darwin":
        cmd = cmd.replace("wg ", f"{WG_PATH} ")
        cmd = cmd.replace("|wg ", f"|{WG_PATH} ")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{r.stderr}")
    return r.stdout.strip()


def detect_lan_ip() -> str:
    """Detect LAN IP"""
    if sys.platform == "darwin":
        for iface in ["en0", "en1", "en8"]:
            try:
                r = subprocess.run(["ifconfig", iface], capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    for line in r.stdout.split("\n"):
                        if "inet " in line and "127.0.0.1" not in line:
                            parts = line.strip().split()
                            idx = parts.index("inet") + 1
                            ip = parts[idx]
                            if ip.startswith("192.168.") or (ip.startswith("10.") and not ip.startswith("10.99.")):
                                return ip
            except (subprocess.SubprocessError, OSError) as e:
                pass  # e silenced
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            for ip in r.stdout.split():
                if ip.startswith("192.168.") or (ip.startswith("10.") and not ip.startswith("10.99.")):
                    return ip
    except (subprocess.SubprocessError, OSError) as e:
        pass  # e silenced
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("10.99."):
            return ip
    except OSError:
        pass  # safe to ignore
    return ""


def is_same_subnet(ip1: str, ip2: str) -> bool:
    """Check if same /24 subnet"""
    if not ip1 or not ip2:
        return False
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception:
        return False


def generate_node_id() -> str:
    """Generate unique node_id"""
    hostname = socket.gethostname()
    try:
        import uuid
        mac = uuid.getnode()
        return hashlib.sha256(f"{hostname}-{mac}".encode()).hexdigest()[:32]
    except OSError:
        return hashlib.sha256(hostname.encode()).hexdigest()[:32]


def generate_vpn_ip(node_id: str) -> str:
    """Generate VPN IP from node_id"""
    h = hashlib.sha256(node_id.encode()).digest()
    return f"10.99.{h[0]}.{h[1]}"


class WireGuardManager:
    """WireGuard interface management"""

    def __init__(self, interface: str, config_dir: str):
        self.interface = interface
        self.config_dir = config_dir
        self.private_key = ""
        self.public_key = ""
        os.makedirs(config_dir, exist_ok=True)

    def load_or_create_keys(self):
        """Load or generate keys"""
        priv_path = os.path.join(self.config_dir, "private.key")
        pub_path = os.path.join(self.config_dir, "public.key")

        if os.path.exists(priv_path) and os.path.exists(pub_path):
            self.private_key = open(priv_path).read().strip()
            self.public_key = open(pub_path).read().strip()
        else:
            self.private_key = run("wg genkey")
            self.public_key = run(f"echo '{self.private_key}' | wg pubkey")
            open(priv_path, "w").write(self.private_key)
            open(pub_path, "w").write(self.public_key)
            os.chmod(priv_path, 0o600)

        print(f"Public Key: {self.public_key}")

    def setup_interface(self, vpn_ip: str, listen_port: int):
        """Setup interface"""
        is_macos = sys.platform == "darwin"

        if is_macos:
            wg_go = "/opt/homebrew/bin/wireguard-go"
            sock_dir = "/var/run/wireguard"
            os.makedirs(sock_dir, exist_ok=True)

            run(f"rm -f {sock_dir}/{self.interface}.sock", check=False)
            run(f"pkill -f 'wireguard-go.*{self.interface}'", check=False)
            time.sleep(0.5)

            utun_name = "utun9"
            run(f"rm -f {sock_dir}/{utun_name}.sock", check=False)
            run(f"pkill -f 'wireguard-go.*{utun_name}'", check=False)

            import subprocess as sp
            proc = sp.Popen([wg_go, utun_name], stdout=sp.PIPE, stderr=sp.PIPE)
            time.sleep(1)

            wg_dir = "/etc/wireguard"
            os.makedirs(wg_dir, exist_ok=True)
            conf = f"[Interface]\nPrivateKey = {self.private_key}\nListenPort = {listen_port}\n"
            conf_path = f"{wg_dir}/{utun_name}.conf"
            open(conf_path, "w").write(conf)
            os.chmod(conf_path, 0o600)

            run(f"wg setconf {utun_name} {conf_path}")
            run(f"ifconfig {utun_name} inet {vpn_ip} {vpn_ip} netmask 255.255.0.0")
            run(f"route delete -net 10.99.0.0/16 2>/dev/null", check=False)
            run(f"route add -net 10.99.0.0/16 -interface {utun_name}")
            self.interface = utun_name
        else:
            run(f"ip link delete {self.interface} 2>/dev/null", check=False)
            run(f"ip link add {self.interface} type wireguard")
            run(f"ip addr add {vpn_ip}/16 dev {self.interface}")

            wg_dir = "/etc/wireguard"
            os.makedirs(wg_dir, exist_ok=True)
            conf = f"[Interface]\nPrivateKey = {self.private_key}\nListenPort = {listen_port}\n"
            conf_path = f"{wg_dir}/{self.interface}.conf"
            open(conf_path, "w").write(conf)
            os.chmod(conf_path, 0o600)

            run(f"wg setconf {self.interface} {conf_path}")
            run(f"ip link set {self.interface} up")

        print(f"Interface {self.interface} up with {vpn_ip}")

    def add_peer(self, public_key: str, endpoint: str, allowed_ips: str):
        """Add/update peer"""
        cmd = f"wg set {self.interface} peer {public_key} allowed-ips {allowed_ips} persistent-keepalive 25"
        if endpoint:
            cmd += f" endpoint {endpoint}"
        run(cmd, check=False)

    def remove_peer(self, public_key: str):
        """Remove peer"""
        run(f"wg set {self.interface} peer {public_key} remove", check=False)

    def get_peers(self) -> dict:
        """Current peer list"""
        result = {}
        try:
            output = run(f"wg show {self.interface} dump", check=False)
            for line in output.split("\n")[1:]:
                parts = line.split("\t")
                if len(parts) >= 5:
                    pub_key = parts[0]
                    result[pub_key] = {
                        "endpoint": parts[2] if parts[2] != "(none)" else "",
                        "allowed_ips": parts[3],
                        "latest_handshake": int(parts[4]) if parts[4] != "0" else 0,
                    }
        except (subprocess.SubprocessError, OSError) as e:
            pass  # e silenced
        return result

    def cleanup(self):
        """Delete interface"""
        run(f"ip link delete {self.interface} 2>/dev/null", check=False)


class Wire:
    """wire main class - autonomous mesh network"""

    def __init__(self, server_url: str, listen_port: int = 51820, config_dir: str = None):
        self.server_url = server_url.rstrip("/")
        self.listen_port = listen_port
        self.config_dir = config_dir or os.path.expanduser("~/.wire")
        self.node_id = generate_node_id()
        self.vpn_ip = generate_vpn_ip(self.node_id)
        self.lan_ip = detect_lan_ip()
        self.public_ip = ""
        self.running = False

        # Connection state tracking
        self.direct_peers = {}      # {vpn_ip: {"pub_key": ..., "endpoint": ..., "status": "ok"|"fail"}}
        self.current_relay = None   # Current relay in use VPN IP

        self.wg = WireGuardManager(INTERFACE, self.config_dir)

    @property
    def is_relay(self) -> bool:
        """Am I a relay server?"""
        return any(r["vpn_ip"] == self.vpn_ip for r in RELAY_CANDIDATES)

    def api(self, method: str, path: str, data: dict = None) -> dict:
        """API call - Multi-server failover"""
        # Try current server first, then others on failure
        servers = [self.server_url] + [s for s in SERVER_URLS if s != self.server_url]

        for server_url in servers:
            url = f"{server_url}{path}"
            req = urllib.request.Request(url, method=method)
            req.add_header("Content-Type", "application/json")
            body = json.dumps(data).encode() if data else None
            try:
                with urllib.request.urlopen(req, body, timeout=5) as resp:
                    result = json.loads(resp.read())
                    # Set this server as default on success
                    if server_url != self.server_url:
                        print(f"[FAILOVER] Server changed: {self.server_url} → {server_url}")
                        self.server_url = server_url
                    return result
            except Exception as e:
                if server_url == servers[-1]:
                    print(f"[ERROR] All servers failed: {e}")
                continue
        return {}

    def register(self):
        """Register with server"""
        result = self.api("POST", "/register", {
            "node_id": self.node_id,
            "port": self.listen_port,
            "wg_public_key": self.wg.public_key,
            "lan_ip": self.lan_ip,
        })
        if result.get("ok"):
            self.public_ip = result.get("your_ip", "")
        return result.get("ok", False)

    def refresh_peers(self):
        """Refresh peer list - autonomous mesh"""
        result = self.api("GET", "/peers")
        peers = result.get("peers", [])

        # SSOT: Server is source of truth - replace local peer on key mismatch
        server_map = {p["vpn_ip"]: p["wg_public_key"] for p in peers}
        current_wg = self.wg.get_peers()

        for pub_key, info in list(current_wg.items()):
            allowed_ips = info.get("allowed_ips", "")
            # Skip relay peers
            if "/16" in allowed_ips:
                continue
            # /32 Extract peer VPN IP
            for ip_range in allowed_ips.split(","):
                ip = ip_range.strip().split("/")[0]
                if ip.startswith("10.99."):
                    # Server has same IP but different key -> delete
                    if ip in server_map and server_map[ip] != pub_key:
                        print(f"[SSOT] Key mismatch: delete old key")
                        self.wg.remove_peer(pub_key)
                    # IP not in server list -> delete
                    elif ip not in server_map and ip != self.vpn_ip:
                        print(f"[SSOT] Not in server list: delete peer")
                        self.wg.remove_peer(pub_key)
                    break

        if self.is_relay:
            # Relay mode: setup VPS nodes and fallback
            vps_ips = {r["vpn_ip"] for r in RELAY_CANDIDATES}
            peer_map = {p["vpn_ip"]: p for p in peers}
            count = 0

            # 1. Direct connect to other VPS nodes
            for peer in peers:
                if peer["node_id"] == self.node_id:
                    continue
                if peer["vpn_ip"] in vps_ips:
                    endpoint = f"{peer['public_ip']}:{peer['port']}"
                    self.wg.add_peer(peer["wg_public_key"], endpoint, f"{peer['vpn_ip']}/32")
                    count += 1

            # 2. Add primary relay for NAT node reach
            # Only if not relay1
            primary = RELAY_CANDIDATES[0]
            if primary["vpn_ip"] != self.vpn_ip and primary["vpn_ip"] in peer_map:
                p = peer_map[primary["vpn_ip"]]
                endpoint = f"{p['public_ip']}:{p['port']}"
                # Add to relay1 for NAT node traffic
                self.wg.add_peer(p["wg_public_key"], endpoint, "10.99.0.0/16")

            return f"[RELAY] {count} VPS peers"
        else:
            return self._refresh_as_client(peers)

    def _refresh_as_client(self, peers: list) -> str:
        """Refresh peers as regular client"""
        peer_map = {p["vpn_ip"]: p for p in peers}
        current_wg = self.wg.get_peers()
        now = time.time()

        direct_ok = 0
        direct_fail = 0

        # 1. Try direct connection to all non-relay peers
        for peer in peers:
            if peer["node_id"] == self.node_id:
                continue
            vpn_ip = peer["vpn_ip"]
            pub_key = peer["wg_public_key"]

            # Process relays later
            if any(r["vpn_ip"] == vpn_ip for r in RELAY_CANDIDATES):
                continue

            # All non-relay peers via relay
            direct_fail += 1
            continue

            # Check existing handshake
            if pub_key in current_wg:
                hs = current_wg[pub_key]["latest_handshake"]
                if hs > 0 and (now - hs) < HANDSHAKE_TIMEOUT:
                    # Direct connection working
                    direct_ok += 1
                    self.direct_peers[vpn_ip] = {"status": "ok", "pub_key": pub_key}
                    self.wg.add_peer(pub_key, endpoint, f"{vpn_ip}/32")
                    continue

            # New peer or handshake failed -> try direct
            if vpn_ip not in self.direct_peers or self.direct_peers[vpn_ip]["status"] != "fail":
                self.wg.add_peer(pub_key, endpoint, f"{vpn_ip}/32")
                self.direct_peers[vpn_ip] = {"status": "trying", "pub_key": pub_key, "since": now}
            else:
                # Already marked failed -> remove for relay
                direct_fail += 1
                if pub_key in current_wg and "/32" in current_wg[pub_key]["allowed_ips"]:
                    self.wg.remove_peer(pub_key)

        # 2. Add relay candidates directly (VPS only)
        # NAT nodes route via relay1 only
        if self.is_relay:  # Only VPS nodes connect to other VPS directly
            peer_map = {p["vpn_ip"]: p for p in peers}
            for candidate in RELAY_CANDIDATES:
                vpn_ip = candidate["vpn_ip"]
                if vpn_ip == self.vpn_ip:
                    continue
                if vpn_ip in peer_map:
                    p = peer_map[vpn_ip]
                    endpoint = f"{p['public_ip']}:{p['port']}"
                    self.wg.add_peer(p["wg_public_key"], endpoint, f"{vpn_ip}/32")

        # 3. Add main relay (for NAT nodes)
        relay = self._select_relay(peers, current_wg)
        if relay:
            endpoint = f"{relay['public_ip']}:{relay['port']}"
            self.wg.add_peer(relay["wg_public_key"], endpoint, "10.99.0.0/16")
            self.current_relay = relay["vpn_ip"]
        else:
            self.current_relay = None

        return f"direct:{direct_ok} relay:{direct_fail}"

    def _select_relay(self, peers: list, current_wg: dict) -> dict:
        """Select relay to use"""
        peer_map = {p["vpn_ip"]: p for p in peers}
        now = time.time()

        # Keep current working relay
        for pub_key, info in current_wg.items():
            if "/16" in info["allowed_ips"] and info["latest_handshake"] > 0:
                if (now - info["latest_handshake"]) < HANDSHAKE_TIMEOUT:
                    for p in peers:
                        if p["wg_public_key"] == pub_key:
                            return p

        # Select new relay from candidates
        for candidate in RELAY_CANDIDATES:
            vpn_ip = candidate["vpn_ip"]
            if vpn_ip == self.vpn_ip:
                continue
            if vpn_ip in peer_map:
                return peer_map[vpn_ip]

        return None

    def check_peer_health(self):
        """Check peer connection status"""
        current = self.wg.get_peers()
        now = time.time()

        for vpn_ip, info in list(self.direct_peers.items()):
            pub_key = info.get("pub_key")
            if not pub_key or pub_key not in current:
                continue

            wg_info = current[pub_key]
            hs = wg_info["latest_handshake"]

            if info["status"] == "trying":
                # Pending peer - check timeout
                since = info.get("since", now)
                if hs > 0 and (now - hs) < HANDSHAKE_TIMEOUT:
                    self.direct_peers[vpn_ip]["status"] = "ok"
                    print(f"  ✓ {vpn_ip} direct connected")
                elif (now - since) > HANDSHAKE_TIMEOUT:
                    self.direct_peers[vpn_ip]["status"] = "fail"
                    print(f"  ✗ {vpn_ip} direct failed → relay")

            elif info["status"] == "ok":
                # Existing connection - check if lost
                if hs == 0 or (now - hs) > HANDSHAKE_TIMEOUT:
                    self.direct_peers[vpn_ip]["status"] = "fail"
                    print(f"  ✗ {vpn_ip} connection lost → relay")

    def retry_failed_peers(self):
        """Retry direct connection to failed peers"""
        retry_count = 0
        for vpn_ip, info in self.direct_peers.items():
            if info["status"] == "fail":
                info["status"] = "trying"
                info["since"] = time.time()
                retry_count += 1

        if retry_count > 0:
            print(f"  ↻ {retry_count} peers retry direct")

    def start(self):
        """Start VPN"""
        mode = "[RELAY]" if self.is_relay else "[CLIENT]"
        print(f"""
╔═══════════════════════════════════════════╗
║     wire {mode:8}              ║
║     Autonomous WireGuard Mesh VPN             ║
╚═══════════════════════════════════════════╝

Server:    {self.server_url}
Node ID:   {self.node_id[:16]}...
VPN IP:    {self.vpn_ip}
LAN IP:    {self.lan_ip or 'N/A'}
Port:      {self.listen_port}
""")

        self.wg.load_or_create_keys()
        self.wg.setup_interface(self.vpn_ip, self.listen_port)

        if not self.register():
            print("Failed to register with server")
            self.wg.cleanup()
            return

        self.running = True

        def shutdown(sig, frame):
            print("\nShutting down...")
            self.running = False

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        print("\nRunning... (Ctrl+C to stop)\n")
        last_refresh = 0
        last_retry = time.time()

        while self.running:
            now = time.time()

            if now - last_refresh >= REFRESH_INTERVAL:
                self.register()
                status = self.refresh_peers()
                self.check_peer_health()
                relay_info = f" via {self.current_relay}" if self.current_relay else ""
                print(f"[{time.strftime('%H:%M:%S')}] {status}{relay_info}")
                last_refresh = now

            # 5min retry direct connection
            if now - last_retry >= RETRY_DIRECT_INTERVAL:
                self.retry_failed_peers()
                last_retry = now

            time.sleep(1)

        self.wg.cleanup()
        print("Stopped")


def main():
    parser = argparse.ArgumentParser(description="wire VPN client")
    parser.add_argument("--server", "-s", required=True, help="Server URL (http://IP:PORT)")
    parser.add_argument("--port", "-p", type=int, default=51820, help="WireGuard listen port")
    parser.add_argument("--config", "-c", help="Config directory")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("Error: Must run as root (sudo)")
        sys.exit(1)

    client = Wire(args.server, args.port, args.config)
    client.start()


if __name__ == "__main__":
    main()
