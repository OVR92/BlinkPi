"""Nightly cleanup of the SM2 backing image.

Strategy
--------
Earlier designs reformatted the backing image. That doesn't work — when
the SM2 sees an unrecognized filesystem on the USB drive, it prompts in
the Blink mobile app for human confirmation to format. Reformatting
nightly would mean a confirmation prompt every morning.

Instead we:
  0. Pause the periodic sync timer so nothing races us
  1. Take the gadget offline (SM2 sees device disconnect)
  2. Mount the same exFAT filesystem read-write (at its own mount point,
     never the sync service's)
  3. Either delete all YY-MM/ subtrees, OR keep the most recent N days
     and delete only older clips, depending on wipe.retention_days.
     In all modes we preserve:
        /blink/                  — the SM2's expected root
        /blink/.tmp/             — staging area
        /blink_backup/           — paid Clip Backup feature
  4. Unmount cleanly
  5. Bring the gadget back up - unconditionally, in a finally block. If the
     gadget stays down the SM2 reports "no USB drive" until reboot.
  6. Resume the sync timer

The SM2 sees the same drive (same UUID, same skeleton) come back online
and resumes recording without any human interaction.

Retention modes
---------------
- retention_days = 0  → delete everything (smallest disk footprint)
- retention_days = N  → keep last N days on the Pi too (extra safety net
                        if destinations are temporarily unreachable)

WARNING
-------
This script invokes systemctl and mount as root. Run via the bub-wipe
CLI (which the systemd unit calls), not directly from a normal shell.
"""

from __future__ import annotations

import hashlib
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
SYNC_TIMER = "blink-sync.timer"


def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args]).returncode


def _unit_exists(unit: str) -> bool:
    """True if systemd knows about the unit (loaded or on disk)."""
    r = subprocess.run(
        ["systemctl", "list-unit-files", "--no-legend", unit],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and unit in r.stdout


def _is_mounted(mount_point: Path) -> bool:
    return subprocess.run(
        ["mountpoint", "-q", str(mount_point)],
    ).returncode == 0


def _ensure_unmounted(mount_point: Path) -> None:
    if _is_mounted(mount_point):
        r = subprocess.run(["umount", str(mount_point)], capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("umount %s failed: %s", mount_point, r.stderr.strip())


def wipe_mount_point(c: cfg.Config) -> Path:
    """Where the wipe mounts the image read-write.

    Deliberately NOT the sync service's mount_point. Earlier versions shared
    it, and a sync run that fired mid-wipe would find the wipe's RW mount
    already present, walk it, and then unmount it from under the wipe.
    """
    return c.mount_point.with_name(c.mount_point.name + "_wipe")


def _restart_gadget() -> None:
    log.info("starting %s", GADGET_SERVICE)
    if _systemctl("start", GADGET_SERVICE) != 0:
        log.error(
            "%s failed to start - the SM2 will report no USB drive until it is started",
            GADGET_SERVICE,
        )


def run(c: cfg.Config) -> int:
    if os.geteuid() != 0:
        log.error("must be run as root (use sudo or the systemd unit)")
        return 2

    if not c.backing_image_path.exists():
        log.error("backing image missing at %s", c.backing_image_path)
        return 1

    # Pause the periodic sync for the duration of the wipe. A sync that
    # starts while the gadget is down and the image is mounted RW would
    # (a) see a half-deleted filesystem and (b) previously unmounted our
    # RW mount from under us, crashing the wipe before the gadget came
    # back up. The timer is restarted in the finally block below.
    timer_paused = False
    if _unit_exists(SYNC_TIMER):
        log.info("pausing %s", SYNC_TIMER)
        timer_paused = _systemctl("stop", SYNC_TIMER) == 0
        if not timer_paused:
            log.warning("could not stop %s; continuing", SYNC_TIMER)

    try:
        return _wipe(c)
    finally:
        if timer_paused:
            log.info("resuming %s", SYNC_TIMER)
            _systemctl("start", SYNC_TIMER)


def _wipe(c: cfg.Config) -> int:
    # Best-effort final sync; losing one cycle is better than skipping the
    # wipe entirely if sync transiently fails. `systemctl start` blocks
    # until the oneshot completes, and serialises with an in-flight run.
    log.info("running pre-wipe sync")
    if _systemctl("start", SYNC_SERVICE) != 0:
        log.warning("pre-wipe sync failed; continuing anyway")

    log.info("stopping %s", GADGET_SERVICE)
    if _systemctl("stop", GADGET_SERVICE) != 0:
        # Never touch the image RW while the SM2 may still be writing to
        # it through the gadget - that corrupts the exFAT filesystem.
        log.error("could not stop %s; aborting wipe without touching the image", GADGET_SERVICE)
        _restart_gadget()
        return 1

    # Everything from here on runs with the gadget down. Whatever happens -
    # mount failure, an exception mid-delete, an early return - the gadget
    # MUST come back up, so the restart lives in a finally block.
    try:
        # Give the SM2 a moment to register the disconnect.
        subprocess.run(["sleep", "5"])
        return _wipe_with_gadget_down(c)
    finally:
        _restart_gadget()
        if c.web.enabled:
            # The web UI holds its own RO loop mount of the image; its exFAT
            # metadata is now stale. Drop it so the UI remounts lazily.
            _ensure_unmounted(c.web.mount_point)


def _wipe_with_gadget_down(c: cfg.Config) -> int:
    mnt = wipe_mount_point(c)
    _ensure_unmounted(mnt)
    log.info("mounting backing image RW at %s", mnt)
    mnt.mkdir(parents=True, exist_ok=True)
    mount_cmd = [
        "mount",
        "-o", f"loop,offset={sm2.partition_offset(c.backing_image_path)}",
        str(c.backing_image_path), str(mnt),
    ]
    r = subprocess.run(mount_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("mount failed: %s; aborting", r.stderr.strip())
        return 1

    survivors: set[str] = set()
    try:
        clips_dir = mnt / sm2.CLIPS_ROOT
        if not clips_dir.exists():
            log.warning("%s missing - SM2 may not have formatted yet", clips_dir)
            return 0

        if c.wipe.retention_days == 0:
            deleted = _delete_all_months(clips_dir)
            log.info("removed %d month directories (retention=0)", deleted)
        else:
            files_deleted, dirs_cleaned = _delete_older_than(
                clips_dir, c.wipe.retention_days,
            )
            log.info(
                "kept last %d days; deleted %d files, cleaned %d empty dirs",
                c.wipe.retention_days, files_deleted, dirs_cleaned,
            )

        # Verify the skeleton survived.
        for required in (
            clips_dir,
            clips_dir / ".tmp",
            mnt / "blink_backup",
        ):
            if not required.exists():
                log.warning("expected directory missing after wipe: %s", required)

        survivors = _surviving_clips(mnt)
        removed = _prune_thumbnails(c, survivors)
        if removed:
            log.info("pruned %d stale thumbnails", removed)

        subprocess.run(["sync"])
    finally:
        log.info("unmounting %s", mnt)
        _ensure_unmounted(mnt)

    _prune_state(c, survivors)
    log.info("wipe complete")
    return 0


def _surviving_clips(mnt: Path) -> set[str]:
    """Relative paths of every clip still on the image after the wipe."""
    alive: set[str] = set()
    clips_dir = mnt / sm2.CLIPS_ROOT
    if not clips_dir.exists():
        return alive
    for p in clips_dir.rglob("*.mp4"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(mnt))
        if sm2.is_skippable(rel):
            continue
        alive.add(rel)
    return alive


def _prune_state(c: cfg.Config, survivors: set[str]) -> None:
    """Drop sync-state entries for clips that no longer exist on the image.

    Earlier versions reset the whole state file, which with retention_days > 0
    made every surviving clip get re-validated and re-pushed the next morning.
    State keys are "rel_path|size|mtime_ns" (see sync.ClipKey).
    """
    import json

    if not c.state_file.exists():
        return
    try:
        data = json.loads(c.state_file.read_text() or "{}")
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read state file (%s); leaving it alone", e)
        return
    if not isinstance(data, dict):
        return

    before = sum(len(v) for v in data.values() if isinstance(v, list))
    pruned = {
        dest: [k for k in keys if k.rsplit("|", 2)[0] in survivors]
        for dest, keys in data.items()
        if isinstance(keys, list)
    }
    after = sum(len(v) for v in pruned.values())
    tmp = c.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(pruned, indent=2))
    tmp.replace(c.state_file)
    log.info("pruned sync state: %d -> %d entries", before, after)
    # Best-effort chown to the user the sync service runs as.
    try:
        import pwd
        uid = pwd.getpwnam(c.pi_user).pw_uid
        os.chown(c.state_file, uid, -1)
    except (ImportError, KeyError, PermissionError) as e:
        log.warning("could not chown state file: %s", e)


def _prune_thumbnails(c: cfg.Config, survivors: set[str]) -> int:
    """Remove thumbnails for clips that no longer exist on the backing image."""
    if not c.thumbnail_dir.exists():
        return 0
    alive = {hashlib.sha256(rel.encode()).hexdigest() for rel in survivors}
    removed = 0
    for thumb in c.thumbnail_dir.glob("*.jpg"):
        if thumb.stem not in alive:
            thumb.unlink(missing_ok=True)
            removed += 1
    return removed


def _rmtree(path: Path) -> None:
    """Recursive delete that doesn't crash on the SMB-style edge cases."""
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            _rmtree(child)
        path.rmdir()
    else:
        path.unlink()


def _delete_all_months(clips_dir: Path) -> int:
    """Original behavior: delete every YY-MM/ directory."""
    count = 0
    for entry in clips_dir.iterdir():
        if entry.is_dir() and sm2.MONTH_DIR_RE.match(entry.name):
            log.info("removing %s", entry)
            _rmtree(entry)
            count += 1
    return count


def _delete_older_than(clips_dir: Path, retention_days: int) -> tuple[int, int]:
    """Walk YY-MM/YY-MM-DD/ and delete clips older than retention_days.

    Returns (files_deleted, empty_dirs_cleaned). After deleting old files
    we also remove now-empty YY-MM-DD/ and YY-MM/ directories so the
    listing stays tidy. The SM2's own .tmp/ and the blink_backup/
    skeleton are never touched.

    Cutoff is calculated against the SM2's UTC clock (clip paths are UTC).
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    files_deleted = dirs_cleaned = 0

    for month_dir in sorted(clips_dir.iterdir()):
        if not (month_dir.is_dir() and sm2.MONTH_DIR_RE.match(month_dir.name)):
            continue

        for day_dir in sorted(month_dir.iterdir()):
            day_m = sm2.DATE_DIR_RE.match(day_dir.name)
            if not (day_dir.is_dir() and day_m):
                continue
            yy, mm, dd = day_m.groups()
            try:
                day_date = datetime(
                    2000 + int(yy), int(mm), int(dd),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                continue

            # If the entire day is older than cutoff, drop the whole dir.
            # End-of-day = next-day midnight; only safe to bulk-drop if
            # the whole day is past the cutoff.
            day_end = day_date + timedelta(days=1)
            if day_end <= cutoff:
                file_count = sum(1 for _ in day_dir.iterdir())
                log.info("removing whole day %s (%d files)", day_dir, file_count)
                _rmtree(day_dir)
                files_deleted += file_count
                dirs_cleaned += 1

        # Clean up empty month dirs.
        try:
            next(month_dir.iterdir())
        except StopIteration:
            log.info("removing empty month dir %s", month_dir)
            month_dir.rmdir()
            dirs_cleaned += 1

    return files_deleted, dirs_cleaned


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
