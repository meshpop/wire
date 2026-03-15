#!/usr/bin/env python3
"""wire CLI"""
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

__version__ = "0.1.0"

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
INTERFACE = "wire0"
REFRESH_INTERVAL = 30
IS_MACOS = sys.platform == "darwin"
WG_PATH = "/opt/homebrew/bin/wg" if IS_MACOS else "wg"
WG_GO_PATH = "/opt/homebrew/bin/wireguard-go" if IS_MACOS else "wireguard-go"


def run(cmd: str, check=True) -> str:
    """쉘 명령 실행"""
    if IS_MACOS:
        cmd = cmd.replace("wg ", f"{WG_PATH} ").replace("|wg ", f"|{WG_PATH} ")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{r.stderr}")
    return r.stdout.strip()


def detect_lan_ip() -> str:
    """LAN IP 감지"""
    if IS_MACOS:
        for iface in ["en0", "en1", "en8"]:
            try:
                r = subprocess.run(["ifconfig", iface], capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    for line in r.stdout.split("\n"):
                        if "inet " in line and "127.0.0.1" not in line:
                            parts = line.strip().split()
                            ip = parts[parts.index("inet") + 1]
                            if ip.startswith("192.168.") or (ip.startswith("10.") and not ip.startswith("10.99.")):
                                return ip
            except:
                pass
    else:
        try:
            r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                for ip in r.stdout.split():
                    if ip.startswith("192.168.") or (ip.startswith("10.") and not ip.startswith("10.99.")):
                        return ip
        except:
            pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("10.99.") else ""
    except:
        return ""


def is_same_subnet(ip1: str, ip2: str) -> bool:
    if not ip1 or not ip2:
        return False
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except:
        return False


def generate_node_id() -> str:
    hostname = socket.gethostname()
    try:
        import uuid
        mac = uuid.getnode()
        return hashlib.sha256(f"{hostname}-{mac}".encode()).hexdigest()[:32]
    except:
        return hashlib.sha256(hostname.encode()).hexdigest()[:32]


def generate_vpn_ip(node_id: str) -> str:
    h = hashlib.sha256(node_id.encode()).digest()
    return f"10.99.{h[0]}.{h[1]}"


# ─────────────────────────────────────────────────────────────
# WireGuard Manager
# ─────────────────────────────────────────────────────────────
class WireGuardManager:
    def __init__(self, interface: str, config_dir: str):
        self.interface = interface
        self.config_dir = config_dir
        self.private_key = ""
        self.public_key = ""
        os.makedirs(config_dir, exist_ok=True)

    def load_or_create_keys(self):
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
        print(f"Public Key: {self.public_key[:20]}...")

    def setup_interface(self, vpn_ip: str, listen_port: int):
        wg_dir = "/etc/wireguard"
        os.makedirs(wg_dir, exist_ok=True)

        if IS_MACOS:
            sock_dir = "/var/run/wireguard"
            os.makedirs(sock_dir, exist_ok=True)
            utun_name = "utun9"

            run(f"rm -f {sock_dir}/{utun_name}.sock", check=False)
            run(f"pkill -f 'wireguard-go.*{utun_name}'", check=False)
            time.sleep(0.5)

            subprocess.Popen([WG_GO_PATH, utun_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(1)

            conf_path = f"{wg_dir}/{utun_name}.conf"
            open(conf_path, "w").write(f"[Interface]\nPrivateKey = {self.private_key}\nListenPort = {listen_port}\n")
            os.chmod(conf_path, 0o600)

            run(f"wg setconf {utun_name} {conf_path}")
            run(f"ifconfig {utun_name} inet {vpn_ip} {vpn_ip} netmask 255.255.0.0")
            run("route delete -net 10.99.x.x/16 2>/dev/null", check=False)
            run(f"route add -net 10.99.x.x/16 -interface {utun_name}")
            self.interface = utun_name
        else:
            run(f"ip link delete {self.interface} 2>/dev/null", check=False)
            run(f"ip link add {self.interface} type wireguard")
            run(f"ip addr add {vpn_ip}/16 dev {self.interface}")

            conf_path = f"{wg_dir}/{self.interface}.conf"
            open(conf_path, "w").write(f"[Interface]\nPrivateKey = {self.private_key}\nListenPort = {listen_port}\n")
            os.chmod(conf_path, 0o600)

            run(f"wg setconf {self.interface} {conf_path}")
            run(f"ip link set {self.interface} up")

        print(f"Interface {self.interface} up with {vpn_ip}")

    def add_peer(self, public_key: str, endpoint: str, allowed_ips: str):
        cmd = f"wg set {self.interface} peer {public_key} allowed-ips {allowed_ips} persistent-keepalive 25"
        if endpoint:
            cmd += f" endpoint {endpoint}"
        run(cmd, check=False)

    def get_peers(self) -> dict:
        result = {}
        try:
            output = run(f"wg show {self.interface} dump", check=False)
            for line in output.split("\n")[1:]:
                parts = line.split("\t")
                if len(parts) >= 5:
                    result[parts[0]] = {
                        "endpoint": parts[2] if parts[2] != "(none)" else "",
                        "allowed_ips": parts[3],
                        "latest_handshake": int(parts[4]) if parts[4] != "0" else 0,
                    }
        except:
            pass
        return result

    def cleanup(self):
        if IS_MACOS:
            run(f"pkill -f 'wireguard-go.*{self.interface}'", check=False)
            run(f"rm -f /var/run/wireguard/{self.interface}.sock", check=False)
        else:
            run(f"ip link delete {self.interface} 2>/dev/null", check=False)


# ─────────────────────────────────────────────────────────────
# VPN Client
# ─────────────────────────────────────────────────────────────
class VPNClient:
    def __init__(self, server_url: str, listen_port: int = 51820, config_dir: str = None):
        self.server_url = server_url.rstrip("/")
        self.listen_port = listen_port
        self.config_dir = config_dir or os.path.expanduser("~/.wire")
        self.node_id = generate_node_id()
        self.vpn_ip = generate_vpn_ip(self.node_id)
        self.lan_ip = detect_lan_ip()
        self.running = False
        self.wg = WireGuardManager(INTERFACE, self.config_dir)

    def api(self, method: str, path: str, data: dict = None) -> dict:
        url = f"{self.server_url}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode() if data else None
        try:
            with urllib.request.urlopen(req, body, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"API error: {e}")
            return {}

    def register(self):
        result = self.api("POST", "/register", {
            "node_id": self.node_id,
            "port": self.listen_port,
            "wg_public_key": self.wg.public_key,
            "lan_ip": self.lan_ip,
        })
        if result.get("ok"):
            print(f"Registered: {result.get('your_ip')} -> VPN {self.vpn_ip}")
        return result.get("ok", False)

    def refresh_peers(self):
        result = self.api("GET", "/peers")
        peers = result.get("peers", [])
        relay_udp = result.get("relay_udp", "")  # 릴레이 서버 UDP 주소
        current = self.wg.get_peers()

        for peer in peers:
            if peer["node_id"] == self.node_id:
                continue

            pub_key = peer["wg_public_key"]
            vpn_ip = peer["vpn_ip"]

            # 기존 핸드쉐이크 확인 (직접 연결 실패 감지)
            peer_info = current.get(pub_key, {})
            last_hs = peer_info.get("latest_handshake", 0)
            no_handshake = last_hs == 0 or (time.time() - last_hs > 180)

            # endpoint 결정
            if self.lan_ip and peer.get("lan_ip") and is_same_subnet(self.lan_ip, peer["lan_ip"]):
                # 같은 LAN
                endpoint = f"{peer['lan_ip']}:{peer['port']}"
                mode = "LAN"
            elif no_handshake and pub_key in current and relay_udp:
                # 직접 연결 실패 → 릴레이 사용
                # 릴레이 endpoint 설정 (서버의 UDP 포트)
                relay_host = self.server_url.split("//")[1].split(":")[0]
                endpoint = f"{relay_host}:8787"
                mode = "RELAY"
            else:
                # 직접 연결 시도
                endpoint = f"{peer['public_ip']}:{peer['port']}"
                mode = "DIRECT"

            if pub_key not in current:
                print(f"  + {vpn_ip} ({mode}: {endpoint})")
            elif mode == "RELAY" and "RELAY" not in str(peer_info.get("endpoint", "")):
                print(f"  ~ {vpn_ip} -> RELAY (direct failed)")

            self.wg.add_peer(pub_key, endpoint, f"{vpn_ip}/32")

        return len(peers)

    def start(self):
        print(f"""
╔═══════════════════════════════════════╗
║     wire v{__version__}              ║
╚═══════════════════════════════════════╝

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

        while self.running:
            now = time.time()
            if now - last_refresh >= REFRESH_INTERVAL:
                self.register()
                count = self.refresh_peers()
                print(f"[{time.strftime('%H:%M:%S')}] {count} peers")
                last_refresh = now
            time.sleep(1)

        self.wg.cleanup()
        print("Stopped")


# ─────────────────────────────────────────────────────────────
# Relay Server with UDP Relay
# ─────────────────────────────────────────────────────────────
def run_server(port: int = 8786, udp_port: int = 8787):
    """릴레이 서버 (HTTP + UDP)"""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading
    import struct

    peers = {}  # node_id -> peer info
    endpoints = {}  # vpn_ip -> (public_ip, wg_port)
    lock = threading.Lock()

    # ─────────────────────────────────────────
    # HTTP Server (피어 등록/조회)
    # ─────────────────────────────────────────
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == "/peers":
                with lock:
                    now = time.time()
                    active = [p for p in peers.values() if now < p.get("expires", 0)]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                # 릴레이 서버 정보 추가
                self.wfile.write(json.dumps({
                    "peers": active,
                    "relay_udp": f"0.0.0.0:{udp_port}"
                }).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/register":
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length)) if length else {}

                node_id = data.get("node_id", "")
                client_ip = self.client_address[0]
                vpn_ip = generate_vpn_ip(node_id)
                wg_port = data.get("port", 51820)

                with lock:
                    peers[node_id] = {
                        "node_id": node_id,
                        "public_ip": client_ip,
                        "port": wg_port,
                        "wg_public_key": data.get("wg_public_key", ""),
                        "lan_ip": data.get("lan_ip", ""),
                        "vpn_ip": vpn_ip,
                        "registered": time.time(),
                        "expires": time.time() + 120,
                    }
                    # UDP 릴레이용 endpoint 매핑
                    endpoints[vpn_ip] = (client_ip, wg_port)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok": True,
                    "your_ip": client_ip,
                    "relay_udp": f"{client_ip}:{udp_port}"
                }).encode())

            elif self.path == "/tunnel":
                # 릴레이 터널 요청
                # {"from_vpn": "10.99.x.x", "to_vpn": "10.99.y.y"}
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length)) if length else {}

                from_vpn = data.get("from_vpn", "")
                to_vpn = data.get("to_vpn", "")

                with lock:
                    if from_vpn in endpoints and to_vpn in endpoints:
                        from_pub, _ = endpoints[from_vpn]
                        to_pub, to_port = endpoints[to_vpn]

                        # 터널 설정: from의 패킷 → to로 전달
                        tunnels[from_pub] = (to_pub, to_port)
                        print(f"[TUNNEL] {from_vpn} ({from_pub}) -> {to_vpn} ({to_pub}:{to_port})")

                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "relay_endpoint": f"0.0.0.0:{udp_port}",
                            "note": f"Set WireGuard peer {to_vpn} endpoint to relay:{udp_port}"
                        }).encode())
                    else:
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"ok": False, "error": "VPN IP not found"}).encode())

            else:
                self.send_response(404)
                self.end_headers()

    # ─────────────────────────────────────────
    # UDP Relay Server (WireGuard 트래픽 중계)
    # ─────────────────────────────────────────
    # 터널 요청: POST /tunnel {"from_vpn": "10.99.x.x", "to_vpn": "10.99.y.y"}
    # 서버가 from_vpn의 트래픽을 to_vpn의 실제 endpoint로 전달
    tunnels = {}  # (src_public_ip) -> (dst_public_ip, dst_port)
    reverse_tunnels = {}  # (dst_public_ip, dst_port) -> (src_public_ip, src_port)

    def udp_relay():
        """
        UDP 릴레이:
        - 등록된 터널에 따라 WireGuard 패킷 양방향 전달
        - 클라이언트가 /tunnel API로 터널 요청하면 자동 설정
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", udp_port))
        print(f"UDP Relay on 0.0.0.0:{udp_port}")

        while True:
            try:
                data, addr = sock.recvfrom(65535)
                src_ip = addr[0]

                with lock:
                    # Forward: src -> relay -> dst
                    if src_ip in tunnels:
                        dst_ip, dst_port = tunnels[src_ip]
                        sock.sendto(data, (dst_ip, dst_port))
                        # 응답 라우팅 설정
                        reverse_tunnels[(dst_ip, dst_port)] = addr

                    # Reverse: dst -> relay -> src
                    elif (src_ip, addr[1]) in reverse_tunnels:
                        orig_addr = reverse_tunnels[(src_ip, addr[1])]
                        sock.sendto(data, orig_addr)

                    # 새 연결: 등록된 피어면 자동 터널 생성
                    else:
                        # 발신자 VPN IP 찾기
                        src_vpn = None
                        for vpn, (pub, _) in endpoints.items():
                            if pub == src_ip:
                                src_vpn = vpn
                                break

                        if src_vpn:
                            # 첫 번째로 매칭되는 다른 피어에게 전달 (simple mode)
                            # 실제로는 /tunnel API로 명시적으로 설정해야 함
                            print(f"[RELAY] New connection from {src_ip} ({src_vpn})")

            except Exception as e:
                print(f"UDP relay error: {e}")

    # HTTP + UDP 서버 동시 실행
    udp_thread = threading.Thread(target=udp_relay, daemon=True)
    udp_thread.start()

    http_server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"HTTP Server on 0.0.0.0:{port}")
    print(f"Relay ready: peers register via HTTP, relay via UDP:{udp_port}")
    http_server.serve_forever()


# ─────────────────────────────────────────────────────────────
# Service Installer
# ─────────────────────────────────────────────────────────────
def install_service(server_url: str, port: int = 51820):
    """시스템 서비스로 설치"""
    if os.geteuid() != 0:
        print("Error: Must run as root (sudo)")
        sys.exit(1)

    python_path = sys.executable

    if IS_MACOS:
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.meshpop.wire</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>-m</string>
        <string>wire.cli</string>
        <string>run</string>
        <string>--server</string>
        <string>{server_url}</string>
        <string>--port</string>
        <string>{port}</string>
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
</plist>"""
        plist_path = "/Library/LaunchDaemons/com.meshpop.wire.plist"
        open(plist_path, "w").write(plist)
        os.system("launchctl load " + plist_path)
        print(f"Installed: {plist_path}")
        print("Service started. Check: launchctl list | grep wire")
    else:
        service = f"""[Unit]
Description=wire VPN
After=network.target

[Service]
Type=simple
ExecStart={python_path} -m wire.cli run --server {server_url} --port {port}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        service_path = "/etc/systemd/system/wire.service"
        open(service_path, "w").write(service)
        os.system("systemctl daemon-reload")
        os.system("systemctl enable wire")
        os.system("systemctl start wire")
        print(f"Installed: {service_path}")
        print("Service started. Check: systemctl status wire")


def show_status(server_url: str = None):
    """tailscale status 스타일로 상태 표시"""
    default_server = os.environ.get("WIRE_SERVER_URL", "")
    srv = server_url or default_server

    # 인터페이스에서 내 VPN IP 가져오기
    iface = "utun9" if IS_MACOS else "wire0"
    my_vpn_ip = ""
    r = subprocess.run(["ifconfig" if IS_MACOS else "ip", "addr" if not IS_MACOS else iface],
                       capture_output=True, text=True)
    for line in r.stdout.split("\n"):
        if "inet " in line and "10.99." in line:
            my_vpn_ip = line.split()[1].split("/")[0]
            break

    # WireGuard 핸드쉐이크 정보 (sudo 필요)
    handshakes = {}
    endpoints = {}
    if os.geteuid() == 0:
        try:
            output = run(f"wg show {iface} dump", check=False)
            for line in output.split("\n")[1:]:
                parts = line.split("\t")
                if len(parts) >= 5:
                    allowed = parts[3].split("/")[0] if "/" in parts[3] else parts[3]
                    hs_time = int(parts[4]) if parts[4] != "0" else 0
                    endpoint = parts[2] if parts[2] != "(none)" else ""
                    handshakes[allowed] = hs_time
                    endpoints[allowed] = endpoint
        except:
            pass

    # 서버에서 피어 목록 가져오기
    try:
        req = urllib.request.Request(f"{srv}/peers")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            peers = data.get("peers", [])
    except:
        print(f"# Server unreachable: {srv}")
        return

    # 헤더
    my_node = ""
    for p in peers:
        if p.get("vpn_ip") == my_vpn_ip:
            my_node = p.get("node_id", "")[:8]
            break

    if my_vpn_ip:
        print(f"{my_vpn_ip}  {my_node}")
    else:
        print("# not connected")
    print()

    # 테이블 출력 (tailscale 스타일)
    for p in peers:
        vpn_ip = p.get("vpn_ip", "")
        pub_ip = p.get("public_ip", "")
        lan_ip = p.get("lan_ip", "")
        port = p.get("port", "")
        node_id = p.get("node_id", "")[:8]

        # 연결 상태 판단
        hs = handshakes.get(vpn_ip, 0)
        ep = endpoints.get(vpn_ip, "")

        if vpn_ip == my_vpn_ip:
            # 자기 자신
            status = "-"
            conn_info = ""
        elif hs:
            ago = int(time.time() - hs)
            if ago < 180:
                status = "active"
            else:
                status = "idle"
            # direct vs relay 판단
            if ep:
                if lan_ip and lan_ip in ep:
                    conn_info = f"direct {ep} (LAN)"
                else:
                    conn_info = f"direct {ep}"
            else:
                conn_info = ""
        else:
            status = "offline"
            conn_info = f"via {pub_ip}:{port}"

        # 출력
        line = f"{vpn_ip:<16} {node_id:<10} {status:<8}"
        if conn_info:
            line += f" {conn_info}"
        print(line)

    # sudo 없이 실행 시 안내
    if os.geteuid() != 0 and not handshakes:
        print()
        print("# run with sudo to see connection status")


# ─────────────────────────────────────────────────────────────
# CLI Main
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="wire",
        description="Simple WireGuard mesh VPN"
    )
    parser.add_argument("--version", "-v", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", help="Commands")

    # run
    p_run = sub.add_parser("run", help="Run VPN client (foreground)")
    p_run.add_argument("--server", "-s", required=True, help="Server URL")
    p_run.add_argument("--port", "-p", type=int, default=51820, help="WireGuard port")
    p_run.add_argument("--config", "-c", help="Config directory")

    # start
    p_start = sub.add_parser("start", help="Install and start as service")
    p_start.add_argument("--server", "-s", required=True, help="Server URL")
    p_start.add_argument("--port", "-p", type=int, default=51820, help="WireGuard port")

    # stop
    sub.add_parser("stop", help="Stop service")

    # status
    p_status = sub.add_parser("status", help="Show status")
    p_status.add_argument("--server", "-s", help="Server URL (optional)")

    # server
    p_server = sub.add_parser("server", help="Run relay server")
    p_server.add_argument("--port", "-p", type=int, default=8786, help="Server port")

    args = parser.parse_args()

    if args.command == "run":
        if os.geteuid() != 0:
            print("Error: Must run as root (sudo)")
            sys.exit(1)
        client = VPNClient(args.server, args.port, args.config)
        client.start()

    elif args.command == "start":
        install_service(args.server, args.port)

    elif args.command == "stop":
        if IS_MACOS:
            os.system("launchctl unload /Library/LaunchDaemons/com.meshpop.wire.plist 2>/dev/null")
            os.system("pkill -f 'wire.cli run'")
        else:
            os.system("systemctl stop wire")
        print("Stopped")

    elif args.command == "status":
        show_status(args.server if hasattr(args, 'server') else None)

    elif args.command == "server":
        run_server(args.port)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
