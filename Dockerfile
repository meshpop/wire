# MeshPOP Wire Relay Server
FROM python:3.11-slim

WORKDIR /app

# Install WireGuard tools
RUN apt-get update && apt-get install -y \
    wireguard-tools \
    iproute2 \
    iptables \
    && rm -rf /var/lib/apt/lists/*

# Copy relay server
COPY server.py .
COPY client.py .
COPY meshpop-install.sh .

# Create data directory
RUN mkdir -p /data

EXPOSE 8786
EXPOSE 51820/udp

CMD ["python3", "server.py", "8786"]
