"""Provisioning modules: packages, users, dotfiles, firewall, services, runtimes."""

from pathlib import Path
from typing import Optional

from rich.console import Console

from .capsule import (
    CapsuleSpec,
    UserSpec,
    DotfileSpec,
    ServiceSpec,
    FirewallRule,
    RuntimeSpec,
    ScriptSpec,
)
from .executor import SSHExecutor

console = Console()


def detect_package_manager(executor: SSHExecutor) -> str:
    """Detect the system package manager."""
    managers = [
        ("apt-get", "apt"),
        ("dnf", "dnf"),
        ("yum", "yum"),
        ("apk", "apk"),
        ("pacman", "pacman"),
        ("brew", "brew"),
        ("zypper", "zypper"),
    ]
    for cmd, name in managers:
        if executor.run_check(f"which {cmd}"):
            return name
    return "unknown"


def install_packages(
    executor: SSHExecutor, packages: list[str], pm: Optional[str] = None
) -> list[str]:
    """Install packages idempotently. Returns list of newly installed packages."""
    if not packages:
        return []

    if pm is None:
        pm = detect_package_manager(executor)

    installed = []

    # Build install command based on package manager
    install_cmds = {
        "apt": {
            "update": "apt-get update -qq",
            "install": "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq",
            "check": "dpkg -s {pkg} 2>/dev/null | grep -q 'Status: install ok installed'",
        },
        "dnf": {
            "update": "dnf check-update -q || true",
            "install": "dnf install -y -q",
            "check": "rpm -q {pkg} >/dev/null 2>&1",
        },
        "yum": {
            "update": "yum check-update -q || true",
            "install": "yum install -y -q",
            "check": "rpm -q {pkg} >/dev/null 2>&1",
        },
        "apk": {
            "update": "apk update -q",
            "install": "apk add -q",
            "check": "apk info -e {pkg} >/dev/null 2>&1",
        },
        "pacman": {
            "update": "pacman -Sy --noconfirm --quiet",
            "install": "pacman -S --noconfirm --quiet --needed",
            "check": "pacman -Q {pkg} >/dev/null 2>&1",
        },
        "brew": {
            "update": "brew update --quiet",
            "install": "brew install --quiet",
            "check": "brew list {pkg} >/dev/null 2>&1",
        },
    }

    if pm not in install_cmds:
        console.print(f"[red]Unknown package manager: {pm}[/red]")
        return []

    cmds = install_cmds[pm]

    # Filter out already-installed packages
    to_install = []
    for pkg in packages:
        check = cmds["check"].format(pkg=pkg)
        if executor.run_check(check, sudo=(pm != "brew")):
            console.print(f"  [dim]Already installed: {pkg}[/dim]")
        else:
            to_install.append(pkg)

    if not to_install:
        console.print("[green]  All packages already installed.[/green]")
        return []

    # Update package index
    console.print(f"  Updating package index ({pm})...")
    executor.run(cmds["update"], sudo=(pm != "brew"), check=False)

    # Install missing packages
    pkg_list = " ".join(to_install)
    console.print(f"  Installing: {pkg_list}")
    code, out, err = executor.run(
        f"{cmds['install']} {pkg_list}", sudo=(pm != "brew"), check=False
    )
    if code == 0:
        installed = to_install
        console.print(f"  [green]Installed {len(installed)} packages.[/green]")
    else:
        console.print(f"  [red]Package install failed (exit {code}).[/red]")

    return installed


def setup_user(executor: SSHExecutor, user: UserSpec) -> bool:
    """Create or update a user idempotently."""
    # Check if user exists
    if executor.run_check(f"id {user.name}"):
        console.print(f"  [dim]User '{user.name}' already exists.[/dim]")
    else:
        groups_arg = ""
        if user.groups:
            groups_arg = f"-G {','.join(user.groups)}"
        cmd = f"useradd -m -s {user.shell} {groups_arg} {user.name}"
        code, _, _ = executor.run(cmd, sudo=True, check=False)
        if code != 0:
            console.print(f"  [red]Failed to create user '{user.name}'.[/red]")
            return False
        console.print(f"  [green]Created user '{user.name}'.[/green]")

    # Add to sudo group if requested
    if user.sudo:
        executor.run(
            f"usermod -aG sudo {user.name} 2>/dev/null || usermod -aG wheel {user.name} 2>/dev/null",
            sudo=True, check=False,
        )

    # Add SSH keys
    if user.ssh_keys:
        home = f"/home/{user.name}"
        ssh_dir = f"{home}/.ssh"
        executor.run(f"mkdir -p {ssh_dir} && chmod 700 {ssh_dir}", sudo=True, check=False)

        for key in user.ssh_keys:
            # Check if key already added
            if not executor.run_check(f"grep -qF '{key}' {ssh_dir}/authorized_keys 2>/dev/null"):
                executor.run(
                    f"echo '{key}' >> {ssh_dir}/authorized_keys",
                    sudo=True, check=False,
                )
        executor.run(
            f"chmod 600 {ssh_dir}/authorized_keys && chown -R {user.name}:{user.name} {ssh_dir}",
            sudo=True, check=False,
        )
        console.print(f"  [green]Added {len(user.ssh_keys)} SSH key(s) for '{user.name}'.[/green]")

    return True


def upload_dotfiles(
    executor: SSHExecutor,
    dotfiles: list[DotfileSpec],
    capsule_dir: str,
    default_user: str = "",
) -> int:
    """Upload dotfiles to remote host. Returns count uploaded."""
    count = 0
    for df in dotfiles:
        local_path = Path(capsule_dir) / df.source
        if not local_path.exists():
            console.print(f"  [yellow]Dotfile not found: {local_path}[/yellow]")
            continue

        owner = df.owner or default_user or "root"
        remote_path = df.dest
        if not remote_path.startswith("/"):
            # Relative to home directory
            home = f"/home/{owner}" if owner != "root" else "/root"
            remote_path = f"{home}/{remote_path}"

        executor.upload_file(str(local_path), remote_path)
        executor.run(f"chown {owner}:{owner} {remote_path}", sudo=True, check=False)
        console.print(f"  [green]Uploaded {df.source} -> {remote_path}[/green]")
        count += 1

    return count


def configure_firewall(executor: SSHExecutor, rules: list[FirewallRule]) -> None:
    """Configure firewall rules using ufw or firewalld."""
    if not rules:
        return

    # Detect firewall tool
    if executor.run_check("which ufw"):
        _apply_ufw_rules(executor, rules)
    elif executor.run_check("which firewall-cmd"):
        _apply_firewalld_rules(executor, rules)
    else:
        console.print("  [yellow]No firewall tool found (ufw/firewalld). Skipping.[/yellow]")


def _apply_ufw_rules(executor: SSHExecutor, rules: list[FirewallRule]) -> None:
    """Apply rules using ufw."""
    for rule in rules:
        source_part = f"from {rule.source}" if rule.source else ""
        cmd = f"ufw {rule.action} {rule.port}/{rule.proto} {source_part}".strip()
        executor.run(cmd, sudo=True, check=False)
        console.print(f"  [green]ufw: {rule.action} {rule.port}/{rule.proto}[/green]")

    executor.run("ufw --force enable", sudo=True, check=False)


def _apply_firewalld_rules(executor: SSHExecutor, rules: list[FirewallRule]) -> None:
    """Apply rules using firewalld."""
    for rule in rules:
        if rule.action == "allow":
            cmd = f"firewall-cmd --permanent --add-port={rule.port}/{rule.proto}"
        else:
            cmd = f"firewall-cmd --permanent --remove-port={rule.port}/{rule.proto}"
        executor.run(cmd, sudo=True, check=False)
        console.print(f"  [green]firewalld: {rule.action} {rule.port}/{rule.proto}[/green]")

    executor.run("firewall-cmd --reload", sudo=True, check=False)


def setup_systemd_service(executor: SSHExecutor, service: ServiceSpec) -> None:
    """Create and enable a systemd service unit."""
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in service.env.items())
    work_dir = f"WorkingDirectory={service.working_dir}" if service.working_dir else ""

    unit = f"""[Unit]
Description={service.description or service.name}
After={service.after}

[Service]
Type=simple
User={service.user}
{work_dir}
ExecStart={service.exec_start}
Restart={service.restart}
RestartSec=5
{env_lines}

[Install]
WantedBy=multi-user.target
"""
    unit_path = f"/etc/systemd/system/{service.name}.service"

    # Check if service already exists and matches
    if executor.file_exists(unit_path):
        console.print(f"  [dim]Service '{service.name}' unit exists, updating...[/dim]")

    executor.upload_content(unit, unit_path, mode=0o644)
    executor.run("systemctl daemon-reload", sudo=True, check=False)
    executor.run(f"systemctl enable {service.name}", sudo=True, check=False)
    executor.run(f"systemctl restart {service.name}", sudo=True, check=False)
    console.print(f"  [green]Service '{service.name}' configured and started.[/green]")


def install_runtime(executor: SSHExecutor, runtime: RuntimeSpec) -> None:
    """Install a programming language runtime."""
    installers = {
        "python": _install_python,
        "node": _install_node,
        "nodejs": _install_node,
        "rust": _install_rust,
        "go": _install_go,
        "golang": _install_go,
    }

    installer = installers.get(runtime.name.lower())
    if installer:
        installer(executor, runtime.version)
    else:
        console.print(f"  [yellow]Unknown runtime: {runtime.name}[/yellow]")


def _install_python(executor: SSHExecutor, version: str) -> None:
    """Install Python via pyenv."""
    if not executor.run_check("which pyenv"):
        console.print("  Installing pyenv...")
        executor.run(
            'curl -fsSL https://pyenv.run | bash',
            check=False,
        )
        executor.run(
            'echo \'export PATH="$HOME/.pyenv/bin:$PATH"\' >> ~/.bashrc && '
            'echo \'eval "$(pyenv init -)"\' >> ~/.bashrc',
            check=False,
        )

    if version != "latest":
        console.print(f"  Installing Python {version} via pyenv...")
        executor.run(f"~/.pyenv/bin/pyenv install -s {version}", check=False, timeout=600)
        executor.run(f"~/.pyenv/bin/pyenv global {version}", check=False)


def _install_node(executor: SSHExecutor, version: str) -> None:
    """Install Node.js via nvm."""
    if not executor.run_check("bash -c 'source ~/.nvm/nvm.sh && nvm --version'"):
        console.print("  Installing nvm...")
        executor.run(
            'curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash',
            check=False,
        )

    node_version = version if version != "latest" else "--lts"
    console.print(f"  Installing Node.js {version} via nvm...")
    executor.run(
        f'bash -c "source ~/.nvm/nvm.sh && nvm install {node_version}"',
        check=False, timeout=300,
    )


def _install_rust(executor: SSHExecutor, version: str) -> None:
    """Install Rust via rustup."""
    if not executor.run_check("which rustup"):
        console.print("  Installing Rust via rustup...")
        executor.run(
            'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y',
            check=False, timeout=300,
        )

    if version != "latest":
        executor.run(f"~/.cargo/bin/rustup install {version}", check=False)
        executor.run(f"~/.cargo/bin/rustup default {version}", check=False)


def _install_go(executor: SSHExecutor, version: str) -> None:
    """Install Go from official tarball."""
    if executor.run_check("which go"):
        code, out, _ = executor.run("go version", check=False)
        if code == 0:
            console.print(f"  [dim]Go already installed: {out}[/dim]")
            return

    go_ver = version if version != "latest" else "1.22.5"
    console.print(f"  Installing Go {go_ver}...")
    executor.run(
        f'curl -fsSL https://go.dev/dl/go{go_ver}.linux-amd64.tar.gz | tar -C /usr/local -xzf -',
        sudo=True, check=False, timeout=300,
    )
    executor.run(
        'echo \'export PATH=$PATH:/usr/local/go/bin\' >> /etc/profile.d/go.sh',
        sudo=True, check=False,
    )


def set_env_vars(executor: SSHExecutor, env_vars: dict[str, str]) -> None:
    """Set environment variables in /etc/environment."""
    if not env_vars:
        return
    for key, value in env_vars.items():
        # Idempotent: remove old line, add new
        executor.run(
            f"sed -i '/^{key}=/d' /etc/environment && echo '{key}={value}' >> /etc/environment",
            sudo=True, check=False,
        )
        console.print(f"  [green]Set {key}[/green]")


def run_scripts(executor: SSHExecutor, scripts: list[ScriptSpec]) -> list[dict]:
    """Run custom scripts. Returns list of results."""
    results = []
    for script in scripts:
        # Check guard: skip if check command succeeds
        if script.check:
            if executor.run_check(script.check, sudo=script.sudo):
                console.print(f"  [dim]Script '{script.name}' skipped (check passed).[/dim]")
                results.append({"name": script.name, "status": "skipped"})
                continue

        console.print(f"  Running script: {script.name}")
        code, out, err = executor.run(script.run, sudo=script.sudo, check=False, timeout=300)
        status = "ok" if code == 0 else "failed"
        if code != 0:
            console.print(f"  [red]Script '{script.name}' failed (exit {code})[/red]")
        else:
            console.print(f"  [green]Script '{script.name}' completed.[/green]")

        results.append({"name": script.name, "status": status, "exit_code": code})

    return results


def apply_capsule(executor: SSHExecutor, spec: CapsuleSpec, capsule_dir: str = ".") -> dict:
    """Apply a full capsule spec to a connected host.

    Returns summary dict with results from each module.
    """
    summary = {"name": spec.name, "modules": {}}

    # 1. Packages
    if spec.packages:
        console.print("\n[bold]Installing packages...[/bold]")
        pm = detect_package_manager(executor)
        installed = install_packages(executor, spec.packages, pm)
        summary["modules"]["packages"] = {
            "manager": pm,
            "requested": len(spec.packages),
            "installed": len(installed),
        }

    # 2. Users
    if spec.users:
        console.print("\n[bold]Setting up users...[/bold]")
        user_results = []
        for user in spec.users:
            ok = setup_user(executor, user)
            user_results.append({"name": user.name, "ok": ok})
        summary["modules"]["users"] = user_results

    # 3. Dotfiles
    if spec.dotfiles:
        console.print("\n[bold]Uploading dotfiles...[/bold]")
        count = upload_dotfiles(executor, spec.dotfiles, capsule_dir)
        summary["modules"]["dotfiles"] = {"uploaded": count}

    # 4. Runtimes
    if spec.runtimes:
        console.print("\n[bold]Installing runtimes...[/bold]")
        for rt in spec.runtimes:
            install_runtime(executor, rt)
        summary["modules"]["runtimes"] = [
            {"name": rt.name, "version": rt.version} for rt in spec.runtimes
        ]

    # 5. Environment variables
    if spec.env_vars:
        console.print("\n[bold]Setting environment variables...[/bold]")
        set_env_vars(executor, spec.env_vars)
        summary["modules"]["env_vars"] = list(spec.env_vars.keys())

    # 6. Firewall
    if spec.firewall:
        console.print("\n[bold]Configuring firewall...[/bold]")
        configure_firewall(executor, spec.firewall)
        summary["modules"]["firewall"] = len(spec.firewall)

    # 7. Services
    if spec.services:
        console.print("\n[bold]Setting up services...[/bold]")
        for svc in spec.services:
            setup_systemd_service(executor, svc)
        summary["modules"]["services"] = [s.name for s in spec.services]

    # 8. Scripts
    if spec.scripts:
        console.print("\n[bold]Running scripts...[/bold]")
        script_results = run_scripts(executor, spec.scripts)
        summary["modules"]["scripts"] = script_results

    return summary
