# ssh-capsule

**Bootstrap reproducible SSH environments on any server with one command.**

Define your server environment as a YAML capsule -- packages, runtimes, dotfiles, firewall rules, services, scripts -- and apply it to any server over SSH. Idempotent, snapshotable, rollbackable.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

---

## Why ssh-capsule?

- **One-command setup** -- Go from bare server to fully configured dev environment in a single command.
- **Declarative YAML** -- Define packages, runtimes (Python/Node/Rust/Go), users, firewall, systemd services, dotfiles, and scripts.
- **Idempotent** -- Every operation checks before applying. Run it again safely.
- **Snapshots** -- Capture server state (packages, services, ports, disk) before and after changes.
- **Rollback** -- Remove packages added since a snapshot.
- **Package manager detection** -- Works with apt, dnf, yum, apk, pacman, and brew.

## Quick Start

```bash
pip install ssh-capsule

# Generate a starter capsule
ssh-capsule init --name my-server -o capsule.yaml

# Preview what will happen (dry run)
ssh-capsule apply myserver.com --capsule capsule.yaml --dry-run

# Apply it
ssh-capsule apply root@myserver.com --capsule capsule.yaml

# Use a built-in template
ssh-capsule apply deploy@10.0.0.5 --capsule dev --key ~/.ssh/id_rsa

# Snapshot current state
ssh-capsule snapshot myserver.com

# List snapshots
ssh-capsule list --host myserver.com

# Rollback to a snapshot
ssh-capsule rollback myserver.com --to 0
```

## Capsule Format

```yaml
name: my-dev-env

packages:
  - git
  - curl
  - tmux
  - docker.io

runtimes:
  - name: python
    version: "3.12"
  - name: node
    version: "20"

users:
  - name: deploy
    shell: /bin/zsh
    sudo: true
    groups: [docker]
    ssh_keys:
      - "ssh-ed25519 AAAA... me@laptop"

env:
  TZ: UTC
  EDITOR: vim

firewall:
  - port: 22
    action: allow
  - port: 80
    action: allow
  - port: 443
    action: allow

services:
  - name: myapp
    exec_start: /usr/bin/python3 -m myapp
    user: deploy
    working_dir: /opt/myapp
    env:
      PORT: "8000"

scripts:
  - name: install-docker
    run: curl -fsSL https://get.docker.com | sh
    sudo: true
    check: docker --version >/dev/null 2>&1
```

## Features

### Modules Applied (in order)
1. **Packages** -- Auto-detects apt/dnf/yum/apk/pacman/brew. Checks before installing.
2. **Users** -- Creates users, adds to groups, configures sudo, deploys SSH keys.
3. **Dotfiles** -- Uploads local dotfiles to remote paths with correct ownership.
4. **Runtimes** -- Installs Python (pyenv), Node (nvm), Rust (rustup), Go (official tarball).
5. **Environment variables** -- Sets system-wide vars in `/etc/environment`.
6. **Firewall** -- Configures ufw or firewalld rules.
7. **Services** -- Creates systemd unit files, enables and starts services.
8. **Scripts** -- Runs custom scripts with optional guard commands (skip if check passes).

### Snapshots
```bash
# Capture state
ssh-capsule snapshot prod-server.com --label "before-deploy"

# Compare later
ssh-capsule snapshot prod-server.com --label "after-deploy"
```

Snapshots capture: installed packages, running services, listening ports, disk usage, system info, user list.

## License

MIT
