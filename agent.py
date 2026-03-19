import os
#!/usr/bin/env python3
"""Server Agent - reports server status to dashboard"""
import subprocess
import json
import socket
import time
import urllib.request
import urllib.error
import platform
import sys

DASHBOARD_URL = os.environ.get("WIRE_DASHBOARD_URL", "http://localhost:8800/api/report")
REPORT_INTERVAL = 30
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

def run_cmd(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""

def get_uptime():
    if IS_MACOS:
        boot = run_cmd("sysctl -n kern.boottime | awk '{print $4}' | tr -d ','")
        if boot:
            try:
                uptime_sec = int(time.time()) - int(boot)
                days, hours, mins = uptime_sec // 86400, (uptime_sec % 86400) // 3600, (uptime_sec % 3600) // 60
                return f"{days}d {hours}h {mins}m" if days > 0 else f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            except (ValueError, TypeError) as e:
                pass  # e silenced
        return run_cmd("uptime | awk '{print $3,$4}'").replace(",", "")
    return run_cmd("uptime -p 2>/dev/null").replace("up ", "") or "?"

def get_memory():
    result = {"memory": "?", "mem_pct": 0, "mem_used": 0, "mem_total": 0}
    if IS_MACOS:
        mem_info = run_cmd("""
page_size=$(pagesize 2>/dev/null || echo 16384)
stats=$(vm_stat 2>/dev/null)
total=$(sysctl -n hw.memsize 2>/dev/null)
if [ -n "$stats" ] && [ -n "$total" ]; then
  active=$(echo "$stats" | awk '/Pages active/ {gsub(/\\./, "", $3); print $3}')
  wired=$(echo "$stats" | awk '/Pages wired/ {gsub(/\\./, "", $4); print $4}')
  compressed=$(echo "$stats" | awk '/Pages occupied by compressor/ {gsub(/\\./, "", $5); print $5}')
  used=$(( (${active:-0} + ${wired:-0} + ${compressed:-0}) * $page_size ))
  total_gb=$((total / 1073741824))
  used_gb=$((used / 1073741824))
  pct=$((used * 100 / total))
  echo "${used_gb}G/${total_gb}G $used $total $pct"
fi""")
        if mem_info:
            parts = mem_info.split()
            if len(parts) >= 4:
                result["memory"] = parts[0]
                result["mem_used"] = int(parts[1]) if parts[1].isdigit() else 0
                result["mem_total"] = int(parts[2]) if parts[2].isdigit() else 0
                result["mem_pct"] = int(parts[3]) if parts[3].isdigit() else 0
    else:
        mem = run_cmd("LANG=C free -b 2>/dev/null | grep Mem | awk '{print $3, $2, int($3/$2*100)}'")
        if mem:
            parts = mem.split()
            if len(parts) >= 3:
                used, total, pct = int(parts[0]), int(parts[1]), int(parts[2])
                result["mem_used"], result["mem_total"], result["mem_pct"] = used, total, pct
                result["memory"] = f"{used//1073741824}Gi/{total//1073741824}Gi" if total > 1073741824 else f"{used//1048576}Mi/{total//1048576}Mi"
    return result

def get_disk():
    result = {"disk_used": "?", "disk_pct": 0, "disk_free": "?"}
    if IS_MACOS:
        # macOS APFS: /System/Volumes/Data is the real data volume
        disk = run_cmd("df -h /System/Volumes/Data 2>/dev/null | tail -1 | awk '{print $5, $4, $3, $2}'").split()
        if not disk or len(disk) < 2:
            disk = run_cmd("df -h / | tail -1 | awk '{print $5, $4, $3, $2}'").split()
    else:
        disk = run_cmd("df -h / | tail -1 | awk '{print $5, $4, $3, $2}'").split()
    if disk:
        result["disk_used"] = disk[0]
        try:
            result["disk_pct"] = int(disk[0].replace("%", ""))
        except (ValueError, TypeError) as e:
            pass  # e silenced
        if len(disk) > 1:
            result["disk_free"] = disk[1]
    return result

def get_load():
    if IS_MACOS:
        return run_cmd("sysctl -n vm.loadavg | awk '{print $2}'") or "?"
    return run_cmd("cat /proc/loadavg | awk '{print $1}'") or "?"

def get_vpn_ip():
    if IS_MACOS:
        vpn = run_cmd('for i in utun0 utun1 utun2 utun3 utun4 utun5 utun6 utun7 utun8 utun9; do IP=$(ifconfig $i 2>/dev/null | grep "inet 10.99" | awk "{print \\$2}"); [ -n "$IP" ] && echo $IP && break; done')
    else:
        vpn = run_cmd("ip addr show wire0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1")
        if not vpn:
            vpn = run_cmd("ip addr show wire0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1")
    return vpn if vpn and "10.99" in vpn else ""

def get_public_ip():
    return run_cmd("curl -s --connect-timeout 2 ifconfig.me 2>/dev/null") or "?"

def get_processes():
    procs = []
    # Docker
    docker = run_cmd("docker ps --format '{{.Names}}' 2>/dev/null | head -5")
    if docker:
        for name in docker.split('\n'):
            if name.strip():
                procs.append(f"docker:{name.strip()}")
    # Python servers
    py = run_cmd("pgrep -fa 'python.*serve|python.*server' 2>/dev/null | grep -v grep | awk '{print $NF}' | head -3")
    if py:
        for p in py.split('\n'):
            if p.strip():
                procs.append(f"python:{p.strip()}")
    # Node
    node = run_cmd("pgrep -fa 'node ' 2>/dev/null | grep -v grep | awk '{print $NF}' | head -3")
    if node:
        for n in node.split('\n'):
            if n.strip():
                procs.append(f"node:{n.strip()}")
    # Services
    for svc in ["nginx", "mysql", "postgres", "redis", "icecast", "liquidsoap"]:
        if run_cmd(f"pgrep {svc} >/dev/null 2>&1 && echo yes") == "yes":
            procs.append(svc)
    return procs[:10]

def get_ports():
    if IS_MACOS:
        ports = run_cmd("lsof -iTCP -sTCP:LISTEN -P -n 2>/dev/null | awk 'NR>1 {split($9,a,\":\"); print a[length(a)]}' | sort -nu | head -15")
    else:
        ports = run_cmd("ss -tlnp 2>/dev/null | awk 'NR>1 {split($4,a,\":\"); port=a[length(a)]; if(port ~ /^[0-9]+$/ && port > 0 && port < 65536) print port}' | sort -nu | head -15")
    return [int(p) for p in ports.split('\n') if p.strip().isdigit()][:15]

def get_services():
    services = {}
    for svc in ["wire", "vssh", "docker", "coturn", "icecast2", "liquidsoap-radio", "nginx", "postgresql", "redis"]:
        status = run_cmd(f"systemctl is-active {svc} 2>/dev/null")
        if status == "active":
            services[svc] = True
        elif status in ["inactive", "failed"]:
            services[svc] = False
    return services

def get_vssh():
    """Check vssh status"""
    result = {"running": False, "port": 0, "connections": 0, "bind": ""}

    # check vssh process
    vssh_proc = run_cmd("pgrep -fa 'vssh.*server' 2>/dev/null | head -1")
    if vssh_proc:
        result["running"] = True
        # extract port
        if "--ssh-port" in vssh_proc:
            try:
                port = vssh_proc.split("--ssh-port")[1].split()[0]
                result["port"] = int(port)
            except (ValueError, TypeError) as e:
                pass  # e silenced
        # extract bind address
        if "--bind" in vssh_proc:
            try:
                result["bind"] = vssh_proc.split("--bind")[1].split()[0]
            except (ValueError, TypeError) as e:
                pass  # e silenced

    # check connection count (by port)
    if result["port"]:
        conns = run_cmd(f"ss -tn 2>/dev/null | grep -c ':{result['port']}' || echo 0")
        try:
            result["connections"] = int(conns)
        except OSError as e:
            pass  # e silenced

    return result

def get_firewall():
    if IS_MACOS:
        return "pf:macOS"
    fw = run_cmd("""
        if command -v ufw >/dev/null 2>&1; then
            status=$(ufw status 2>/dev/null | head -1)
            if echo "$status" | grep -q "active"; then echo "ufw:active"; else echo "ufw:inactive"; fi
        elif command -v firewall-cmd >/dev/null 2>&1; then
            if firewall-cmd --state 2>/dev/null | grep -q running; then echo "firewalld:active"; else echo "firewalld:inactive"; fi
        else
            echo "none"
        fi
    """)
    return fw.strip() if fw else "unknown"

def get_security():
    """Security check"""
    issues = []

    # 1. SSH failed login attempts (last 1 hour)
    if IS_MACOS:
        ssh_fails = run_cmd("log show --predicate 'process == \"sshd\" && eventMessage contains \"Failed\"' --last 1h 2>/dev/null | wc -l").strip()
    else:
        ssh_fails = run_cmd("journalctl -u ssh -u sshd --since '1 hour ago' 2>/dev/null | grep -c 'Failed password' || grep -c 'Failed password' /var/log/auth.log 2>/dev/null || echo 0").strip()
    try:
        ssh_fail_count = int(ssh_fails)
        if ssh_fail_count > 10:
            issues.append({"level": "warning", "type": "ssh_bruteforce", "msg": f"SSH failures {ssh_fail_count} (1 hour)"})
        elif ssh_fail_count > 50:
            issues.append({"level": "critical", "type": "ssh_bruteforce", "msg": f"SSH attack suspected {ssh_fail_count} attempts"})
    except (ValueError, TypeError) as e:
        pass  # e silenced

    # 2. Root login enabled check
    if not IS_MACOS:
        root_login = run_cmd("grep -E '^PermitRootLogin' /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}'").strip()
        if root_login in ["yes", "without-password"]:
            issues.append({"level": "info", "type": "ssh_root", "msg": "Root SSH allowed"})

    # 3. Password authentication enabled check
    if not IS_MACOS:
        pwd_auth = run_cmd("grep -E '^PasswordAuthentication' /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}'").strip()
        if pwd_auth == "yes":
            issues.append({"level": "info", "type": "ssh_password", "msg": "SSH password auth allowed"})

    # 4. Dangerous open port check
    dangerous_ports = {23: "Telnet", 21: "FTP", 3389: "RDP", 5900: "VNC", 6379: "Redis(external)", 27017: "MongoDB(external)"}
    open_ports = get_ports()
    for port, name in dangerous_ports.items():
        if port in open_ports:
            issues.append({"level": "warning", "type": "dangerous_port", "msg": f"{name} port {port} open"})

    # 5. Low disk space
    disk_pct = int(run_cmd("df / | tail -1 | awk '{print $5}' | tr -d '%'") or "0")
    if disk_pct > 90:
        issues.append({"level": "critical", "type": "disk_full", "msg": f"Disk {disk_pct}% used"})
    elif disk_pct > 80:
        issues.append({"level": "warning", "type": "disk_warning", "msg": f"Disk {disk_pct}% used"})

    # 6. Low memory
    mem_info = get_memory()
    if mem_info.get("mem_pct", 0) > 90:
        issues.append({"level": "warning", "type": "memory_high", "msg": f"Memory {mem_info['mem_pct']}% used"})

    # 7. Zombie processes
    zombie = run_cmd("ps aux | grep -c ' Z ' 2>/dev/null || echo 0").strip()
    try:
        if int(zombie) > 5:
            issues.append({"level": "warning", "type": "zombie_procs", "msg": f"Zombie processes: {zombie}"})
    except (ValueError, TypeError) as e:
        pass  # e silenced

    return issues

def get_recent_logs():
    """Analyze recent logs"""
    logs = []

    if IS_MACOS:
        # macOS: recent error logs
        errors = run_cmd("log show --predicate 'messageType == error' --last 10m 2>/dev/null | tail -5")
    else:
        # Linux: extract errors/warnings from journalctl
        errors = run_cmd("journalctl -p err -n 10 --no-pager 2>/dev/null | tail -5")

    if errors:
        for line in errors.strip().split('\n')[:5]:
            if line.strip():
                logs.append({"level": "error", "msg": line.strip()[:100]})

    # OOM Killer detection
    if not IS_MACOS:
        oom = run_cmd("dmesg 2>/dev/null | grep -i 'out of memory' | tail -1")
        if oom:
            logs.append({"level": "critical", "msg": "OOM Killer triggered: " + oom[:80]})

    # Service failure detection
    if not IS_MACOS:
        failed_svc = run_cmd("systemctl --failed --no-pager 2>/dev/null | grep -E '●|failed' | head -3")
        if failed_svc and "0 loaded" not in failed_svc:
            for line in failed_svc.strip().split('\n'):
                if line.strip():
                    logs.append({"level": "warning", "msg": "Service failed: " + line.strip()[:60]})

    return logs

def get_status():
    status = {
        "hostname": socket.gethostname(),
        "timestamp": time.time(),
        "online": True,
        "os": "macOS" if IS_MACOS else "Linux",
        "uptime": get_uptime(),
        "load": get_load(),
    }
    status.update(get_memory())
    status.update(get_disk())
    status["vpn_ip"] = get_vpn_ip()
    status["public_ip"] = get_public_ip()
    status["processes"] = get_processes()
    status["ports"] = get_ports()
    status["services"] = get_services()
    status["vssh"] = get_vssh()
    status["firewall"] = get_firewall()
    status["security"] = get_security()
    status["logs"] = get_recent_logs()
    return status

def send_report(status):
    try:
        data = json.dumps(status).encode('utf-8')
        req = urllib.request.Request(
            DASHBOARD_URL,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        return False

def main():
    hostname = socket.gethostname()
    print(f"Server Agent starting on {hostname}")
    print(f"OS: {'macOS' if IS_MACOS else 'Linux'}, Reporting to: {DASHBOARD_URL}")
    sys.stdout.flush()

    while True:
        try:
            status = get_status()
            ok = send_report(status)
            print(f"[{time.strftime('%H:%M:%S')}] {'OK' if ok else 'FAIL'} | mem={status.get('memory')} disk={status.get('disk_used')} load={status.get('load')}")
            sys.stdout.flush()
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(REPORT_INTERVAL)

if __name__ == "__main__":
    main()
