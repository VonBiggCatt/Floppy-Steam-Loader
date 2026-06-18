#!/usr/bin/env python3
"""Watch for an inserted floppy disk and auto-launch a Steam game from it.

When a disk is inserted into a (USB) floppy drive, this daemon mounts it,
reads a marker file containing a numeric Steam AppID, and launches the game
with `steam steam://rungameid/<appid>`.

Detection uses udev (via pyudev); mounting uses udisksctl, so the whole thing
runs as your normal user with no root/sudo.

Run `floppy_steam_daemon.py --help` for options, or `--list` to see candidate
drives, or `--test PATH` to dry-run the marker-file parsing on a directory.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import pyudev

# Name of the marker file on the disk, and the max bytes we'll read from it.
MARKER_FILENAME = "game.id"
MARKER_MAX_BYTES = 4096

# Steam AppIDs are positive integers (e.g. 620 = Portal 2).
APPID_RE = re.compile(r"^[0-9]+$")

# Desktop notifications. Disabled with --no-notify (see argparse).
NOTIFY_APP_NAME = "Floppy Steam Launcher"
_notify_enabled = True
_notify_available: bool | None = None  # lazily resolved

log = logging.getLogger("floppy-steam")


def notify(summary: str, body: str = "", urgency: str = "normal") -> None:
    """Show a desktop notification, if possible. Never raises."""
    global _notify_available
    if not _notify_enabled:
        return
    if _notify_available is None:
        _notify_available = shutil.which("notify-send") is not None
        if not _notify_available:
            log.debug("notify-send not found; desktop notifications disabled")
    if not _notify_available:
        return
    try:
        subprocess.run(
            ["notify-send", "--app-name", NOTIFY_APP_NAME,
             "--urgency", urgency, "--icon", "input-gaming",
             summary, body],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("notify-send failed: %s", e)


# --------------------------------------------------------------------------- #
# Device matching
# --------------------------------------------------------------------------- #
def is_floppy(device: pyudev.Device, force_device: str | None) -> bool:
    """Return True if `device` is the floppy drive we care about.

    If --device was given, match strictly on that node (e.g. /dev/sdb).
    Otherwise match drives udev tagged as floppies, falling back to small
    removable USB disks.
    """
    if device.get("DEVTYPE") != "disk":
        return False

    if force_device:
        return device.device_node == force_device

    if device.get("ID_DRIVE_FLOPPY") == "1":
        return True

    # Fallback heuristic: a removable USB disk (covers floppy emulators that
    # don't set ID_DRIVE_FLOPPY).
    removable = _read_sysfs_int(device, "removable") == 1
    return removable and device.get("ID_BUS") == "usb"


def _read_sysfs_int(device: pyudev.Device, attr: str) -> int | None:
    try:
        raw = device.attributes.get(attr)
        return int(raw) if raw is not None else None
    except (ValueError, KeyError):
        return None


def has_media(device: pyudev.Device) -> bool:
    """True if a usable filesystem is present on the inserted media.

    udev probes the media on insertion and sets ID_FS_USAGE=filesystem once it
    recognizes a filesystem (e.g. vfat on a FAT-formatted floppy).
    """
    return device.get("ID_FS_USAGE") == "filesystem"


# --------------------------------------------------------------------------- #
# Mount / unmount via udisksctl (rootless, polkit-mediated)
# --------------------------------------------------------------------------- #
def mount(device_node: str) -> str | None:
    """Mount `device_node` and return its mountpoint, or None on failure."""
    try:
        out = subprocess.run(
            ["udisksctl", "mount", "--no-user-interaction", "-b", device_node],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error("mount failed to run: %s", e)
        return None

    text = (out.stdout or "") + (out.stderr or "")
    # Success line: "Mounted /dev/sdb at /run/media/user/FLOPPY."
    m = re.search(r" at (.+?)\.?\s*$", text.strip(), re.MULTILINE)
    if out.returncode == 0 and m:
        return m.group(1)
    # Already mounted: "...already mounted at `/run/media/user/FLOPPY'"
    m = re.search(r"already mounted at [`'\"]?([^`'\"]+)", text)
    if m:
        return m.group(1)

    log.error("mount failed (rc=%s): %s", out.returncode, text.strip())
    return None


def unmount(device_node: str) -> None:
    try:
        subprocess.run(
            ["udisksctl", "unmount", "--no-user-interaction", "-b", device_node],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("unmount failed: %s", e)


# --------------------------------------------------------------------------- #
# Marker file -> AppID
# --------------------------------------------------------------------------- #
def read_appid(mountpoint: str) -> str | None:
    """Read and validate the Steam AppID from the marker file on the disk."""
    path = os.path.join(mountpoint, MARKER_FILENAME)
    if not os.path.isfile(path):
        log.warning("no %s on disk at %s", MARKER_FILENAME, mountpoint)
        notify("No game on this disk",
               f"Couldn't find a {MARKER_FILENAME} file.", urgency="critical")
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(MARKER_MAX_BYTES)
    except OSError as e:
        log.error("could not read %s: %s", path, e)
        notify("Couldn't read the disk", str(e), urgency="critical")
        return None

    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if APPID_RE.match(line):
            return line
        log.warning("ignoring non-numeric marker line: %r", line)
        notify("Invalid game disk",
               f"{MARKER_FILENAME} must contain a numeric Steam AppID.",
               urgency="critical")
        return None
    log.warning("marker file %s contained no AppID", path)
    notify("Invalid game disk",
           f"{MARKER_FILENAME} contained no Steam AppID.", urgency="critical")
    return None


# --------------------------------------------------------------------------- #
# AppID -> human-readable game name
# --------------------------------------------------------------------------- #
def _steam_roots() -> list[str]:
    """Candidate Steam install roots, de-duplicated by realpath."""
    candidates = [
        "~/.steam/steam", "~/.steam/root", "~/.local/share/Steam",
        "~/.var/app/com.valvesoftware.Steam/data/Steam",  # Flatpak
    ]
    roots, seen = [], set()
    for c in candidates:
        real = os.path.realpath(os.path.expanduser(c))
        if os.path.isdir(real) and real not in seen:
            seen.add(real)
            roots.append(real)
    return roots


def _library_steamapps_dirs() -> list[str]:
    """All `steamapps` dirs across every configured Steam library folder."""
    dirs, seen = [], set()
    for root in _steam_roots():
        base = os.path.join(root, "steamapps")
        if os.path.isdir(base):
            seen.add(base)
            dirs.append(base)
        vdf = os.path.join(base, "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for path in re.findall(r'"path"\s*"([^"]+)"', text):
            d = os.path.join(path.replace("\\\\", "/"), "steamapps")
            if os.path.isdir(d) and d not in seen:
                seen.add(d)
                dirs.append(d)
    return dirs


def name_from_manifest(appid: str) -> str | None:
    """Read the game name from a local appmanifest_<appid>.acf, if installed."""
    for steamapps in _library_steamapps_dirs():
        acf = os.path.join(steamapps, f"appmanifest_{appid}.acf")
        try:
            with open(acf, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        m = re.search(r'"name"\s*"([^"]*)"', text)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def name_from_web(appid: str, timeout: float = 4.0) -> str | None:
    """Resolve the game name via the Steam store API (needs network)."""
    url = (f"https://store.steampowered.com/api/appdetails"
           f"?appids={appid}&filters=basic")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "floppy-steam"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as e:  # network/JSON/etc. — name is best-effort only
        log.debug("store API lookup failed for %s: %s", appid, e)
        return None
    entry = data.get(str(appid)) if isinstance(data, dict) else None
    if isinstance(entry, dict) and entry.get("success"):
        name = (entry.get("data") or {}).get("name")
        if name:
            return str(name).strip()
    return None


def resolve_game_name(appid: str) -> str | None:
    """Best-effort game name: local manifest first, then the store API."""
    return name_from_manifest(appid) or name_from_web(appid)


def launch_steam(appid: str, dry_run: bool = False) -> None:
    url = f"steam://rungameid/{appid}"
    name = resolve_game_name(appid)
    label = f"{name} (AppID {appid})" if name else f"Steam AppID {appid}"
    title = name or f"Steam AppID {appid}"
    log.info("launching %s -> %s", label, url)
    if dry_run:
        log.info("[dry-run] would run: steam %s", url)
        notify("Launching game (dry-run)", title)
        return
    try:
        subprocess.Popen(
            ["steam", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        notify("Launching game", title)
    except OSError as e:
        log.error("failed to launch steam: %s", e)
        notify("Failed to launch Steam", str(e), urgency="critical")


# --------------------------------------------------------------------------- #
# Handling an inserted disk
# --------------------------------------------------------------------------- #
def handle_disk(device_node: str, keep_mounted: bool, dry_run: bool) -> None:
    log.info("disk detected on %s", device_node)
    mountpoint = mount(device_node)
    if not mountpoint:
        notify("Couldn't mount the disk",
               f"Mounting {device_node} failed.", urgency="critical")
        return
    log.info("mounted at %s", mountpoint)
    try:
        appid = read_appid(mountpoint)
        if appid:
            launch_steam(appid, dry_run=dry_run)
    finally:
        if not keep_mounted:
            unmount(device_node)
            log.info("unmounted %s", device_node)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    context = pyudev.Context()

    # Handle a disk that's already inserted when we start.
    for device in context.list_devices(subsystem="block"):
        if is_floppy(device, args.device) and has_media(device):
            handle_disk(device.device_node, args.keep_mounted, args.dry_run)

    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("block")
    log.info("watching for floppy disks (device filter: %s)...",
             args.device or "auto-detect")

    # A floppy drive (and our own mount/unmount) emits a stream of `change`
    # events while a disk just sits in the drive. To avoid relaunching the
    # game on every one, we launch ONCE per insertion: a node goes into
    # `armed` when handled, and is only re-armed once the disk is removed
    # (a change event with no filesystem, or the drive is unplugged).
    armed: set[str] = set()
    last_seen: dict[str, float] = {}
    for device in iter(monitor.poll, None):
        if device.action not in ("add", "change", "remove"):
            continue
        node = device.device_node

        # Drive unplugged: re-arm so the next disk launches.
        if device.action == "remove":
            if node in armed:
                armed.discard(node)
                log.info("%s removed; ready for next disk", node)
            continue

        if not is_floppy(device, args.device):
            continue

        # Disk ejected (drive present, but no filesystem on the media):
        # re-arm so reinserting a disk launches again.
        if not has_media(device):
            if node in armed:
                armed.discard(node)
                log.info("disk removed from %s; ready for next disk", node)
            continue

        # Media is present.
        if node in armed:
            continue  # already launched for the disk currently in the drive

        # Debounce duplicate events for the same insertion.
        now = time.monotonic()
        if now - last_seen.get(node, 0.0) < args.debounce:
            continue
        last_seen[node] = now
        armed.add(node)
        handle_disk(node, args.keep_mounted, args.dry_run)
    return 0


def list_candidates() -> int:
    context = pyudev.Context()
    found = False
    for device in context.list_devices(subsystem="block", DEVTYPE="disk"):
        node = device.device_node
        floppy = device.get("ID_DRIVE_FLOPPY") == "1"
        bus = device.get("ID_BUS")
        removable = _read_sysfs_int(device, "removable")
        fs = device.get("ID_FS_TYPE") or "-"
        print(f"{node:12} bus={bus or '-':5} floppy={int(bool(floppy))} "
              f"removable={removable} fstype={fs} "
              f"model={device.get('ID_MODEL') or '-'}")
        found = True
    if not found:
        print("no block 'disk' devices found")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", metavar="NODE",
                   help="match only this device node (e.g. /dev/sdb) "
                        "instead of auto-detecting the floppy drive")
    p.add_argument("--keep-mounted", action="store_true",
                   help="don't unmount the disk after launching")
    p.add_argument("--dry-run", action="store_true",
                   help="detect and parse, but don't actually launch Steam")
    p.add_argument("--no-notify", action="store_true",
                   help="disable desktop notifications")
    p.add_argument("--debounce", type=float, default=3.0, metavar="SECS",
                   help="ignore repeat events within this window (default 3s)")
    p.add_argument("--list", action="store_true",
                   help="list candidate block devices and exit")
    p.add_argument("--test", metavar="PATH",
                   help="parse the marker file in PATH (a mounted dir) and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    global _notify_enabled
    _notify_enabled = not args.no_notify

    if not shutil.which("udisksctl"):
        log.error("udisksctl not found; install udisks2")
        return 1

    if args.list:
        return list_candidates()
    if args.test:
        appid = read_appid(args.test)
        if appid:
            launch_steam(appid, dry_run=True)
            return 0
        return 1

    try:
        return run(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
