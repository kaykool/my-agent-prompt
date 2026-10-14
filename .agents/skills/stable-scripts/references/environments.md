# Target Environment: CachyOS / KDE Plasma 6 Laptop

This environment specification defines the exact host machine for all scripts generated or hardened by this skill.

## System Profile

- **Hardware**: Lenovo IdeaPad 5 Pro 16ARH7, AMD Ryzen 5 6600HS, NVIDIA RTX 3050 Mobile + AMD Radeon 680M (hybrid graphics), battery laptop.
- **Operating System**: CachyOS Linux x86_64 (Arch-based rolling release, Linux kernel).
- **Interactive Shell**: fish 4 (`/bin/fish`). Scripts are written in Python 3 or Bash (`#!/usr/bin/env bash`).
- **Desktop Environment**: KDE Plasma 6 running on Wayland (KWin).
- **Filesystem**: btrfs root (`/@` subvolume) with active snapper configuration.
- **Init & Service Manager**: systemd (user session active).

## Python

- **Interpreter**: Python 3.14+ at `/usr/bin/python3`.
- **Packaging constraint**: System Python is externally managed (PEP 668). Scripts must strictly use the standard library. If an external library is unavoidable, install via `pacman` or recommend a dedicated venv.
- **Shebang**: `#!/usr/bin/env python3` with `from __future__ import annotations`.

## Package Management (`pacman`)

- **Query installed**: `pacman -Qq PKG` (exits 0 if installed).
- **Find package owning file**: `pacman -Qo /path/to/file`.
- **Install**: `pacman -S --needed PKG`.
- **Warning**: Never run `pacman -Sy` alone; partial upgrades can break Arch/CachyOS. Always use `pacman -Syu` if refreshing package databases.
- **Privilege**: Sudo prompts for a password in terminal; invoke via `run_interactive(["sudo", "pacman", "-S", ...])` outside any `step()`. Prefer batching into a single sudo call.

## Filesystem & Snapshots (btrfs / snapper)

- **Disk usage**: Standard `df` misreports free space on btrfs. Use `btrfs filesystem usage /` (or `shutil.disk_usage` for approximate figures).
- **Pre-mutation safety**: `snapper` is configured on the root subvolume (`snapper list-configs` confirms `root` on `/`). Before running any destructive, bulk, or un-reversible mutation on the root filesystem, trigger a snapper pre-snapshot:
  ```bash
  snapper create -t pre -p -c number -d "Pre-script snapshot"
  ```

## Wayland & Desktop Integration (KDE Plasma 6)

- **Clipboard**: `wl-copy` and `wl-paste` (`wl-clipboard` 2.3+ installed). Do not use `xclip` or `xsel`.
- **Notifications**: `notify-send` (`libnotify`).
- **Dialogs / Prompts**: `kdialog` for desktop GUI prompts.
- **Plasma settings**: `kreadconfig6` and `kwriteconfig6` for reading/writing KDE Plasma configurations.
- **Input injection**: No `xdotool` input injection exists under Wayland. Do not write scripts relying on fake keypresses or X11 screen scraping.
- **GPU offload**: Use `prime-run <command>` to run specific compute or render tasks on the discrete NVIDIA RTX 3050 Mobile GPU.

## Shells, Installation, and PATH

- **Scripts**: Always write scripts with shebang `#!/usr/bin/env bash` or `#!/usr/bin/env python3`. Never write scripts in fish syntax (shellcheck cannot lint fish).
- **Environment mutation**: A script cannot modify the calling shell's directory or environment variables. Provide a fish function if environment manipulation is needed (e.g., `cd (script_output)`).
- **Installation**:
  ```bash
  mkdir -p ~/.local/bin && install -m 755 NAME.py ~/.local/bin/NAME
  ```
  Fish inherits `~/.local/bin` in PATH (`fish_add_path ~/.local/bin` if missing).
  When the source stays in a repo or another edited folder, symlink instead so the installed command is always the source:
  ```bash
  ln -sfn /path/to/NAME.py ~/.local/bin/NAME
  ```

## Scheduling (systemd user timers)

Use systemd user timers instead of cron. User timers integrate cleanly with systemd journal logging (`journalctl --user -u NAME`) and laptop sleep cycles.

`~/.config/systemd/user/NAME.service`:
```ini
[Unit]
Description=NAME

[Service]
Type=oneshot
ExecStart=%h/.local/bin/NAME
TimeoutStartSec=5min
```

`~/.config/systemd/user/NAME.timer`:
```ini
[Unit]
Description=Run NAME periodically

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

- `Persistent=true` ensures missed timer triggers during laptop sleep run immediately upon wake-up.
- Enable: `systemctl --user daemon-reload && systemctl --user enable --now NAME.timer`.
- In non-interactive systemd unit execution, stderr is not a terminal, and the template outputs clean `[ok]` / `[fail]` status lines without ANSI cursor escape codes.
