"""Nightly cleanup of the SM2 backing image.

Strategy
--------
Earlier designs reformatted the backing image. That doesn't work — when
the SM2 sees an unrecognized filesystem on the USB drive, it prompts in
the Blink mobile app for human confirmation to format. Reformatting
nightly would mean a confirmation prompt every morning.

Instead we:
  1. Take the gadget offline (SM2 sees device disconnect)
  2. Mount the same exFAT filesystem read-write
  3. Delete YY-MM/ subtrees inside /blink/ (the actual clip data)
     while preserving:
        /blink/                  — the SM2's expected root
        /blink/.tmp/             — staging area
        /blink_backup/           — paid Clip Backup feature
  4. Unmount cleanly
  5. Bring the gadget back up

The SM2 sees the same drive (same UUID, same skeleton) come back online
and resumes recording without any human interaction.

WARNING
-------
This script invokes systemctl and mount as root. Run via the bub-wipe
CLI (which the systemd unit calls), not directly from a normal shell.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from . import config as cfg
from . import sm2

log = logging.getLogger(__name__)

GADGET_SERVICE = "blink-gadget.service"
SYNC_SERVICE = "blink-sync.service"


def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args]).returncode


def _ensure_unmounted(mount_point: Path) -> None:
    if subprocess.run(
        ["mountpoint", "-q", str(mount_point)],
    ).returncode == 0:
        subprocess.run(["umount", str(mount_point)], capture_output=True)


def run(c: cfg.Config) -> int:
    if os.geteuid() != 0:
        log.error("must be run as root (use sudo or the systemd unit)")
        return 2

    if not c.backing_image_path.exists():
        log.error("backing image missing at %s", c.backing_image_path)
        return 1

    # Best-effort final sync; losing one cycle is better than skipping the
    # wipe entirely if sync transiently fails.
    log.info("running pre-wipe sync")
    if _systemctl("start", SYNC_SERVICE) != 0:
        log.warning("pre-wipe sync failed; continuing anyway")

    log.info("stopping %s", GADGET_SERVICE)
    _systemctl("stop", GADGET_SERVICE)
    # Give the SM2 a moment to register the disconnect.
    subprocess.run(["sleep", "5"])

    log.info("mounting backing image RW at %s", c.mount_point)
    c.mount_point.mkdir(parents=True, exist_ok=True)
    mount_cmd = [
        "mount",
        "-o", f"loop,offset={sm2.PARTITION_OFFSET}",
        str(c.backing_image_path), str(c.mount_point),
    ]
    if subprocess.run(mount_cmd).returncode != 0:
        log.error("mount failed; restarting gadget and aborting")
        _systemctl("start", GADGET_SERVICE)
        return 1

    try:
        clips_dir = c.mount_point / sm2.CLIPS_ROOT
        if not clips_dir.exists():
            log.warning("%s missing — SM2 may not have formatted yet", clips_dir)
            return 0

        # Delete only YY-MM month directories. Skeleton (clips_dir,
        # .tmp/, blink_backup/) stays untouched.
        deleted = 0
        for entry in clips_dir.iterdir():
            if entry.is_dir() and sm2.MONTH_DIR_RE.match(entry.name):
                log.info("removing %s", entry)
                _rmtree(entry)
                deleted += 1
        log.info("removed %d month directories", deleted)

        # Verify the skeleton survived.
        for required in (
            clips_dir,
            clips_dir / ".tmp",
            c.mount_point / "blink_backup",
        ):
            if not required.exists():
                log.warning("expected directory missing after wipe: %s", required)

        subprocess.run(["sync"])
    finally:
        log.info("unmounting %s", c.mount_point)
        _ensure_unmounted(c.mount_point)

    log.info("starting %s", GADGET_SERVICE)
    _systemctl("start", GADGET_SERVICE)

    # State referenced post-wipe clips that no longer exist; reset so we
    # don't keep them around as stale entries.
    log.info("resetting sync state")
    if c.state_file.exists():
        c.state_file.write_text('{}')
        # Best-effort chown to the user the sync service runs as.
        try:
            import pwd
            uid = pwd.getpwnam(c.pi_user).pw_uid
            os.chown(c.state_file, uid, -1)
        except (KeyError, PermissionError) as e:
            log.warning("could not chown state file: %s", e)

    log.info("wipe complete")
    return 0


def _rmtree(path: Path) -> None:
    """Recursive delete that doesn't crash on the SMB-style edge cases."""
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            _rmtree(child)
        path.rmdir()
    else:
        path.unlink()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BUB_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        c = cfg.load()
    except (FileNotFoundError, ValueError) as e:
        log.error("config error: %s", e)
        return 2
    return run(c)


if __name__ == "__main__":
    sys.exit(main())
