# MeshPOP Architecture

MeshPOP is an infrastructure stack that lets AI and humans build, deploy, and operate services across a server mesh.

```
AI / CLI
   │
   ▼
┌─────────────────────────────────────────┐
│  mpop (control plane)                   │  pip install meshpop
│  ~60 MCP tools, monitoring, deployment  │
├─────────────────────────────────────────┤
│  vssh (transport)                       │  pip install vssh
│  SSH, file transfer, P2P, tunneling     │
├─────────────────────────────────────────┤
│  Wire (network)                         │  pip install meshpop-wire
│  WireGuard mesh VPN, NAT traversal      │
├─────────────────────────────────────────┤
│  14 cluster nodes                       │
│  VPS × 5, GPU × 4, Metal × 2,          │
│  Mac × 1, NAS × 2                      │
└─────────────────────────────────────────┘

  MeshDB (search)      pip install meshpop-db
  ← indexes everything, powers AI context →

  Vault (secrets)      pip install sv-vault
  ← identity & encryption across all layers →
```

## Layer 1 — Network: Wire

Mesh networking fabric. Every node connects to every other node through WireGuard tunnels.

- P2P priority with multi-relay fallback
- NAT traversal, auto-recovery watchdog
- Wire IP range: `10.99.x.x`, Tailscale failover: `100.x.x.x`

```bash
pip install meshpop-wire
wire --server http://coordinator:8787    # join mesh
wire-mcp                                 # MCP server (8 tools)
```

## Layer 2 — Transport: vssh

Distributed command and file transport daemon. Single TCP port (48291), single Python file, zero dependencies.

Every node runs vssh as both client and server (full mesh topology).

```
RPC          remote procedure call (12 methods)
SSH          command execution with PTY
PUT/GET      file transfer (MD5 skip, zlib, parallel streams)
SESSION      persistent connection for multiple commands
SYNC         directory delta transfer
P2P          NAT hole-punch for direct high-speed transfers
```

Protocol: `CMD:HMAC_TOKEN:args\n` — HMAC-SHA256, 60s timestamp window.

vssh is the bus that mpop, MeshDB, and all services use to talk to nodes.

```bash
pip install vssh
vssh status                              # check all nodes
vssh exec v1 "df -h"                    # run command
vssh-mcp                                 # MCP server (9 tools)
```

## Layer 3 — Control Plane: mpop

Fleet manager. Cluster orchestration, monitoring, deployment, AI integration.

```
mpop status      cluster health dashboard
mpop exec        run commands across nodes
mpop deploy      push code (uses vssh PUT internally)
mpop info        node telemetry (GPU, processes, security)
mpop secret      encrypted credential management
mpop heal        auto-detect problems and fix
mpop enforce      cold automated enforcement
mpop ask         natural language → mpop commands
```

mpop exposes **~60 MCP tools** organized into categories: Overview, Monitoring, Security, Network, Execution, FileOps, NAS, AI-Diagnostics, Config, Agents, Knowledge, Integration, and Media.

```bash
pip install meshpop
mpop status                              # dashboard
mpop-mcp                                 # MCP server (60+ tools)
```

## Layer 4 — Search: MeshDB

Distributed search engine across all servers. SQLite FTS5 + optional ChromaDB for semantic search.

- **244K+ files** indexed across 14 nodes
- Full-text search with BM25 ranking
- File pattern search across all servers
- Semantic code search via AI embeddings

```bash
pip install meshpop-db
meshdb search "nginx config"             # full-text search
meshdb-mcp                               # MCP server (5 tools)
```

MeshDB gives AI the context it needs to understand and modify code across the mesh.

## Layer 5 — Security: Vault

Identity and secrets for the cluster.

- **AES-256-GCM** authenticated encryption
- **Argon2id** memory-hard key derivation
- **Shamir's Secret Sharing** — split master key across N parties
- vssh transport-agnostic sync between nodes

```bash
pip install sv-vault
sv init                                  # create vault
sv add service/api_key                   # store secret
```

Transport encryption: WireGuard ChaCha20-Poly1305 (Wire VPN) + TLS 1.3 (P2P mode).

## The AI Loop

MeshPOP enables a closed loop where AI develops and operates services:

```
AI writes code
     ↓
mpop deploys (via vssh PUT)
     ↓
cluster runs
     ↓
AI reads logs (via vssh exec)
     ↓
AI fixes
     ↓
repeat
```

This is not theoretical — it's how this infrastructure was built. Claude wrote vssh features, deployed them to 14 nodes via vssh, tested them, and fixed issues, all in the same session.

## Quick Setup (for AI tools)

Install all components:

```bash
pip install vssh meshpop meshpop-wire meshpop-db sv-vault
```

Claude Code config (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "vssh":   { "command": "vssh-mcp" },
    "mpop":   { "command": "mpop-mcp" },
    "wire":   { "command": "wire-mcp" },
    "meshdb": { "command": "meshdb-mcp" }
  }
}
```

## Cluster Nodes

| Type | Nodes | Purpose |
|------|-------|---------|
| VPS | v1–v4, n1 | Web, relay, services |
| GPU | g1–g4 | AI inference, training |
| Bare Metal | d1–d2 | Heavy compute |
| Mac Studio | m1 | macOS development |
| NAS | s1–s2 | Storage (Synology DSM 7) |

## Component Repos

| Repo | PyPI | Description |
|------|------|-------------|
| [meshpop/vssh](https://github.com/meshpop/vssh) | `pip install vssh` | Distributed command & file transport |
| [meshpop/mpop](https://github.com/meshpop/mpop) | `pip install meshpop` | Control plane + 60 MCP tools |
| [meshpop/wire](https://github.com/meshpop/wire) | `pip install meshpop-wire` | WireGuard mesh VPN |
| [meshpop/meshdb](https://github.com/meshpop/meshdb) | `pip install meshpop-db` | Distributed search engine |
| [meshpop/vault](https://github.com/meshpop/vault) | `pip install sv-vault` | Secret management |

## One-line Summary

> AI writes the code. MeshPOP runs it.
