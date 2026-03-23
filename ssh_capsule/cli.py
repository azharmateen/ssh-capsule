"""Click CLI for ssh-capsule: apply, snapshot, rollback, list, init."""

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .capsule import load_capsule, get_builtin_capsule, list_builtin_capsules, CapsuleSpec
from .executor import SSHExecutor, parse_host_string
from .modules import apply_capsule, detect_package_manager, install_packages
from .snapshot import (
    take_snapshot,
    save_snapshot,
    load_snapshots,
    load_latest_snapshot,
    compare_snapshots,
)

console = Console()


@click.group()
@click.version_option(package_name="ssh-capsule")
def cli():
    """ssh-capsule - Bootstrap reproducible SSH environments on any server."""
    pass


@cli.command()
@click.argument("host")
@click.option("--capsule", "-c", "capsule_path", required=True, help="Path to capsule YAML (or built-in name)")
@click.option("--key", "-k", "key_file", help="SSH private key path")
@click.option("--password", "-P", help="SSH password")
@click.option("--port", "-p", type=int, default=22, help="SSH port")
@click.option("--user", "-u", default="root", help="SSH user")
@click.option("--snapshot/--no-snapshot", default=True, help="Take before/after snapshots")
@click.option("--dry-run", is_flag=True, help="Parse capsule and show plan without executing")
def apply(host, capsule_path, key_file, password, port, user, snapshot, dry_run):
    """Apply a capsule spec to a remote host.

    HOST format: [user@]hostname[:port]

    Examples:
        ssh-capsule apply myserver.com --capsule dev.yaml
        ssh-capsule apply deploy@10.0.0.5 --capsule dev --key ~/.ssh/id_rsa
    """
    # Resolve capsule path
    resolved = _resolve_capsule(capsule_path)
    if not resolved:
        console.print(f"[red]Capsule not found: {capsule_path}[/red]")
        console.print(f"Available built-in capsules: {', '.join(list_builtin_capsules())}")
        sys.exit(1)

    spec = load_capsule(resolved)
    capsule_dir = str(Path(resolved).parent)

    console.print(Panel(f"[bold]Capsule: {spec.name}[/bold]\n"
                        f"Packages: {len(spec.packages)} | Users: {len(spec.users)} | "
                        f"Services: {len(spec.services)} | Runtimes: {len(spec.runtimes)} | "
                        f"Scripts: {len(spec.scripts)}", title="Plan"))

    if dry_run:
        _show_plan(spec)
        return

    # Parse host string for user/port overrides
    parsed = parse_host_string(host)
    ssh_host = parsed["host"]
    ssh_user = user if user != "root" else parsed["user"]
    ssh_port = port if port != 22 else parsed["port"]

    executor = SSHExecutor(
        host=ssh_host,
        user=ssh_user,
        port=ssh_port,
        key_file=key_file,
        password=password,
    )

    with executor:
        # Before snapshot
        if snapshot:
            console.print("\n[bold cyan]Taking before-snapshot...[/bold cyan]")
            before = take_snapshot(executor, host, label="before-apply")
            before_path = save_snapshot(before, host)
            console.print(f"[dim]Saved: {before_path}[/dim]")

        # Apply capsule
        console.print(f"\n[bold green]Applying capsule '{spec.name}' to {host}...[/bold green]")
        summary = apply_capsule(executor, spec, capsule_dir)

        # After snapshot
        if snapshot:
            console.print("\n[bold cyan]Taking after-snapshot...[/bold cyan]")
            after = take_snapshot(executor, host, label="after-apply")
            after_path = save_snapshot(after, host)
            console.print(f"[dim]Saved: {after_path}[/dim]")

            # Show diff
            diff = compare_snapshots(before, after)
            _show_diff(diff)

    console.print(f"\n[bold green]Capsule '{spec.name}' applied successfully![/bold green]")


@cli.command()
@click.argument("host")
@click.option("--key", "-k", "key_file", help="SSH private key path")
@click.option("--password", "-P", help="SSH password")
@click.option("--port", "-p", type=int, default=22, help="SSH port")
@click.option("--user", "-u", default="root", help="SSH user")
@click.option("--label", "-l", default="", help="Snapshot label")
def snapshot(host, key_file, password, port, user, label):
    """Take a snapshot of the current server state.

    Captures: installed packages, running services, open ports, disk usage, system info.
    """
    parsed = parse_host_string(host)
    executor = SSHExecutor(
        host=parsed["host"],
        user=user if user != "root" else parsed["user"],
        port=port if port != 22 else parsed["port"],
        key_file=key_file,
        password=password,
    )

    with executor:
        console.print(f"[bold]Taking snapshot of {host}...[/bold]")
        snap = take_snapshot(executor, host, label=label)
        filepath = save_snapshot(snap, host)

    data = snap["data"]
    console.print(f"\n[green]Snapshot saved: {filepath}[/green]")
    console.print(f"  Packages: {data['packages']['count']}")
    console.print(f"  Services: {data['services']['count']}")
    console.print(f"  Ports: {data['ports']['listening']}")
    console.print(f"  Disk: {data['disk']}")


@cli.command()
@click.argument("host")
@click.option("--to", "target_index", type=int, help="Rollback to snapshot index (from 'list')")
@click.option("--key", "-k", "key_file", help="SSH private key path")
@click.option("--password", "-P", help="SSH password")
@click.option("--port", "-p", type=int, default=22)
@click.option("--user", "-u", default="root")
def rollback(host, target_index, key_file, password, port, user):
    """Rollback: compare current state with a snapshot and remove added packages.

    This is a best-effort rollback that removes packages added since the snapshot.
    """
    snaps = load_snapshots(host)
    if not snaps:
        console.print(f"[red]No snapshots found for {host}[/red]")
        sys.exit(1)

    if target_index is not None:
        if target_index < 0 or target_index >= len(snaps):
            console.print(f"[red]Invalid index. Use 'ssh-capsule list' to see snapshots.[/red]")
            sys.exit(1)
        target = snaps[target_index]
    else:
        target = snaps[0]  # Earliest snapshot

    console.print(f"[bold]Rolling back to snapshot from {target['timestamp']}[/bold]")

    parsed = parse_host_string(host)
    executor = SSHExecutor(
        host=parsed["host"],
        user=user if user != "root" else parsed["user"],
        port=port if port != 22 else parsed["port"],
        key_file=key_file,
        password=password,
    )

    with executor:
        # Take current snapshot
        current = take_snapshot(executor, host, label="pre-rollback")
        diff = compare_snapshots(target, current)

        added_pkgs = diff["packages"]["added"]
        if not added_pkgs:
            console.print("[green]No packages to remove. State matches snapshot.[/green]")
            return

        console.print(f"Packages to remove ({len(added_pkgs)}): {', '.join(added_pkgs[:20])}")
        if not click.confirm("Proceed with rollback?"):
            return

        pm = detect_package_manager(executor)
        if pm == "apt":
            pkg_list = " ".join(added_pkgs)
            executor.run(f"apt-get remove -y {pkg_list}", sudo=True, check=False)
        elif pm in ("dnf", "yum"):
            pkg_list = " ".join(added_pkgs)
            executor.run(f"{pm} remove -y {pkg_list}", sudo=True, check=False)
        else:
            console.print(f"[yellow]Auto-rollback not supported for {pm}. Manual cleanup needed.[/yellow]")
            return

        console.print(f"[green]Rollback complete. Removed {len(added_pkgs)} packages.[/green]")


@cli.command("list")
@click.option("--host", "-h", "filter_host", help="Filter by host")
def list_cmd(filter_host):
    """List all saved snapshots."""
    snaps = load_snapshots(filter_host)
    if not snaps:
        console.print("[yellow]No snapshots found.[/yellow]")
        if not filter_host:
            console.print("[dim]Take one with: ssh-capsule snapshot <host>[/dim]")
        return

    table = Table(title="Saved Snapshots")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Host", style="bold")
    table.add_column("Label", style="green")
    table.add_column("Timestamp", style="dim")
    table.add_column("Packages", justify="right")
    table.add_column("Services", justify="right")

    for i, snap in enumerate(snaps):
        data = snap.get("data", {})
        table.add_row(
            str(i),
            snap.get("host", "?"),
            snap.get("label", ""),
            snap.get("timestamp", "?")[:19],
            str(data.get("packages", {}).get("count", "?")),
            str(data.get("services", {}).get("count", "?")),
        )
    console.print(table)


@cli.command()
@click.option("--name", "-n", default="my-capsule", help="Capsule name")
@click.option("--output", "-o", default="capsule.yaml", help="Output file path")
def init(name, output):
    """Generate a starter capsule YAML file."""
    import yaml

    starter = {
        "name": name,
        "packages": ["git", "curl", "wget", "htop", "tmux", "vim", "unzip"],
        "runtimes": [
            {"name": "python", "version": "3.12"},
            {"name": "node", "version": "20"},
        ],
        "env": {
            "TZ": "UTC",
            "EDITOR": "vim",
        },
        "firewall": [
            {"port": 22, "proto": "tcp", "action": "allow"},
            {"port": 80, "proto": "tcp", "action": "allow"},
            {"port": 443, "proto": "tcp", "action": "allow"},
        ],
        "scripts": [
            {
                "name": "set-timezone",
                "run": "timedatectl set-timezone UTC",
                "sudo": True,
                "check": "timedatectl show --property=Timezone --value | grep -q UTC",
            },
        ],
    }

    with open(output, "w") as f:
        yaml.dump(starter, f, default_flow_style=False, sort_keys=False)

    console.print(f"[green]Created starter capsule: {output}[/green]")
    console.print(f"[dim]Edit it, then run: ssh-capsule apply <host> --capsule {output}[/dim]")


@cli.command("templates")
def templates_cmd():
    """List available built-in capsule templates."""
    names = list_builtin_capsules()
    if not names:
        console.print("[yellow]No built-in templates found.[/yellow]")
        return

    table = Table(title="Built-in Capsule Templates")
    table.add_column("Name", style="cyan")
    table.add_column("File", style="dim")

    for name in names:
        path = get_builtin_capsule(name)
        table.add_row(name, path or "")
    console.print(table)
    console.print("\n[dim]Use: ssh-capsule apply <host> --capsule <name>[/dim]")


def _resolve_capsule(path_or_name: str) -> str | None:
    """Resolve capsule: try as file path first, then as built-in name."""
    if Path(path_or_name).exists():
        return path_or_name
    # Try built-in
    builtin = get_builtin_capsule(path_or_name)
    if builtin:
        return builtin
    # Try with .yaml extension
    if Path(f"{path_or_name}.yaml").exists():
        return f"{path_or_name}.yaml"
    return None


def _show_plan(spec: CapsuleSpec) -> None:
    """Display the capsule plan without executing."""
    if spec.packages:
        console.print(f"\n[bold]Packages ({len(spec.packages)}):[/bold]")
        console.print(f"  {', '.join(spec.packages)}")

    if spec.users:
        console.print(f"\n[bold]Users ({len(spec.users)}):[/bold]")
        for u in spec.users:
            console.print(f"  {u.name} (shell={u.shell}, sudo={u.sudo})")

    if spec.runtimes:
        console.print(f"\n[bold]Runtimes ({len(spec.runtimes)}):[/bold]")
        for rt in spec.runtimes:
            console.print(f"  {rt.name} {rt.version}")

    if spec.services:
        console.print(f"\n[bold]Services ({len(spec.services)}):[/bold]")
        for svc in spec.services:
            console.print(f"  {svc.name}: {svc.exec_start}")

    if spec.firewall:
        console.print(f"\n[bold]Firewall rules ({len(spec.firewall)}):[/bold]")
        for fw in spec.firewall:
            console.print(f"  {fw.action} {fw.port}/{fw.proto}")

    if spec.scripts:
        console.print(f"\n[bold]Scripts ({len(spec.scripts)}):[/bold]")
        for sc in spec.scripts:
            console.print(f"  {sc.name}: {sc.run[:60]}")


def _show_diff(diff: dict) -> None:
    """Display snapshot diff."""
    has_changes = False

    if diff["packages"]["added"]:
        has_changes = True
        console.print(f"\n[green]+{len(diff['packages']['added'])} packages added:[/green] {', '.join(diff['packages']['added'][:20])}")
    if diff["packages"]["removed"]:
        has_changes = True
        console.print(f"[red]-{len(diff['packages']['removed'])} packages removed:[/red] {', '.join(diff['packages']['removed'][:20])}")

    if diff["services"]["added"]:
        has_changes = True
        console.print(f"[green]+{len(diff['services']['added'])} services added:[/green] {', '.join(diff['services']['added'])}")
    if diff["services"]["removed"]:
        has_changes = True
        console.print(f"[red]-{len(diff['services']['removed'])} services removed:[/red] {', '.join(diff['services']['removed'])}")

    if diff["ports"]["opened"]:
        has_changes = True
        console.print(f"[green]+{len(diff['ports']['opened'])} ports opened:[/green] {', '.join(diff['ports']['opened'])}")
    if diff["ports"]["closed"]:
        has_changes = True
        console.print(f"[red]-{len(diff['ports']['closed'])} ports closed:[/red] {', '.join(diff['ports']['closed'])}")

    if not has_changes:
        console.print("\n[dim]No state changes detected.[/dim]")


if __name__ == "__main__":
    cli()
