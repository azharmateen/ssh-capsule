"""SSH executor: connect via paramiko, run commands, upload files, handle sudo."""

import io
import os
import stat
import time
from pathlib import Path
from typing import Optional

import paramiko
from rich.console import Console

console = Console()


class SSHExecutor:
    """Execute commands and transfer files over SSH."""

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_file: Optional[str] = None,
        password: Optional[str] = None,
        sudo_password: Optional[str] = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key_file = key_file
        self.password = password
        self.sudo_password = sudo_password
        self.client: Optional[paramiko.SSHClient] = None

    def connect(self) -> None:
        """Establish SSH connection."""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
        }

        if self.key_file:
            connect_kwargs["key_filename"] = os.path.expanduser(self.key_file)
        elif self.password:
            connect_kwargs["password"] = self.password
        else:
            # Try default SSH agent / keys
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        self.client.connect(**connect_kwargs)
        console.print(f"[green]Connected to {self.user}@{self.host}:{self.port}[/green]")

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self.client:
            self.client.close()
            self.client = None

    def run(
        self,
        command: str,
        sudo: bool = False,
        check: bool = True,
        timeout: int = 120,
    ) -> tuple[int, str, str]:
        """Execute a command over SSH.

        Args:
            command: Shell command to execute
            sudo: Whether to run with sudo
            check: Whether to raise on non-zero exit
            timeout: Command timeout in seconds

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        if not self.client:
            raise RuntimeError("Not connected. Call connect() first.")

        if sudo and self.user != "root":
            if self.sudo_password:
                command = f"echo '{self.sudo_password}' | sudo -S bash -c '{command}'"
            else:
                command = f"sudo bash -c '{command}'"

        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()

        if check and exit_code != 0:
            console.print(f"[yellow]Command exited with code {exit_code}:[/yellow] {command[:80]}")
            if err:
                console.print(f"[red]stderr:[/red] {err[:500]}")

        return exit_code, out, err

    def run_check(self, command: str, sudo: bool = False) -> bool:
        """Run a command and return True if exit code is 0."""
        code, _, _ = self.run(command, sudo=sudo, check=False)
        return code == 0

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        mode: int = 0o644,
    ) -> None:
        """Upload a file to the remote host."""
        if not self.client:
            raise RuntimeError("Not connected.")

        sftp = self.client.open_sftp()
        try:
            # Ensure parent directory exists
            remote_dir = str(Path(remote_path).parent)
            self._ensure_remote_dir(sftp, remote_dir)

            sftp.put(local_path, remote_path)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def upload_content(
        self,
        content: str,
        remote_path: str,
        mode: int = 0o644,
    ) -> None:
        """Upload string content as a file to the remote host."""
        if not self.client:
            raise RuntimeError("Not connected.")

        sftp = self.client.open_sftp()
        try:
            remote_dir = str(Path(remote_path).parent)
            self._ensure_remote_dir(sftp, remote_dir)

            with sftp.open(remote_path, "w") as f:
                f.write(content)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the remote host."""
        if not self.client:
            raise RuntimeError("Not connected.")

        sftp = self.client.open_sftp()
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            sftp.get(remote_path, local_path)
        finally:
            sftp.close()

    def file_exists(self, remote_path: str) -> bool:
        """Check if a remote file exists."""
        if not self.client:
            raise RuntimeError("Not connected.")

        sftp = self.client.open_sftp()
        try:
            sftp.stat(remote_path)
            return True
        except FileNotFoundError:
            return False
        finally:
            sftp.close()

    def _ensure_remote_dir(self, sftp: paramiko.SFTPClient, path: str) -> None:
        """Recursively create remote directories if they don't exist."""
        parts = path.split("/")
        current = ""
        for part in parts:
            if not part:
                current = "/"
                continue
            current = f"{current}/{part}" if current != "/" else f"/{part}"
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()


def parse_host_string(host_str: str) -> dict:
    """Parse a host string like 'user@host:port' into components."""
    user = "root"
    port = 22
    host = host_str

    if "@" in host:
        user, host = host.rsplit("@", 1)

    if ":" in host:
        host, port_str = host.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            pass

    return {"host": host, "user": user, "port": port}
