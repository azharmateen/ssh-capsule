"""Snapshot remote state: packages, services, ports, disk usage."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console

from .executor import SSHExecutor

console = Console()

SNAPSHOT_DIR = Path.home() / ".ssh-capsule" / "snapshots"


def take_snapshot(executor: SSHExecutor, host: str, label: str = "") -> dict:
    """Capture the current state of a remote server.

    Returns a snapshot dict with packages, services, ports, disk, etc.
    """
    snapshot = {
        "host": host,
        "label": label or "manual",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {},
    }

    # Installed packages
    console.print("  Collecting installed packages...")
    pkg_data = _get_packages(executor)
    snapshot["data"]["packages"] = pkg_data

    # Running services
    console.print("  Collecting running services...")
    snapshot["data"]["services"] = _get_services(executor)

    # Open ports
    console.print("  Collecting open ports...")
    snapshot["data"]["ports"] = _get_ports(executor)

    # Disk usage
    console.print("  Collecting disk usage...")
    snapshot["data"]["disk"] = _get_disk_usage(executor)

    # System info
    console.print("  Collecting system info...")
    snapshot["data"]["system"] = _get_system_info(executor)

    # Users
    console.print("  Collecting user list...")
    snapshot["data"]["users"] = _get_users(executor)

    return snapshot


def save_snapshot(snapshot: dict, host: str) -> str:
    """Save snapshot to disk. Returns the file path."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe_host = host.replace("@", "_at_").replace(":", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_host}_{ts}.json"
    filepath = SNAPSHOT_DIR / filename

    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2)

    return str(filepath)


def load_snapshots(host: Optional[str] = None) -> list[dict]:
    """Load all snapshots, optionally filtered by host."""
    if not SNAPSHOT_DIR.exists():
        return []

    snapshots = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        with open(path) as f:
            data = json.load(f)
        if host and data.get("host") != host:
            continue
        data["_file"] = str(path)
        snapshots.append(data)

    return snapshots


def load_latest_snapshot(host: str) -> Optional[dict]:
    """Load the most recent snapshot for a host."""
    snaps = load_snapshots(host)
    return snaps[-1] if snaps else None


def compare_snapshots(old: dict, new: dict) -> dict:
    """Compare two snapshots and return the diff."""
    diff = {}

    # Package diff
    old_pkgs = set(old.get("data", {}).get("packages", {}).get("names", []))
    new_pkgs = set(new.get("data", {}).get("packages", {}).get("names", []))
    diff["packages"] = {
        "added": sorted(new_pkgs - old_pkgs),
        "removed": sorted(old_pkgs - new_pkgs),
    }

    # Service diff
    old_svcs = set(old.get("data", {}).get("services", {}).get("names", []))
    new_svcs = set(new.get("data", {}).get("services", {}).get("names", []))
    diff["services"] = {
        "added": sorted(new_svcs - old_svcs),
        "removed": sorted(old_svcs - new_svcs),
    }

    # Port diff
    old_ports = set(str(p) for p in old.get("data", {}).get("ports", {}).get("listening", []))
    new_ports = set(str(p) for p in new.get("data", {}).get("ports", {}).get("listening", []))
    diff["ports"] = {
        "opened": sorted(new_ports - old_ports),
        "closed": sorted(old_ports - new_ports),
    }

    return diff


# --- Private helpers ---


def _get_packages(executor: SSHExecutor) -> dict:
    """Get installed package list."""
    # Try dpkg first, then rpm
    code, out, _ = executor.run(
        "dpkg-query -f '${Package}\n' -W 2>/dev/null || rpm -qa --qf '%{NAME}\n' 2>/dev/null || apk list -I 2>/dev/null | awk '{print $1}'",
        check=False,
    )
    names = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    return {"count": len(names), "names": names}


def _get_services(executor: SSHExecutor) -> dict:
    """Get running systemd services."""
    code, out, _ = executor.run(
        "systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null | awk '{print $1}'",
        check=False,
    )
    names = [line.strip().replace(".service", "") for line in out.splitlines() if line.strip()] if code == 0 else []
    return {"count": len(names), "names": names}


def _get_ports(executor: SSHExecutor) -> dict:
    """Get listening ports."""
    code, out, _ = executor.run(
        "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null",
        check=False,
    )
    ports = []
    if code == 0:
        for line in out.splitlines()[1:]:  # Skip header
            parts = line.split()
            if len(parts) >= 4:
                addr = parts[3]
                if ":" in addr:
                    port = addr.rsplit(":", 1)[-1]
                    try:
                        ports.append(int(port))
                    except ValueError:
                        pass
    return {"listening": sorted(set(ports))}


def _get_disk_usage(executor: SSHExecutor) -> dict:
    """Get disk usage summary."""
    code, out, _ = executor.run("df -h / --output=size,used,avail,pcent 2>/dev/null || df -h /", check=False)
    lines = out.strip().splitlines()
    if len(lines) >= 2:
        parts = lines[-1].split()
        if len(parts) >= 4:
            return {
                "total": parts[0] if len(parts) > 0 else "?",
                "used": parts[1] if len(parts) > 1 else "?",
                "available": parts[2] if len(parts) > 2 else "?",
                "percent": parts[3] if len(parts) > 3 else "?",
            }
    return {"raw": out}


def _get_system_info(executor: SSHExecutor) -> dict:
    """Get basic system information."""
    info = {}
    code, out, _ = executor.run("uname -a", check=False)
    if code == 0:
        info["uname"] = out

    code, out, _ = executor.run("cat /etc/os-release 2>/dev/null | head -5", check=False)
    if code == 0:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.lower()] = v.strip('"')

    code, out, _ = executor.run("nproc 2>/dev/null", check=False)
    if code == 0:
        info["cpus"] = out.strip()

    code, out, _ = executor.run("free -h 2>/dev/null | grep Mem | awk '{print $2}'", check=False)
    if code == 0 and out.strip():
        info["memory"] = out.strip()

    return info


def _get_users(executor: SSHExecutor) -> list[str]:
    """Get list of human users (UID >= 1000)."""
    code, out, _ = executor.run(
        "awk -F: '$3 >= 1000 && $3 < 65534 {print $1}' /etc/passwd",
        check=False,
    )
    if code == 0:
        return [u.strip() for u in out.splitlines() if u.strip()]
    return []
