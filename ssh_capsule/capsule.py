"""Capsule spec loader: YAML file defining server environment."""

from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import yaml


@dataclass
class UserSpec:
    name: str
    shell: str = "/bin/bash"
    groups: list[str] = field(default_factory=list)
    ssh_keys: list[str] = field(default_factory=list)
    sudo: bool = False


@dataclass
class DotfileSpec:
    source: str  # Local path (relative to capsule file)
    dest: str  # Remote path (relative to home or absolute)
    owner: str = ""  # Username, default = connection user


@dataclass
class ServiceSpec:
    name: str
    exec_start: str
    description: str = ""
    user: str = "www-data"
    working_dir: str = ""
    env: dict[str, str] = field(default_factory=dict)
    restart: str = "always"
    after: str = "network.target"


@dataclass
class FirewallRule:
    port: int
    proto: str = "tcp"
    action: str = "allow"  # allow or deny
    source: str = ""  # CIDR or empty for any


@dataclass
class RuntimeSpec:
    name: str  # python, node, rust, go
    version: str = "latest"
    manager: str = ""  # pyenv, nvm, rustup -- auto-detected if empty


@dataclass
class ScriptSpec:
    name: str
    run: str  # Shell command(s)
    sudo: bool = False
    check: str = ""  # If this command succeeds, skip the run


@dataclass
class CapsuleSpec:
    """Full capsule specification for a server environment."""

    name: str = "default"
    packages: list[str] = field(default_factory=list)
    users: list[UserSpec] = field(default_factory=list)
    dotfiles: list[DotfileSpec] = field(default_factory=list)
    firewall: list[FirewallRule] = field(default_factory=list)
    services: list[ServiceSpec] = field(default_factory=list)
    runtimes: list[RuntimeSpec] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    scripts: list[ScriptSpec] = field(default_factory=list)


def load_capsule(path: str) -> CapsuleSpec:
    """Load a capsule spec from a YAML file."""
    filepath = Path(path)
    if not filepath.exists():
        raise FileNotFoundError(f"Capsule file not found: {path}")

    with open(filepath, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Invalid capsule format: expected a YAML mapping")

    spec = CapsuleSpec(name=raw.get("name", filepath.stem))

    # Packages
    spec.packages = raw.get("packages", [])

    # Users
    for u in raw.get("users", []):
        spec.users.append(
            UserSpec(
                name=u["name"],
                shell=u.get("shell", "/bin/bash"),
                groups=u.get("groups", []),
                ssh_keys=u.get("ssh_keys", []),
                sudo=u.get("sudo", False),
            )
        )

    # Dotfiles
    for d in raw.get("dotfiles", []):
        spec.dotfiles.append(
            DotfileSpec(
                source=d["source"],
                dest=d["dest"],
                owner=d.get("owner", ""),
            )
        )

    # Firewall
    for fw in raw.get("firewall", []):
        spec.firewall.append(
            FirewallRule(
                port=fw["port"],
                proto=fw.get("proto", "tcp"),
                action=fw.get("action", "allow"),
                source=fw.get("source", ""),
            )
        )

    # Services
    for svc in raw.get("services", []):
        spec.services.append(
            ServiceSpec(
                name=svc["name"],
                exec_start=svc["exec_start"],
                description=svc.get("description", ""),
                user=svc.get("user", "www-data"),
                working_dir=svc.get("working_dir", ""),
                env=svc.get("env", {}),
                restart=svc.get("restart", "always"),
                after=svc.get("after", "network.target"),
            )
        )

    # Runtimes
    for rt in raw.get("runtimes", []):
        spec.runtimes.append(
            RuntimeSpec(
                name=rt["name"],
                version=rt.get("version", "latest"),
                manager=rt.get("manager", ""),
            )
        )

    # Environment variables
    spec.env_vars = raw.get("env", {})

    # Scripts
    for sc in raw.get("scripts", []):
        spec.scripts.append(
            ScriptSpec(
                name=sc["name"],
                run=sc["run"],
                sudo=sc.get("sudo", False),
                check=sc.get("check", ""),
            )
        )

    return spec


def get_builtin_capsule(name: str) -> Optional[str]:
    """Get path to a built-in capsule template."""
    templates_dir = Path(__file__).parent / "templates"
    path = templates_dir / f"{name}.yaml"
    if path.exists():
        return str(path)
    return None


def list_builtin_capsules() -> list[str]:
    """List available built-in capsule templates."""
    templates_dir = Path(__file__).parent / "templates"
    if not templates_dir.exists():
        return []
    return [p.stem for p in sorted(templates_dir.glob("*.yaml"))]
