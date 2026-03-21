# wire

**wire** is a self-hosted WireGuard mesh VPN — like Tailscale, but you own every component.

Any machine (VPS, home server, laptop, cloud instance) installs the same `wire_client.py`. One server runs `wire_server.py` as the coordination server. Every node registers with the server, discovers peers, and establishes direct encrypted tunnels — **the coordination server only facilitates introductions; your traffic never passes through it.**

---

## Table of Contents

1. [Architecture](#architecture)
2. [How It Works — Step by Step](#how-it-works)
3. [VPN IP Assignment](#vpn-ip-assignment)
4. [NAT Traversal and Hole Punching](#nat-traversal-and-hole-punching)
5. [UDP STUN — NAT Port Discovery](#udp-stun)
6. [Keeping Connections Alive](#keeping-connections-alive)
7. [Installation](#installation)
8. [Usage — CLI](#usage--cli)
9. [Usage — MCP (Claude AI)](#usage--mcp)
10. [Configuration](#configuration)
11. [Server API Reference](#server-api-reference)
12. [File Reference](#file-reference)
13. [Design Principles](#design-principles)

---

## Architecture

```
                ┌────────────────────────────────────────┐
                │  Coordination Server  (one always-on)  │
                │  wire_server.py                        │
                │  HTTP :8787  —  API                    │
                │  UDP  :8788  —  STUN (NAT discovery)   │
                │                                        │
                │  Knows: who exists, where they are     │
                │  Does NOT carry VPN traffic            │
                └──────────┬─────────────┬──────────────┘
                           │             │
               registers / │             │ registers /
               heartbeat   │             │ heartbeat
                           │             │
              ┌────────────▼──┐       ┌──▼────────────┐
              │  Node A       │       │  Node B        │
              │  wire_client  │       │  wire_client   │
              │  10.99.x.x    │       │  10.99.y.y     │
              └──────┬────────┘       └────────┬───────┘
                     │                         │
                     └────── direct P2P ───────┘
                            WireGuard tunnel
                            end-to-end encrypted
                            coordination server not involved
```

Three files, three roles:

| File | Role | Where it runs |
|---|---|---|
| `wire_server.py` | Coordination server + STUN | One always-on server |
| `wire_client.py` | VPN daemon + CLI | Every node |
| `wire_mcp_server.py` | MCP interface for Claude | Machines with Claude Desktop |

---

## How It Works

Here is the full flow using generic examples.

**Example network:**

```
server1 = always-on VPS  (runs wire_server.py, has public IP, no NAT)
node1   = server or desktop, direct public IP, no NAT
node2   = laptop, behind home router  (NAT — no direct public IP)
node3   = home server, behind router  (NAT — no direct public IP)
```

### Step 1 — Start the coordination server

```bash
# on server1
python3 wire_server.py
# or specify port:
python3 wire_server.py 8787
```

```
wire server v2.2.0
  HTTP :8787  — register / peers / status / health / ip / punch
  UDP  :8788  — STUN (NAT port discovery)
```

No nodes registered yet:

```bash
curl http://SERVER1_IP:8787/status
# → { "total": 0, "online": 0, "nodes": [] }
```

---

### Step 2 — node1 joins the network

```bash
# on node1
sudo python3 wire_client.py up \
  --server http://SERVER1_IP:8787 \
  --name node1
```

**What happens internally:**

**① Key generation** (once, then reused)

```
/etc/wire/private.key   ← never leaves this machine
/etc/wire/public.key    ← shared with coordination server
```

**② NAT port discovery via UDP STUN**

Before WireGuard starts, the client opens a UDP socket on port 51820 and sends a probe to the server's STUN port (8788):

```
node1 UDP :51820  →  SERVER1_IP:8788
server sees source: PUBLIC_IP:51820  (no NAT on node1, same port)
server replies:     {"ip": "PUBLIC_IP", "port": 51820}
```

The socket closes. WireGuard will use the same port.

**③ VPN IP assignment** (deterministic, no DHCP)

```python
node_id = sha256(hostname + mac_address)[:32]
vpn_ip  = f"10.99.{hash[0]}.{hash[1]}"   # e.g. 10.99.23.187
```

Same machine → same VPN IP, every time. No central allocation needed.

**④ WireGuard interface brought up**

```bash
# Linux
ip link add wire0 type wireguard
ip addr add 10.99.23.187/16 dev wire0
wg setconf wire0 /etc/wireguard/wire0.conf
ip link set wire0 up

# macOS
wireguard-go utun9
wg setconf utun9 ...
ifconfig utun9 inet 10.99.23.187 ...
```

**⑤ Registration with coordination server**

```
POST SERVER1_IP:8787/register
{
  "node_id":       "a1b2c3...",
  "node_name":     "node1",
  "wg_public_key": "XYZ...",
  "port":          51820,
  "nat_port":      51820,    ← discovered via UDP STUN
  "lan_ip":        "10.0.0.2"
}
```

Server records the node and replies:
```json
{ "ok": true, "vpn_ip": "10.99.23.187", "your_ip": "PUBLIC_IP_OF_NODE1" }
```

**⑥ Background daemon starts**

A thread runs silently: every 30 seconds it heartbeats `/register` and syncs peers from `/peers` into WireGuard.

---

### Step 3 — node2 joins (behind NAT)

```bash
# on node2 (laptop behind home router)
sudo python3 wire_client.py up \
  --server http://SERVER1_IP:8787 \
  --name node2
```

node2 is behind NAT. Its internal address is `192.168.x.x`. It does not know its public IP or what port its router assigned to its WireGuard traffic.

**UDP STUN probe from node2:**

```
node2 UDP :51820  →  SERVER1_IP:8788

node2's router NAT table:
  internal 192.168.x.x:51820  →  external ROUTER_IP:54321

server sees source: ROUTER_IP:54321
server replies:     {"ip": "ROUTER_IP", "port": 54321}
```

node2 now knows its real external UDP endpoint: `ROUTER_IP:54321`.

node2 sends `/register` with `nat_port: 54321`. The coordination server stores this.

---

### Step 4 — node1 and node2 discover each other

node1's daemon calls `/peers` and receives node2's entry:

```json
{
  "node_name":     "node2",
  "vpn_ip":        "10.99.45.22",
  "public_ip":     "ROUTER_IP",
  "nat_port":      54321,
  "wg_public_key": "PQR..."
}
```

node1 applies this to WireGuard:
```bash
wg set wire0 \
  peer PQR... \
  allowed-ips 10.99.45.22/32 \
  endpoint ROUTER_IP:54321 \
  persistent-keepalive 25
```

node2 does the same for node1. Both WireGuard instances now have each other as peers with correct endpoints.

---

## NAT Traversal and Hole Punching

When two nodes are both behind NAT, neither can receive incoming connections by default. WireGuard + PersistentKeepalive solves this:

```
node1  →  UDP  →  node2's router (ROUTER2_IP:54321)
node2  →  UDP  →  node1's router (ROUTER1_IP:44321)

node1's router NAT table entry: allow traffic from ROUTER2_IP:54321
node2's router NAT table entry: allow traffic from ROUTER1_IP:44321

Both packets arrive. Tunnel established.
```

This works for Full Cone, Restricted Cone, and Port-Restricted Cone NAT — which covers the vast majority of home routers. Symmetric NAT (rare, mostly corporate firewalls) requires the relay fallback (`/punch` → `use_relay: true`).

**The coordination server's role ends here.** It provided the addresses. It carries no VPN traffic.

---

## UDP STUN

Standard STUN servers (e.g. Google's `stun.l.google.com`) are external services. wire has no external dependencies — the coordination server itself provides STUN over UDP.

```
Client: UDP socket bound to WireGuard port (51820)
        → sends probe to SERVER:8788
Server: observes source IP:port after NAT translation
        → responds with {"ip": "...", "port": ...}
Client: closes socket, WireGuard binds to same port
```

The UDP port (8788) is always `HTTP_PORT + 1`. Configurable via `WIRE_PORT` environment variable.

**Why UDP, not TCP:**

NAT routers maintain separate mapping tables for TCP and UDP. A TCP connection to port 8787 reveals the TCP NAT mapping. WireGuard uses UDP. The only way to see the UDP NAT mapping for port 51820 is to send a UDP packet from port 51820 and observe what the server receives.

---

## Keeping Connections Alive

```
PersistentKeepalive = 25
```

Every peer gets this setting. A keepalive packet is sent every 25 seconds.

- Prevents NAT routers from expiring the UDP mapping (most expire after 30–120s of silence)
- Re-establishes the connection after an IP change (laptop switches from WiFi to mobile — next keepalive re-opens the path)
- No user action needed after initial setup

---

## Installation

### Coordination server

```bash
# Copy wire_server.py to your always-on server
scp wire_server.py user@YOUR_SERVER:/opt/wire/

# Run
python3 /opt/wire/wire_server.py

# Or with a custom port
python3 /opt/wire/wire_server.py 8787
```

**Systemd service (recommended for always-on servers):**

```ini
[Unit]
Description=wire coordination server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/wire/wire_server.py
Restart=always
RestartSec=5
Environment=WIRE_STATE_FILE=/etc/wire/state.json

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now wire-server
```

### WireGuard tools (every node)

```bash
# Debian / Ubuntu
apt install wireguard wireguard-tools

# RHEL / Fedora / CentOS
dnf install wireguard-tools

# Alpine
apk add wireguard-tools

# macOS
brew install wireguard-tools wireguard-go
```

Run `wire install` to check your platform and get the right command.

---

## Usage — CLI

### `wire up` — join the network

First time (server URL and name required):
```bash
sudo wire up --server http://YOUR_SERVER:8787 --name NODENAME
```

After first run the config is saved. Subsequent starts need no arguments:
```bash
sudo wire up
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--server` / `-s` | saved config | Coordination server URL |
| `--name` / `-n` | hostname | This node's name |
| `--port` / `-p` | `51820` | WireGuard listen port |

---

### `wire status` — see the whole network

```bash
wire status
```

Output:
```
wire status  http://YOUR_SERVER:8787
  3 online / 1 offline / 4 total

  ● node1          10.99.23.187     203.0.113.10          5s ago
  ● node2          10.99.45.22      198.51.100.20         12s ago  (this node)
  ● node3          10.99.87.3       192.0.2.30            8s ago
  ○ node4          10.99.200.5      203.0.113.40          14m ago
```

`●` online — heartbeat within 5 minutes  
`○` offline — no heartbeat for 5+ minutes, kept in list for 24 hours

```bash
wire status --json   # machine-readable output
```

---

### `wire peers` — list all registered nodes

```bash
wire peers
```

---

### `wire ping` — ping a peer by name

```bash
wire ping node1
wire ping 10.99.23.187
```

Resolves node names to VPN IPs via the coordination server, then pings.

---

### `wire down` — leave the network

```bash
sudo wire down
```

Removes the WireGuard interface and stops the daemon. The node will appear offline after 5 minutes.

---

### `wire install` — check WireGuard installation

```bash
wire install
```

Checks if WireGuard tools are present and prints platform-specific install instructions if not.

---

## Usage — MCP

Add `wire_mcp_server.py` to your Claude Desktop config:

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

**Available tools:**

| Tool | What it does |
|---|---|
| `wire_status` | Full network view — all nodes, online/offline, VPN IPs |
| `wire_up` | Bring up VPN tunnel |
| `wire_down` | Tear down VPN tunnel |
| `wire_peers` | List all registered peers |
| `wire_ping` | Ping a peer by name or VPN IP |
| `wire_install` | Check WireGuard installation |
| `wire_diagnose` | Full diagnostic: WG installed? server reachable? interface up? |
| `wire_watchdog` | Peer handshakes, stale connections, service status |

The MCP server imports all logic from `wire_client.py`. CLI and MCP call the **same core functions** — behavior is always identical between the two interfaces.

---

## Configuration

Config file locations:

| Context | Path |
|---|---|
| Root / system daemon | `/etc/wire/config.json` |
| Regular user | `~/.wire/config.json` |

Written automatically by `wire up`. Example:

```json
{
  "server_url":   "http://YOUR_SERVER:8787",
  "node_name":    "NODENAME",
  "node_id":      "a1b2c3d4e5f6...",
  "vpn_ip":       "10.99.x.x",
  "listen_port":  51820,
  "nat_port":     54321
}
```

Environment variables (server-side):

| Variable | Default | Description |
|---|---|---|
| `WIRE_PORT` | `8787` | HTTP listen port (UDP STUN = this + 1) |
| `WIRE_VPN_SUBNET` | `10.99` | VPN IP prefix |
| `WIRE_STATE_FILE` | `/etc/wire/state.json` | Peer state persistence path |

---

## Server API Reference

All HTTP endpoints return JSON.

### `POST /register`

Node heartbeat. Call every 30 seconds to stay online.

Request body:
```json
{
  "node_id":       "string  (SHA-256 of hostname+MAC, 32 hex chars)",
  "node_name":     "string  (human name, e.g. myserver)",
  "wg_public_key": "string  (WireGuard public key, base64)",
  "port":          51820,
  "nat_port":      54321,
  "lan_ip":        "192.168.x.x  (optional)"
}
```

`nat_port` is the WireGuard UDP port as seen from outside NAT, discovered via UDP STUN before calling this endpoint. If the node has no NAT, `nat_port` equals `port`.

Response:
```json
{
  "ok":      true,
  "vpn_ip":  "10.99.x.x",
  "your_ip": "1.2.3.4"
}
```

---

### `GET /status`

All nodes (online and offline). Used by `wire status`.

Response:
```json
{
  "version": "2.2.0",
  "total":   4,
  "online":  3,
  "offline": 1,
  "nodes": [
    {
      "node_name":     "node1",
      "vpn_ip":        "10.99.23.187",
      "public_ip":     "203.0.113.10",
      "nat_port":      51820,
      "status":        "online",
      "last_seen_ago": 5
    }
  ]
}
```

---

### `GET /peers`

Online nodes only. Used by the client daemon for WireGuard peer sync every 30 seconds.

---

### `GET /ip`

Returns the caller's public IP (TCP). Quick check only — not for WireGuard port discovery (use UDP STUN for that).

```json
{ "ip": "1.2.3.4" }
```

---

### `GET /health`

```json
{ "ok": true, "version": "2.2.0", "total": 4, "online": 3 }
```

---

### `POST /punch`

NAT hole-punch coordination. Called when a direct connection attempt fails. After 3 attempts the server sets `use_relay: true`, signaling that a relay path should be used.

Request:
```json
{ "from_vpn_ip": "10.99.x.x", "to_vpn_ip": "10.99.y.y" }
```

Response:
```json
{ "ok": true, "use_relay": false, "attempts": 1 }
```

---

### UDP STUN — port `HTTP_PORT + 1`

Send any UDP packet from your WireGuard port. Receive the NAT-mapped external IP:port.

```
Client → UDP packet (from port 51820) → SERVER:8788
Server → {"ip": "EXTERNAL_IP", "port": EXTERNAL_PORT}
```

---

## File Reference

```
wire/
├── wire_server.py       Coordination server + UDP STUN — run on one always-on server
├── wire_client.py       VPN daemon + CLI — run on every node
│                          Exports: cmd_status, cmd_up, cmd_down,
│                                   cmd_peers, cmd_ping, cmd_install
├── wire_mcp_server.py   MCP wrapper for Claude AI
│                          Imports core functions from wire_client.py
└── wire_agent.py        (optional) agent utilities

/etc/wire/               (root) or ~/.wire/ (user)
├── config.json          Node config — written by wire up
├── private.key          WireGuard private key (chmod 600)
├── public.key           WireGuard public key
└── state.json           Server peer state — written by wire_server.py
```

---

## Design Principles

**No hardcoded values.** No IP addresses, hostnames, or node names in the code. Everything comes from config files or CLI arguments.

**No external dependencies.** No Google STUN servers, no relay services, no cloud providers. The coordination server you run handles everything including NAT port discovery.

**No central bottleneck.** The coordination server handles only small JSON messages. VPN traffic flows directly between nodes.

**Deterministic VPN IPs.** Derived from each machine's own identity hash. No DHCP, no manual assignment, no conflicts.

**Same logic everywhere.** `wire_client.py` exports the same functions used by both the CLI and the MCP server. They always behave identically.

**Offline tolerance.** Nodes keep their WireGuard peers configured even when the coordination server is unreachable. Established tunnels survive server restarts.
