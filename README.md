# Wire

**Self-hosted WireGuard mesh VPN with auto-discovery and NAT traversal.**

Part of [MeshPOP](https://mpop.dev) — Layer 1 (Network)

- Full-mesh WireGuard topology with automatic peer discovery
- NAT traversal for nodes behind firewalls
- AI-managed network configuration through MCP

## Install

```bash
pip install meshpop-wire
```

## Usage

```bash
# Check mesh status
wire status

# List connected peers
wire peers

# Diagnose connectivity
wire diagnose
```

## MCP Setup

```json
{
  "mcpServers": {
    "wire": { "command": "wire-mcp" }
  }
}
```

Gives AI agents: `wire_status`, `wire_peers`, `wire_connect`, `wire_diagnose`, `wire_add_node`, `wire_remove_node`

## Links

- Main project: [github.com/meshpop/mpop](https://github.com/meshpop/mpop)
- Website: [mpop.dev](https://mpop.dev)
- PyPI: [pypi.org/project/meshpop-wire](https://pypi.org/project/meshpop-wire/)

## License

MIT
