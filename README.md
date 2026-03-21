# wire

**wire** is a self-hosted WireGuard mesh VPN — like Tailscale, but you own the server.

Any machine (VPS, home server, laptop, cloud instance) installs the same `wire_client.py`. One VPS runs `wire_server.py` as the coordination server. Every node registers with the server, discovers peers, and establishes direct encrypted tunnels — **the coordination server only facilitates introductions; your actual traffic never passes through it**.

---

## Table of Contents

1. [Architecture](#architecture)
2. [How It Works — Step by Step](#how-it-works)
3. [VPN IP Assignment](#vpn-ip-assignment)
4. [NAT Traversal and Hole Punching](#nat-traversal-and-hole-punching)
5. [Keeping Connections Alive](#keeping-connections-alive)
6. [Installation](#installation)
7. [Usage — CLI](#usage--cli)
8. [Usage — MCP (Claude AI)](#usage--mcp)
9. [Configuration](#configuration)
10. [Server API Reference](#server-api-reference)
11. [File Reference](#file-reference)

---

## Architecture

```
                ┌─────────────────────────────────────┐
                │  Coordination Server  (one VPS)      │
                │  wire_server.py  port 8787           │
                │                                      │
                │  Knows: who exists, where they are   │
                │  Does NOT carry traffic              │
                └──────────┬────────────┬─────────────┘
                           │            │
               registers / │            │ registers /
               heartbeat   │            │ heartbeat
                           │            │
              ┌────────────▼──┐      ┌──▼────────────┐
              │  Node A       │      │  Node B        │
              │  wire_client  │      │  wire_client   │
              │  10.99.x.x    │      │  10.99.y.y     │
              └──────┬────────┘      └────────┬───────┘
                     │                        │
                     └──── direct P2P ────────┘
                          WireGuard tunnel
                          (end-to-end encrypted)
```

Three files, three roles:

| File | Role | Where it runs |
|---|---|---|
| `wire_server.py` | Coordination server | One VPS (always on) |
| `wire_client.py` | VPN daemon + CLI | Every node |
| `wire_mcp_server.py` | MCP interface for Claude | Every machine with Claude Desktop |

---

## How It Works

Here is the full flow using concrete IP addresses.

**Our example network:**

```
v1      = VPS, public IP 45.76.100.10    ← runs wire_server.py
g1      = server, public IP 123.45.67.89  ← no NAT
macbook = laptop, behind home router,
          router public IP 211.100.200.50,
          laptop LAN IP 192.168.0.5        ← behind NAT
l1      = home server, no public IP,
          behind router, LAN IP 192.168.1.10 ← behind NAT
```

### Step 1 — Start the coordination server on v1

```bash
# on v1
python3 wire_server.py 8787
```

v1 is now the directory service. It knows nothing yet:

```
GET http://45.76.100.10:8787/status
→ { "total": 0, "online": 0, "nodes": [] }
```

---

### Step 2 — g1 joins the network

```bash
# on g1
sudo python3 wire_client.py up \
  --server http://45.76.100.10:8787 \
  --name g1
```

**What happens internally:**

**① Key generation**

wire generates a WireGuard keypair once and saves it:

```
/etc/wire/private.key  ← never leaves this machine
/etc/wire/public.key   ← sent to the coordination server
```

**② VPN IP assignment**

wire does not use DHCP. Each node's VPN IP is computed deterministically from its node ID (a SHA-256 hash of hostname + MAC address):

```python
h = sha256("g1-AA:BB:CC:DD:EE:FF".encode()).digest()
vpn_ip = f"10.99.{h[0]}.{h[1]}"   # e.g. 10.99.23.187
```

The same machine always gets the same VPN IP. No central allocation, no conflicts (with 65,536 possible addresses in the 10.99.0.0/16 subnet).

**③ WireGuard interface brought up**

```bash
ip link add wire0 type wireguard
ip addr add 10.99.23.187/16 dev wire0
wg setconf wire0 /etc/wireguard/wire0.conf
ip link set wire0 up
```

At this point g1 has a VPN interface but no peers yet.

**④ Registration with coordination server**

```
POST http://45.76.100.10:8787/register
{
  "node_id":       "a1b2c3d4...",
  "node_name":     "g1",
  "wg_public_key": "XYZ9876...",
  "port":          51820,
  "lan_ip":        "10.0.0.2"
}
```

The server records:
- `public_ip = 123.45.67.89` — the IP the HTTP request arrived from
- `nat_port = 51820` — the source port the server observed
- `vpn_ip = 10.99.23.187` — assigned from the hash
- `last_seen = now`

Server response:
```json
{
  "ok":        true,
  "vpn_ip":    "10.99.23.187",
  "your_ip":   "123.45.67.89",
  "your_port": 51820
}
```

**⑤ Background daemon starts**

A thread runs silently: every 30 seconds it calls `/register` (heartbeat) and `/peers` (sync new peers into WireGuard). If g1 goes offline, the server marks it offline after 5 minutes.

---

### Step 3 — macbook joins the network

```bash
# on macbook
sudo python3 wire_client.py up \
  --server http://45.76.100.10:8787 \
  --name macbook
```

macbook is behind a NAT router. It does not know its own public IP or what port the router assigned to its WireGuard traffic. But the coordination server does.

When macbook POSTs to `/register`, v1 observes:
```
public_ip = 211.100.200.50   ← router's public IP
nat_port  = 54321            ← port the router's NAT table assigned
```

The server returns this to macbook:
```json
{
  "your_ip":   "211.100.200.50",
  "your_port": 54321
}
```

macbook saves `nat_port = 54321` and includes it in subsequent heartbeats, so the server can store it in the peer record.

---

### Step 4 — g1 and macbook discover each other

g1's daemon calls `GET /peers` and receives:

```json
{
  "peers": [
    {
      "node_name":     "macbook",
      "vpn_ip":        "10.99.45.22",
      "public_ip":     "211.100.200.50",
      "nat_port":      54321,
      "wg_public_key": "PQR1234..."
    }
  ]
}
```

g1 applies this to its WireGuard interface:

```bash
wg set wire0 \
  peer PQR1234... \
  allowed-ips 10.99.45.22/32 \
  endpoint 211.100.200.50:54321 \
  persistent-keepalive 25
```

macbook does the same and gets g1's entry:

```bash
wg set utun9 \
  peer XYZ9876... \
  allowed-ips 10.99.23.187/32 \
  endpoint 123.45.67.89:51820 \
  persistent-keepalive 25
```

Both sides now know where to send packets.

---

## NAT Traversal and Hole Punching

Most nodes in the real world are behind NAT. A home router or cloud firewall will block incoming UDP unless it has already seen outbound traffic to that remote IP:port.

**The hole punching process:**

```
g1      sends UDP → 211.100.200.50:54321   (macbook's router)
macbook sends UDP → 123.45.67.89:51820     (g1)

macbook's router NAT table already has:
    internal 192.168.0.5:51820  ←→  external 211.100.200.50:54321

When g1's packet arrives at 211.100.200.50:54321,
the router sees the table entry and forwards it to 192.168.0.5:51820.
```

The hole is punched. WireGuard takes over from here — all subsequent traffic is encrypted with each node's public key, and only the intended peer can decrypt it.

**Why this works without a relay:**

1. Both nodes independently registered their public IP:port with the coordination server.
2. Both nodes retrieved each other's public IP:port from `/peers`.
3. Both nodes set each other as a WireGuard `endpoint`.
4. WireGuard sends the first packet. The NAT table entry exists because the node initiated the outbound connection when it first connected.
5. `PersistentKeepalive = 25` ensures a packet is sent every 25 seconds, keeping the NAT entry alive indefinitely.

The coordination server's only role was to exchange addresses. **Actual traffic flows directly between nodes** — the coordination server carries zero bytes of VPN traffic.

---

## Keeping Connections Alive

WireGuard's `PersistentKeepalive = 25` sends a small keepalive packet every 25 seconds:

- Prevents the NAT router from expiring the table entry (most routers expire UDP after 30–120 seconds of silence)
- Automatically re-establishes the connection after an IP change (e.g. laptop moves from home wifi to mobile data — next keepalive re-punches the hole)
- No user intervention required after initial setup

---

## Installation

### Coordination server (one VPS)

```bash
# Install Python 3 (already present on most Linux systems)
python3 --version

# Copy wire_server.py to the server
scp wire_server.py root@45.76.100.10:/opt/wire/

# Run (or add to systemd — see below)
python3 /opt/wire/wire_server.py 8787
```

**Systemd service (recommended):**

```ini
[Unit]
Description=wire coordination server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/wire/wire_server.py 8787
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now wire-server
```

### WireGuard tools (every node)

**Linux (Debian/Ubuntu):**
```bash
apt install wireguard wireguard-tools
```

**Linux (RHEL/Fedora/CentOS):**
```bash
dnf install wireguard-tools
```

**macOS:**
```bash
brew install wireguard-tools wireguard-go
```

---

## Usage — CLI

### `wire up` — join the network

```bash
sudo wire up --server http://45.76.100.10:8787 --name mynode
```

After the first run, the server URL and node name are saved to config. Subsequent runs need no arguments:

```bash
sudo wire up
```

Options:

| Flag | Description | Default |
|---|---|---|
| `--server` / `-s` | Coordination server URL | saved config |
| `--name` / `-n` | This node's name | hostname |
| `--port` / `-p` | WireGuard listen port | 51820 |

---

### `wire status` — see the whole network

```bash
wire status
```

Output (like `tailscale status`):

```
wire status  http://45.76.100.10:8787
  4 online / 1 offline / 5 total

  ● g1              10.99.23.187     123.45.67.89          5s ago
  ● macbook         10.99.45.22      211.100.200.50        12s ago
  ● l1              10.99.87.3       192.168.1.10 (LAN)    8s ago
  ● v1              10.99.1.1        45.76.100.10          2s ago  (this node)
  ○ oldserver       10.99.200.5      203.0.113.10          14m ago
```

`●` = online (heartbeat within 5 minutes)
`○` = offline (no heartbeat for 5+ minutes, kept in list for 24 hours)

Use `--json` for machine-readable output:
```bash
wire status --json
```

---

### `wire peers` — list registered peers

```bash
wire peers
```

Shows all nodes currently registered on the coordination server.

---

### `wire ping` — ping a peer by name

```bash
wire ping g1
wire ping 10.99.23.187
```

Resolves node names to VPN IPs via the coordination server, then pings.

---

### `wire down` — leave the network

```bash
sudo wire down
```

Removes the WireGuard interface and stops the daemon. The node will be marked offline on the coordination server after 5 minutes.

---

### `wire install` — check WireGuard installation

```bash
wire install
```

Checks if WireGuard tools are installed and prints platform-specific install instructions if not.

---

## Usage — MCP

wire integrates with Claude AI via the Model Context Protocol. After adding `wire_mcp_server.py` to your Claude Desktop config, Claude can manage your VPN directly.

**Available tools:**

| Tool | What it does |
|---|---|
| `wire_status` | Show full network status — all nodes, online/offline, VPN IPs |
| `wire_up` | Bring up VPN tunnel |
| `wire_down` | Tear down VPN tunnel |
| `wire_peers` | List all registered peers |
| `wire_ping` | Ping a peer by name or VPN IP |
| `wire_install` | Check WireGuard installation |
| `wire_diagnose` | Full diagnostic: WG installed? server reachable? interface up? |
| `wire_watchdog` | Check peer handshakes, stale connections, service status |

**Claude Desktop config** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "wire": {
      "command": "python3",
      "args": ["/path/to/wire_mcp_server.py"]
    }
  }
}
```

The MCP server imports its logic from `wire_client.py`. CLI and MCP call the **same core functions** — `cmd_status`, `cmd_up`, `cmd_down`, `cmd_peers`, `cmd_ping` — so behavior is always identical between the two interfaces.

---

## Configuration

Config file locations:

| Context | Path |
|---|---|
| Root / system daemon | `/etc/wire/config.json` |
| Regular user | `~/.wire/config.json` |

Example config (written automatically by `wire up`):

```json
{
  "server_url":   "http://45.76.100.10:8787",
  "node_name":    "macbook",
  "node_id":      "a1b2c3d4e5f6...",
  "vpn_ip":       "10.99.45.22",
  "listen_port":  51820,
  "nat_port":     54321
}
```

Environment variables (override config):

| Variable | Description |
|---|---|
| `WIRE_VPN_SUBNET` | VPN subnet prefix (default: `10.99`) |
| `WIRE_STATE_FILE` | Server state file path (default: `/etc/wire/state.json`) |
| `WIRE_PORT` | Server listen port (default: `8787`) |

---

## Server API Reference

All endpoints return JSON.

### `POST /register`

Node heartbeat. Call every 30 seconds to stay online.

Request:
```json
{
  "node_id":       "string  (SHA-256 hash of hostname+MAC)",
  "node_name":     "string  (human-readable name, e.g. g1)",
  "wg_public_key": "string  (WireGuard public key, base64)",
  "port":          51820,
  "lan_ip":        "192.168.x.x  (optional)"
}
```

Response:
```json
{
  "ok":        true,
  "vpn_ip":    "10.99.x.x",
  "your_ip":   "1.2.3.4",
  "your_port": 54321
}
```

`your_ip` and `your_port` are the external IP and port the server observed — the NAT-mapped values, not what the client thinks it has.

---

### `GET /status`

Returns all nodes (online and offline). Used by `wire status`.

Response:
```json
{
  "version": "2.1.0",
  "total":   5,
  "online":  4,
  "offline": 1,
  "nodes": [
    {
      "node_name":     "g1",
      "vpn_ip":        "10.99.23.187",
      "public_ip":     "123.45.67.89",
      "nat_port":      51820,
      "status":        "online",
      "last_seen_ago": 5
    }
  ]
}
```

---

### `GET /peers`

Returns only **online** nodes. Used by the client daemon for WireGuard peer sync.

---

### `GET /stun`

Returns the caller's external IP and port as observed by the server. Used after `/register` to discover NAT-mapped port.

Response:
```json
{
  "ip":   "211.100.200.50",
  "port": 54321
}
```

---

### `GET /health`

Simple health check.

Response:
```json
{
  "ok":      true,
  "version": "2.1.0",
  "total":   5,
  "online":  4
}
```

---

### `POST /punch`

NAT hole-punch coordination. Called when a direct connection attempt fails.

Request:
```json
{
  "from_vpn_ip": "10.99.45.22",
  "to_vpn_ip":   "10.99.23.187"
}
```

Response includes `use_relay: true` after 3 failed attempts, signaling that a relay path should be used as fallback.

---

## File Reference

```
wire/
├── wire_server.py       Coordination server — run on one VPS
├── wire_client.py       VPN daemon + CLI — run on every node
│                          Exports: cmd_status, cmd_up, cmd_down,
│                                   cmd_peers, cmd_ping, cmd_install
├── wire_mcp_server.py   MCP wrapper for Claude AI
│                          Imports core functions from wire_client.py
│                          No duplicate logic
└── wire_agent.py        (optional) agent utilities

/etc/wire/
├── config.json          Node config (written by wire up)
├── private.key          WireGuard private key (chmod 600)
├── public.key           WireGuard public key
└── state.json           Server peer state (written by wire_server.py)
```

---

## Design Principles

**No central bottleneck.** The coordination server handles only small JSON messages (registration, peer lists). Your VPN traffic never touches it.

**Deterministic IPs.** VPN IPs are derived from a hash of the machine's identity. No DHCP, no race conditions, no manual assignment.

**Same logic everywhere.** `wire_client.py` exports the same functions used by both the CLI (`wire status`) and the MCP server (`wire_status` tool). They always behave identically.

**Offline tolerance.** Nodes keep their WireGuard peers configured even when the coordination server is unreachable. Established tunnels survive server restarts.

**NAT-transparent.** The `/stun` endpoint and `PersistentKeepalive = 25` together handle NAT traversal without any external STUN servers. The coordination server itself provides the NAT discovery service.
