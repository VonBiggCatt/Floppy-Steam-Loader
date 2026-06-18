# Floppy → Steam launcher

Insert a floppy disk that contains a `game.id` file and the matching Steam
game launches automatically. No root needed at runtime.

## How it works

1. A small daemon (`floppy_steam_daemon.py`) watches udev for your floppy drive.
2. When a disk with a filesystem appears, it mounts it with `udisksctl`.
3. It reads `game.id` (a plain file holding a numeric Steam **AppID**).
4. It runs `steam steam://rungameid/<appid>`, then unmounts the disk.

Because it only reads a number and never executes anything from the disk,
inserting a disk can't run arbitrary code.

You also get a desktop notification (via `notify-send`) when a game launches,
or when a disk can't be mounted / has no valid `game.id`. Disable with
`--no-notify`. The launch notification shows the game's real name, resolved
from your local Steam install (`appmanifest_*.acf`), or the Steam store API if
the game isn't installed yet, falling back to the AppID if neither is reachable.

## Preparing a floppy

Format the floppy as FAT (most are already FAT12/FAT16) and put a file named
`game.id` on it containing the game's AppID, e.g.:

```
620
```

Find the AppID in the game's Steam store URL:
`https://store.steampowered.com/app/620/Portal_2/` → `620`.
See `game.id.example`.

## First-time setup

Find your floppy drive's device node (plug a disk in first):

```sh
python3 floppy_steam_daemon.py --list
```

Look for the line with `floppy=1` (or your USB drive). Auto-detect usually
works; if not, note the node (e.g. `/dev/sdb`) and pass `--device /dev/sdb`.

Test parsing on an already-mounted disk without launching anything:

```sh
python3 floppy_steam_daemon.py --test /run/media/$USER/<LABEL>
```

Run it in the foreground to watch it work:

```sh
python3 floppy_steam_daemon.py --verbose
# add --dry-run to detect/parse without actually starting Steam
```

## Make it permanent (systemd user service)

Once the foreground test works, install it as a user service so it starts
automatically with your desktop session (no root, no sudo).

1. Install and enable the service:

   ```sh
   mkdir -p ~/.config/systemd/user
   cp ~/Floppy-Steam-Loader/floppy-steam.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now floppy-steam.service
   ```

2. Make sure Steam and notifications can reach your display (run once):

   ```sh
   systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_RUNTIME_DIR
   ```

3. Confirm it's running and watch the logs:

   ```sh
   systemctl --user status floppy-steam.service
   journalctl --user -u floppy-steam.service -f
   ```

   Insert a disk — the log should show one launch, and pulling the disk should
   log "ready for next disk".

### Managing the service

```sh
systemctl --user restart floppy-steam.service   # after editing the script
systemctl --user stop floppy-steam.service       # stop until next login
systemctl --user disable --now floppy-steam.service   # turn off permanently
```

To force a specific drive, edit the `ExecStart` line in
`~/.config/systemd/user/floppy-steam.service` to add `--device /dev/sdX`, then
`systemctl --user daemon-reload && systemctl --user restart floppy-steam.service`.

### sway / bare wlroots compositors

The service is `WantedBy=graphical-session.target`. Full desktops (GNOME, KDE)
activate that target for you, so the service starts at login. A hand-rolled
compositor session — **sway**, Hyprland, river, etc. started from a TTY or a
minimal display manager — usually does **not**: `graphical-session.target`
stays `inactive (dead)`, so the service is enabled but never autostarts.

Check with:

```sh
systemctl --user is-active graphical-session.target   # "inactive" = affected
```

Fix it by adding a compositor session target that pulls the generic one up.
`graphical-session.target` refuses a manual `start`, so bind to it instead.
Create `~/.config/systemd/user/sway-session.target`:

```ini
[Unit]
Description=sway compositor session
Documentation=man:systemd.special(7)
BindsTo=graphical-session.target
Wants=graphical-session-pre.target
After=graphical-session-pre.target
```

Then start it from your compositor config *after* the session environment is
propagated to the user manager, so the daemon inherits `WAYLAND_DISPLAY` /
`DISPLAY` (it needs them to launch Steam and show notifications). In your sway
config (e.g. `~/.config/sway/config`):

```sh
exec sh -c 'dbus-update-activation-environment --systemd \
    WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE; \
    systemctl --user start sway-session.target'
```

Apply it to the running session without logging out:

```sh
systemctl --user daemon-reload
systemctl --user start sway-session.target
systemctl --user is-active floppy-steam.service   # should print "active"
```

On the next login, sway starts `sway-session.target`, which pulls up
`graphical-session.target`, which starts `floppy-steam.service`.

## Notes / troubleshooting

- **udisksctl asks for a password / fails:** your user must own the active
  login session (normal for a desktop login). Check with `loginctl`.
- **Disk inserted but nothing happens:** run with `--verbose`. If the drive
  isn't detected, use `--list` and pass the node with `--device`.
- **Game won't launch:** confirm Steam is logged in and the AppID is correct;
  try `steam steam://rungameid/<appid>` manually.
