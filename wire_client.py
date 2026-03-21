#!/usr/bin/env python3
"""
wire client v2.0.0 - WireGuard mesh VPN
CLI and daemon — same core functions used by MCP server.

Usage:
  wire status               - Show network status (queries server)
  wire up [--name NAME]     - Bring up VPN tunnel
  wire down                 - Tear down VPN tunnel
  wire peers                - List peers from server
  wire ping <target>        - Ping a peer by name or VPN IP
  wire install              - Install WireGuard tools

Config: /etc/wire/config.json (root) or ~/.wire/config.json (user)
"""

import argparse
import hashlib
import json
import os
import signal
import socket
import socket as _socket
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error

VERSION          = "2.2.0"
INTERFACE        = "wire0"
REFRESH_INTERVAL = 30       # Heartbeat / peer sync every 30s
PEER_OFFLINE_TTL = 300      # Seconds before marking peer offline in status display
WG_LISTEN_PORT   = 51820

CONFIG_PATHS = [
    "/etc/wire/config.json",
    os.path.expanduser("~/.wire/config.json"),
]

# ── Binary discovery ──────────────────────────────────────────────────

def find_bin(name: str) -> str:
    """Find a binary — checks Homebrew, /usr/local, /usr/bin, PATH."""
    candidates = [
        f"/opt/homebrew/bin/{name}",    # macOS arm64 (Apple Silicon)
        f"/usr/local/bin/{name}",       # macOS x86 / manual install
        f"/usr/bin/{name}",             # Linux
        name,                           # PATH fallback
    ]
    for c in candidates:
        if c == name:
            # PATH search
            r = subprocess.run(["which", name], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        elif os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return name  # last resort — let shell try


def _run(cmd: str, check: bool = False, timeout: int = 10) -> tuple:
    """Run shell command. Returns (stdout, stderr, returncode)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{r.stderr.strip()}")
    return r.stdout.strip(), r.stderr.strip(), r.returncode


# ── Config ────────────────────────────────────────────────────────────

def load_config() -> dict:
    for p in CONFIG_PATHS:
        try:
            with open(p) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {}


def save_config(cfg: dict):
    path = CONFIG_PATHS[0] if os.geteuid() == 0 else CONFIG_PATHS[1]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(path, 0o600)


def get_server_url(override: str = None) -> str:
    if override:
        return override.rstrip("/")
    cfg = load_config()
    url = cfg.get("server_url", "")
    if not url:
        print("Error: no server URL. Run: wire up --server http://IP:8787 --name MYNAME")
        sys.exit(1)
    return url.rstrip("/")


# ── Node identity ─────────────────────────────────────────────────────

def generate_node_id() -> str:
    hostname = socket.gethostname()
    try:
        import uuid
        mac = uuid.getnode()
        seed = f"{hostname}-{mac}"
    except OSError:
        seed = hostname
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def generate_vpn_ip(node_id: str) -> str:
    h = hashlib.sha256(node_id.encode()).digest()
    return f"10.99.{h[0]}.{h[1]}"


def detect_lan_ip() -> str:
    if sys.platform == "darwin":
        for iface in ["en0", "en1", "en8"]:
            out, _, rc = _run(f"ipconfig getifaddr {iface}", timeout=2)
            if rc == 0 and out:
                ip = out.strip()
                if ip.startswith("192.168.") or (ip.startswith("10.") and not ip.startswith("10.99.")):
                    return ip
    try:
        out, _, rc = _run("hostname -I", timeout=2)
        if rc == 0:
            for ip in out.split():
                if ip.startswith("192.168.") or (ip.startswith("10.") and not ip.startswith("10.99.")):
                    return ip
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("10.99."):
            return ip
    except OSError:
        pass

    return ""


# ── UDP STUN — NAT port discovery ─────────────────────────────────────

def discover_nat_port(server_host: str, stun_port: int,
                      wg_port: int = WG_LISTEN_PORT, timeout: float = 3.0) -> tuple:
    """
    Discover the external (NAT-mapped) IP and port for our WireGuard UDP port.

    How it works:
      1. Open a UDP socket bound to wg_port (same port WireGuard will use).
      2. Send a probe packet to the wire server's UDP STUN port.
      3. The server sees the source IP:port after NAT translation and replies.
      4. Close the socket — WireGuard will then bind to the same port.

    For most NAT types (Full Cone, Restricted Cone, Port-Restricted Cone):
      the NAT mapping for port wg_port stays consistent, so WireGuard
      will get the same external port as the probe.

    For Symmetric NAT (per-destination port mapping):
      the probe gives the mapping for the server destination.
      Other peers may see a different external port from the server.
      In that case /punch + relay fallback handles it.

    Returns: (external_ip: str, external_port: int)
      On failure returns ("", wg_port) — caller uses wg_port as best guess.
    """
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", wg_port))
        sock.settimeout(timeout)
        # Send a small probe; content does not matter
        sock.sendto(b"wire-stun-probe", (server_host, stun_port))
        data, _ = sock.recvfrom(256)
        result  = json.loads(data)
        return result.get("ip", ""), int(result.get("port", wg_port))
    except OSError as e:
        # Port already in use — WireGuard may already be running
        _log(f"[stun] bind :{wg_port} failed ({e}) — using port as-is")
        return "", wg_port
    except Exception as e:
        _log(f"[stun] probe failed ({e}) — using port as-is")
        return "", wg_port
    finally:
        sock.close()


def _log(msg: str):
    """Simple stderr logger (does not interfere with MCP stdout)."""
    import sys as _sys
    _sys.stderr.write(msg + "\n")
    _sys.stderr.flush()


# ── HTTP helpers ──────────────────────────────────────────────────────

def api_get(server: str, path: str, timeout: int = 5) -> dict:
    try:
        with urllib.request.urlopen(f"{server}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def api_post(server: str, path: str, data: dict, timeout: int = 5) -> dict:
    try:
        body = json.dumps(data).encode()
        req  = urllib.request.Request(f"{server}{path}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


# ── WireGuard interface management ────────────────────────────────────

def _wg_iface() -> str:
    """Detect active WireGuard interface name (no sudo needed)."""
    import glob
    socks = glob.glob("/var/run/wireguard/*.sock")
    if socks:
        return os.path.basename(sorted(socks)[0]).replace(".sock", "")
    wg = find_bin("wg")
    out, _, _ = _run(f"sudo -n {wg} show interfaces 2>/dev/null")
    if out and "sudo" not in out.lower() and "password" not in out.lower():
        parts = out.split()
        if parts:
            return parts[0]
    if sys.platform != "darwin":
        out, _, _ = _run("ip link show 2>/dev/null")
        for token in out.split():
            name = token.rstrip(":")
            if name.startswith(("wg", "wire")):
                return name
    return ""


def _load_or_create_keys(config_dir: str) -> tuple:
    """Returns (private_key, public_key). Creates if not present."""
    os.makedirs(config_dir, exist_ok=True)
    priv_path = os.path.join(config_dir, "private.key")
    pub_path  = os.path.join(config_dir, "public.key")
    wg = find_bin("wg")

    if os.path.exists(priv_path) and os.path.exists(pub_path):
        priv = open(priv_path).read().strip()
        pub  = open(pub_path).read().strip()
    else:
        priv, _, _ = _run(f"{wg} genkey", check=True)
        pub,  _, _ = _run(f"echo '{priv}' | {wg} pubkey", check=True)
        with open(priv_path, "w") as f: f.write(priv + "\n")
        with open(pub_path,  "w") as f: f.write(pub  + "\n")
        os.chmod(priv_path, 0o600)

    return priv, pub


def _setup_interface(iface: str, vpn_ip: str, listen_port: int, private_key: str):
    """Bring up WireGuard interface."""
    wg     = find_bin("wg")
    wg_dir = "/etc/wireguard"
    os.makedirs(wg_dir, exist_ok=True)

    conf = (
        f"[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"ListenPort = {listen_port}\n"
    )

    if sys.platform == "darwin":
        wg_go   = find_bin("wireguard-go")
        utun    = "utun9"
        sock_dir = "/var/run/wireguard"
        os.makedirs(sock_dir, exist_ok=True)

        _run(f"pkill -f 'wireguard-go.*{utun}' 2>/dev/null")
        _run(f"rm -f {sock_dir}/{utun}.sock")
        time.sleep(0.5)

        subprocess.Popen([wg_go, utun], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

        conf_path = f"{wg_dir}/{utun}.conf"
        with open(conf_path, "w") as f: f.write(conf)
        os.chmod(conf_path, 0o600)

        _run(f"{wg} setconf {utun} {conf_path}", check=True)
        _run(f"ifconfig {utun} inet {vpn_ip} {vpn_ip} netmask 255.255.0.0", check=True)
        _run(f"route delete -net 10.99.0.0/16 2>/dev/null")
        _run(f"route add -net 10.99.0.0/16 -interface {utun}", check=True)
        return utun
    else:
        _run(f"ip link delete {iface} 2>/dev/null")
        _run(f"ip link add {iface} type wireguard", check=True)
        _run(f"ip addr add {vpn_ip}/16 dev {iface}", check=True)

        conf_path = f"{wg_dir}/{iface}.conf"
        with open(conf_path, "w") as f: f.write(conf)
        os.chmod(conf_path, 0o600)

        _run(f"{wg} setconf {iface} {conf_path}", check=True)
        _run(f"ip link set {iface} up", check=True)
        return iface


def _teardown_interface(iface: str):
    wg = find_bin("wg")
    if sys.platform == "darwin":
        _run(f"pkill -f 'wireguard-go.*{iface}' 2>/dev/null")
        _run(f"rm -f /var/run/wireguard/{iface}.sock 2>/dev/null")
        _run(f"route delete -net 10.99.0.0/16 2>/dev/null")
    else:
        _run(f"ip link delete {iface} 2>/dev/null")


def _add_peer(iface: str, pub_key: str, vpn_ip: str, endpoint: str = ""):
    wg  = find_bin("wg")
    cmd = f"{wg} set {iface} peer {pub_key} allowed-ips {vpn_ip}/32 persistent-keepalive 25"
    if endpoint:
        cmd += f" endpoint {endpoint}"
    _run(cmd)


def _sync_peers(iface: str, server: str, my_node_id: str):
    """Pull peers from server and apply to WireGuard interface."""
    data = api_get(server, "/peers")
    peers = data.get("peers", [])
    wg = find_bin("wg")

    for p in peers:
        if p.get("node_id") == my_node_id:
            continue
        pub_key  = p.get("wg_public_key", "")
        vpn_ip   = p.get("vpn_ip", "")
        pub_ip   = p.get("public_ip", "")
        port     = p.get("port", 51820)
        if not pub_key or not vpn_ip:
            continue
        # Use nat_port if available (NAT-mapped external port from server)
        effective_port = p.get("nat_port") or port
        endpoint = f"{pub_ip}:{effective_port}" if pub_ip else ""
        _add_peer(iface, pub_key, vpn_ip, endpoint)

    return len(peers)


# ── Core commands (shared by CLI and MCP) ─────────────────────────────

def cmd_status(server_url: str = None) -> dict:
    """
    Query server /status and return structured data.
    CLI prints tailscale-style table. MCP returns the dict.
    """
    server = get_server_url(server_url)
    data   = api_get(server, "/status", timeout=5)

    if "error" in data:
        return {"ok": False, "error": data["error"], "server": server}

    nodes   = data.get("nodes", [])
    cfg     = load_config()
    my_id   = cfg.get("node_id", generate_node_id())
    my_name = cfg.get("node_name", "")

    result = {
        "ok":      True,
        "server":  server,
        "version": data.get("version", "?"),
        "total":   data.get("total", 0),
        "online":  data.get("online", 0),
        "offline": data.get("offline", 0),
        "nodes":   nodes,
        "my_node_id": my_id,
    }
    return result


def _print_status(data: dict):
    """Print tailscale-style status table to stdout."""
    if not data.get("ok"):
        print(f"Error: {data.get('error', 'unknown')}")
        print(f"Server: {data.get('server', '?')}")
        print("Is the wire server running?")
        return

    nodes   = data.get("nodes", [])
    my_id   = data.get("my_node_id", "")

    GREEN  = "\033[32m"
    GRAY   = "\033[90m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

    print(f"\n{BOLD}wire status{RESET}  {data['server']}")
    print(f"  {data['online']} online / {data['offline']} offline / {data['total']} total\n")

    for n in nodes:
        status  = n.get("status", "offline")
        name    = n.get("node_name") or n.get("node_id", "")[:12]
        vpn_ip  = n.get("vpn_ip", "")
        pub_ip  = n.get("public_ip", "")
        ago     = n.get("last_seen_ago", -1)
        is_me   = n.get("node_id", "") == my_id

        if status == "online":
            dot   = f"{GREEN}●{RESET}"
            color = GREEN
        else:
            dot   = f"{GRAY}○{RESET}"
            color = GRAY

        me_tag = f" {BOLD}(this node){RESET}" if is_me else ""

        if ago < 0:
            seen = "never"
        elif ago < 60:
            seen = f"{ago}s ago"
        elif ago < 3600:
            seen = f"{ago//60}m ago"
        else:
            seen = f"{ago//3600}h ago"

        print(f"  {dot} {color}{name:<16}{RESET}  {vpn_ip:<16}  {pub_ip:<20}  {seen}{me_tag}")

    print()


def cmd_up(name: str = None, server: str = None, port: int = WG_LISTEN_PORT,
           config_dir: str = None) -> dict:
    """
    Bring up WireGuard interface and start daemon.
    Saves config for future runs.
    """
    if os.geteuid() != 0:
        return {"ok": False, "error": "Must run as root (sudo wire up ...)"}

    cfg         = load_config()
    server      = (server or cfg.get("server_url", "")).rstrip("/")
    node_name   = name or cfg.get("node_name", socket.gethostname())
    config_dir  = config_dir or (CONFIG_PATHS[0].replace("/config.json", "") if os.geteuid() == 0
                                 else CONFIG_PATHS[1].replace("/config.json", ""))
    node_id     = cfg.get("node_id") or generate_node_id()
    vpn_ip      = cfg.get("vpn_ip")  or generate_vpn_ip(node_id)
    lan_ip      = detect_lan_ip()

    if not server:
        return {"ok": False, "error": "Server URL required. Use: wire up --server http://IP:8787 --name NAME"}

    # Save config
    cfg.update({
        "server_url": server,
        "node_name":  node_name,
        "node_id":    node_id,
        "vpn_ip":     vpn_ip,
        "listen_port": port,
    })
    save_config(cfg)

    # Keys
    priv, pub = _load_or_create_keys(config_dir)

    # Discover NAT-mapped external UDP port BEFORE WireGuard takes the socket.
    # Parse server host from URL (e.g. "http://45.76.100.10:8787" → "45.76.100.10")
    import urllib.parse as _up
    _parsed      = _up.urlparse(server)
    _server_host = _parsed.hostname or server.split("//")[-1].split(":")[0]
    _stun_port   = (_parsed.port or 8787) + 1  # UDP STUN = HTTP port + 1

    ext_ip, nat_port = discover_nat_port(_server_host, _stun_port, port)
    if ext_ip:
        _log(f"[stun] external UDP: {ext_ip}:{nat_port}")
    else:
        _log(f"[stun] could not discover NAT port, using {port}")
        nat_port = port

    # Bring up interface
    actual_iface = _setup_interface(INTERFACE, vpn_ip, port, priv)

    # Register with server
    reg = api_post(server, "/register", {
        "node_id":       node_id,
        "node_name":     node_name,
        "port":          port,
        "nat_port":      nat_port,
        "wg_public_key": pub,
        "lan_ip":        lan_ip,
    })
    if reg.get("error"):
        return {"ok": False, "error": f"Registration failed: {reg['error']}"}

    # Sync peers
    peer_count = _sync_peers(actual_iface, server, node_id)

    # Start daemon thread
    _start_daemon(actual_iface, server, node_id, node_name, pub, port, nat_port)

    result = {
        "ok":         True,
        "node_name":  node_name,
        "node_id":    node_id,
        "vpn_ip":     vpn_ip,
        "interface":  actual_iface,
        "server":     server,
        "peers":      peer_count,
    }
    return result


def cmd_down() -> dict:
    """Tear down WireGuard interface and stop daemon."""
    if os.geteuid() != 0:
        return {"ok": False, "error": "Must run as root"}

    _stop_daemon()

    iface = _wg_iface() or INTERFACE
    _teardown_interface(iface)
    return {"ok": True, "message": f"Interface {iface} removed"}


def cmd_peers(server_url: str = None) -> dict:
    """List peers from server."""
    server = get_server_url(server_url)
    data   = api_get(server, "/peers", timeout=5)
    return {"ok": True, "server": server, **data}


def cmd_ping(target: str, server_url: str = None, count: int = 4) -> dict:
    """Ping a peer by VPN IP or node name."""
    server = get_server_url(server_url)
    data   = api_get(server, "/status", timeout=5)
    nodes  = data.get("nodes", [])

    # Resolve name → VPN IP
    vpn_ip = target
    if not target.startswith("10."):
        for n in nodes:
            if n.get("node_name", "").lower() == target.lower():
                vpn_ip = n.get("vpn_ip", target)
                break

    out, _, rc = _run(f"ping -c {count} -W 2 {vpn_ip}", timeout=count * 3 + 5)
    return {
        "ok":     rc == 0,
        "target": target,
        "vpn_ip": vpn_ip,
        "output": out,
    }


def cmd_install() -> dict:
    """Install WireGuard tools for this platform."""
    is_macos = sys.platform == "darwin"

    wg = find_bin("wg")
    out, _, rc = _run(f"{wg} --version 2>/dev/null || {wg} version 2>/dev/null")
    if rc == 0 and out:
        return {"ok": True, "status": "already_installed", "version": out.split("\n")[0]}

    if is_macos:
        wg_go = find_bin("wireguard-go")
        go_ok = os.path.isfile(wg_go) and os.access(wg_go, os.X_OK)
        if go_ok:
            return {"ok": True, "status": "already_installed", "wireguard-go": wg_go}

        instructions = (
            "Install WireGuard on macOS:\n"
            "  brew install wireguard-tools wireguard-go\n"
            "\nOr via Mac App Store: WireGuard.app"
        )
    else:
        distro, _, _ = _run("cat /etc/os-release 2>/dev/null | grep -i 'id=' | head -1")
        if "ubuntu" in distro.lower() or "debian" in distro.lower():
            instructions = (
                "Install WireGuard on Debian/Ubuntu:\n"
                "  sudo apt-get update && sudo apt-get install -y wireguard wireguard-tools"
            )
        elif "centos" in distro.lower() or "rhel" in distro.lower() or "fedora" in distro.lower():
            instructions = (
                "Install WireGuard on RHEL/Fedora:\n"
                "  sudo dnf install -y wireguard-tools"
            )
        else:
            instructions = (
                "Install WireGuard:\n"
                "  Debian/Ubuntu: apt install wireguard-tools\n"
                "  RHEL/Fedora:   dnf install wireguard-tools\n"
                "  Alpine:        apk add wireguard-tools"
            )

    return {"ok": False, "status": "not_installed", "instructions": instructions}


# ── Daemon ────────────────────────────────────────────────────────────

_daemon_thread = None
_daemon_stop   = threading.Event()


def _daemon_loop(iface: str, server: str, node_id: str, node_name: str,
                 pub_key: str, listen_port: int, nat_port: int = 0):
    """Background thread: heartbeat + peer sync every REFRESH_INTERVAL seconds."""
    lan_ip   = detect_lan_ip()
    nat_port = nat_port or listen_port
    while not _daemon_stop.is_set():
        try:
            api_post(server, "/register", {
                "node_id":       node_id,
                "node_name":     node_name,
                "port":          listen_port,
                "nat_port":      nat_port,
                "wg_public_key": pub_key,
                "lan_ip":        lan_ip,
            })
            _sync_peers(iface, server, node_id)
        except Exception:
            pass
        _daemon_stop.wait(REFRESH_INTERVAL)


def _start_daemon(iface: str, server: str, node_id: str, node_name: str,
                  pub_key: str, listen_port: int, nat_port: int = 0):
    global _daemon_thread, _daemon_stop
    _daemon_stop.clear()
    _daemon_thread = threading.Thread(
        target=_daemon_loop,
        args=(iface, server, node_id, node_name, pub_key, listen_port, nat_port),
        daemon=True,
        name="wire-daemon",
    )
    _daemon_thread.start()


def _stop_daemon():
    global _daemon_stop
    _daemon_stop.set()


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="wire - WireGuard mesh VPN",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  wire up --server http://YOUR_SERVER:8787 --name mynode\n"
            "  wire status\n"
            "  wire peers\n"
            "  wire ping g1\n"
            "  wire down\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"wire v{VERSION}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    # status
    p_st = sub.add_parser("status", help="Show network status")
    p_st.add_argument("--server", "-s", help="Server URL")
    p_st.add_argument("--json", action="store_true", help="JSON output")

    # up
    p_up = sub.add_parser("up", help="Bring up VPN tunnel")
    p_up.add_argument("--server", "-s", help="Server URL (e.g. http://IP:8787)")
    p_up.add_argument("--name",   "-n", help="Node name (default: hostname)")
    p_up.add_argument("--port",   "-p", type=int, default=WG_LISTEN_PORT, help="WireGuard listen port")

    # down
    sub.add_parser("down", help="Tear down VPN tunnel")

    # peers
    p_pr = sub.add_parser("peers", help="List peers from server")
    p_pr.add_argument("--server", "-s", help="Server URL")
    p_pr.add_argument("--json", action="store_true", help="JSON output")

    # ping
    p_pg = sub.add_parser("ping", help="Ping a peer")
    p_pg.add_argument("target", help="Node name or VPN IP")
    p_pg.add_argument("--server", "-s", help="Server URL")
    p_pg.add_argument("--count", "-c", type=int, default=4, help="Ping count")

    # install
    sub.add_parser("install", help="Install WireGuard tools")

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.cmd == "status":
        data = cmd_status(getattr(args, "server", None))
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            _print_status(data)

    elif args.cmd == "up":
        if os.geteuid() != 0:
            print("Error: wire up requires root. Run: sudo wire up ...")
            sys.exit(1)
        result = cmd_up(
            name=getattr(args, "name", None),
            server=getattr(args, "server", None),
            port=getattr(args, "port", WG_LISTEN_PORT),
        )
        if result.get("ok"):
            print(f"✓ wire up: {result['node_name']} ({result['vpn_ip']}) ↔ {result['server']}")
            print(f"  interface: {result['interface']}  peers synced: {result['peers']}")
            print("  daemon running in background (heartbeat every 30s)")
            # Block until signal
            def _sig(s, f): cmd_down()
            signal.signal(signal.SIGINT,  _sig)
            signal.signal(signal.SIGTERM, _sig)
            signal.pause()
        else:
            print(f"✗ {result.get('error', 'unknown error')}")
            sys.exit(1)

    elif args.cmd == "down":
        if os.geteuid() != 0:
            print("Error: wire down requires root")
            sys.exit(1)
        result = cmd_down()
        print(f"✓ {result.get('message', 'done')}")

    elif args.cmd == "peers":
        data = cmd_peers(getattr(args, "server", None))
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            peers = data.get("peers", [])
            print(f"\nPeers ({len(peers)}) from {data.get('server', '?')}:\n")
            for p in peers:
                name   = p.get("node_name") or p.get("node_id", "")[:12]
                vpn_ip = p.get("vpn_ip", "")
                pub_ip = p.get("public_ip", "")
                print(f"  {name:<16}  {vpn_ip:<16}  {pub_ip}")
            print()

    elif args.cmd == "ping":
        result = cmd_ping(args.target, getattr(args, "server", None), getattr(args, "count", 4))
        print(result.get("output", ""))
        sys.exit(0 if result.get("ok") else 1)

    elif args.cmd == "install":
        result = cmd_install()
        if result.get("ok"):
            print(f"✓ WireGuard already installed: {result.get('status')}")
        else:
            print(result.get("instructions", ""))
            sys.exit(1)


if __name__ == "__main__":
    main()
