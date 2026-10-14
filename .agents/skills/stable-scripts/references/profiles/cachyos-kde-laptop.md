# Profile: CachyOS / KDE Plasma laptop

Load this only when the system matches (`ID=cachyos` in `/etc/os-release`) or the user says it is their machine. The facts come from the user's fastfetch output and were not verified on the machine itself, so still probe with `shutil.which()` before relying on a tool. General guidance is in `../environments.md`.

Lenovo IdeaPad 5 Pro 16ARH7 · CachyOS x86_64 (Arch-based, rolling) · KDE Plasma 6 on Wayland (KWin) · fish 4 as the interactive shell, bash also used · alacritty · btrfs root · Ryzen 5 6600HS, NVIDIA RTX 3050 Mobile + AMD Radeon 680M (hybrid graphics) · battery laptop.

## fish and bash
- A script with a shebang runs the same from fish or bash. Write nothing in fish syntax; shellcheck cannot lint fish.
- A script cannot change the caller's directory or environment. If the user wants that, write a fish function, or have the script print the path and let the user wrap it: `cd (NAME)`.
- If `NAME` is not found after installing to `~/.local/bin`, fish needs `fish_add_path ~/.local/bin` (run once; it persists).

## Arch and CachyOS
- Never `pacman -Sy` without `-u`: partial upgrades break Arch. Use `-Syu`, or `-S --needed <pkg>` for installs.
- Installed? `pacman -Qq <pkg>` exits 0 if yes. Package owning a file: `pacman -Qo <path>`.
- AUR helper: detect with `shutil.which("paru") or shutil.which("yay")`; do not assume one.
- shellcheck comes from the AUR package `shellcheck-bin` (the user's choice); install it with the AUR helper that `shutil.which` finds. Test with `command -v shellcheck`, not the package name.
- `sudo pacman ...` prompts for a password, so use `run_interactive()`, never inside `step()`. Prefer one `sudo` call over sudo scattered through the script.

## btrfs
- `df` misreports free space on btrfs. Use `btrfs filesystem usage /` (may need root) or `shutil.disk_usage` for a rough figure.
- Before a destructive or bulk change, snapshot if snapper or timeshift is present (check with `shutil.which`); otherwise say there is no snapshot.

## Wayland and KDE
- Clipboard: `wl-copy` / `wl-paste` (package `wl-clipboard`), not `xclip`.
- Notifications: `notify-send` (libnotify). Dialogs: `kdialog`. Plasma settings: `kreadconfig6` / `kwriteconfig6`.
- No `xdotool`-style input injection or screen scraping under Wayland; say so instead of writing something that silently fails.

## Hybrid graphics
- To run a program on the NVIDIA GPU, check for `prime-run` first; do not hard-code NVIDIA environment variables unless asked.

## Scheduling
Use a systemd user timer (see `../environments.md`). `Persistent=true` runs a missed job after wake-up, which suits a laptop.
