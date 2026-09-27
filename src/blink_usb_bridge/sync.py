"""The sync engine.

Walks the SM2's mounted filesystem, validates each candidate clip, and
pushes new ones to all enabled destinations. Per-destination state lets
us retry failed pushes on the next run without re-pushing succeeded ones.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from . import ai_vision, health
from . import config as cfg
from . import sm2
from .destinations import Destination
from .notify import Notifier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClipKey:
    """Stable identifier for a clip on the SM2 side, used for state dedup."""
    rel_path: str
    size: int
    mtime_ns: int

    def to_str(self) -> str:
        return f"{self.rel_path}|{self.size}|{self.mtime_ns}"


def _load_state(path: Path, dest_names: list[str]) -> dict[str, set[str]]:
    if not path.exists():
        return {n: set() for n in dest_names}
    try:
        with path.open() as f:
            data = json.load(f)
        return {n: set(data.get(n, [])) for n in dest_names}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read state file %s (%s); starting fresh", path, e)
        return {n: set() for n in dest_names}


def _save_state(path: Path, state: dict[str, set[str]]) -> None:
    serializable = {k: sorted(v) for k, v in state.items()}
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(serializable, f, indent=2)
    tmp.replace(path)


def _drop_caches() -> None:
    """Force the kernel to drop page cache so we re-read SM2 metadata fresh.

    Necessary because the SM2 writes the same exFAT we read, with no
    multi-host coordination — without dropping caches we may see stale
    directory entries.
    """
    try:
        subprocess.run(["sync"], check=True)
        subprocess.run(
            ["sudo", "-n", "tee", "/proc/sys/vm/drop_caches"],
            input=b"3", check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        log.warning("drop_caches failed (%s); continuing", e)


def _is_mounted(mount_point: Path) -> bool:
    return subprocess.run(
        ["mountpoint", "-q", str(mount_point)],
    ).returncode == 0


def _mount_ro(image: Path, mount_point: Path) -> bool:
    mount_point.mkdir(parents=True, exist_ok=True)
    if _is_mounted(mount_point):
        # A mount is already present: a previous run failed to unmount.
        # Its exFAT metadata may be stale, so try to replace it with a
        # fresh mount. If that fails (EBUSY), use what we have.
        log.warning("%s already mounted (stale?); remounting", mount_point)
        _unmount(mount_point)
        if _is_mounted(mount_point):
            return True
    cmd = [
        "sudo", "-n", "mount",
        "-o", f"ro,loop,offset={sm2.partition_offset(image)},noatime,nodev,nosuid,noexec",
        str(image), str(mount_point),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("mount failed: %s", result.stderr.strip())
        return False
    return True


def _unmount(mount_point: Path) -> None:
    if not _is_mounted(mount_point):
        return
    result = subprocess.run(
        ["sudo", "-n", "umount", str(mount_point)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("umount %s failed: %s", mount_point, result.stderr.strip())


def _stat(p: Path) -> Optional[tuple[int, int]]:
    try:
        st = p.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def _is_ready(p: Path, val: cfg.Validation) -> bool:
    """Return True if the clip looks complete enough to sync.

    Cheap floor (size) + brief settle window (size+mtime stable). The SM2's
    atomic .tmp/ rename should make this redundant, but exFAT has no
    multi-host coordination so a tiny window catches the rare race.
    """
    first = _stat(p)
    if first is None:
        return False
    if first[0] < val.min_bytes:
        log.debug("below min_bytes (%d): %s", first[0], p.name)
        return False
    time.sleep(val.settle_seconds)
    second = _stat(p)
    return first == second


def _ffprobe_duration(p: Path, timeout: int) -> Optional[float]:
    """Return clip duration in seconds, or None if the file is unreadable/invalid."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(p),
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("ffprobe timed out on %s", p.name)
        return None
    if result.returncode != 0:
        return None
    try:
        d = float(result.stdout.strip())
        return d if d > 0.0 else None
    except ValueError:
        return None


def _thumbnail_path(thumbnail_dir: Path, rel_path: str) -> Path:
    key = hashlib.sha256(rel_path.encode()).hexdigest()
    return thumbnail_dir / f"{key}.jpg"


def _make_thumbnail(src: Path, dest: Path, duration: float) -> bool:
    """Extract a single JPEG frame and write it to dest. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    seek = min(1.0, duration * 0.3)
    # Try seeking into the clip; fall back to first frame for very short clips.
    for extra in (["-ss", str(seek)], []):
        try:
            r = subprocess.run(
                ["ffmpeg", *extra, "-i", str(src),
                 "-vframes", "1", "-vf", "scale=320:-1",
                 "-q:v", "5", "-f", "image2", str(dest), "-y"],
                capture_output=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            return False
        if r.returncode == 0 and dest.exists():
            return True
    return False


def _walk_clips(mount_point: Path) -> Iterable[Path]:
    """Yield mp4 files under blink/, skipping .tmp and blink_backup.

    Tolerates directories disappearing mid-walk (e.g. the image was
    modified underneath us) by ending the walk instead of crashing.
    """
    clips_dir = mount_point / sm2.CLIPS_ROOT
    if not clips_dir.exists():
        return
    try:
        for p in clips_dir.rglob("*.mp4"):
            if not p.is_file():
                continue
            rel = p.relative_to(mount_point)
            if sm2.is_skippable(str(rel)):
                continue
            yield p
    except OSError as e:
        log.warning("clip walk aborted: %s", e)


def _format_filename(clip: sm2.Clip, tz: ZoneInfo) -> str:
    """Turn a ParsedClip into the canonical destination filename.

    Format: YYYY-MM-DD_HH-MM-SS_<camera>.mp4
    Timestamp is the configured local timezone.
    """
    local_ts = clip.timestamp_utc.astimezone(tz)
    return f"{local_ts.strftime('%Y-%m-%d_%H-%M-%S')}_{clip.camera}.mp4"


def _analyze(c: cfg.Config, clip_path: Path, rel: str, parsed: sm2.Clip, duration: float, tz: ZoneInfo) -> Optional[dict]:
    """AI description for one clip (before pushing, so destinations can include it)."""
    if not c.ai.enabled:
        return None
    try:
        analysis, msg = ai_vision.analyze_clip(c, clip_path, rel, parsed.camera, duration,
                                               parsed.timestamp_utc.astimezone(tz))
        if analysis is None:
            log.info("AI skipped for %s: %s", rel, msg)
        return analysis
    except Exception:  # noqa: BLE001 - never let analysis break the sync
        log.exception("AI analysis failed for %s", rel)
        return None


def _notify_new_clip(
    c: cfg.Config, notifier: Notifier, analysis: Optional[dict], rel: str, parsed: sm2.Clip,
    duration: float, thumb: Path, size: int, tz: ZoneInfo,
) -> None:
    """Webhook and/or summary email for one freshly synced clip."""
    local_ts = parsed.timestamp_utc.astimezone(tz)
    em, wh = c.notify.email, c.notify.webhook
    # In "clip" mode the EmailDestination already mailed the clip itself.
    want_email = em.enabled and em.on_new_clip == "summary"
    want_hook = wh.enabled and wh.on_new_clip
    if not (want_email or want_hook):
        return

    filename = _format_filename(parsed, tz)
    desc = (analysis or {}).get("description", "") if (analysis and c.ai.in_notifications) else ""
    is_alert = bool((analysis or {}).get("alert"))
    subject = f"Blink: motion at {parsed.camera} {local_ts.strftime('%H:%M')}" + (" (alert)" if is_alert else "")
    body = (desc + "\n\n" if desc else "") + (
        f"Camera: {parsed.camera}\nTime: {local_ts:%Y-%m-%d %H:%M:%S}\n"
        f"Clip: {filename}\nDuration: {duration:.0f} s, {size / 1e6:.1f} MB"
    )
    smb_rel = f"{parsed.camera}/{filename}" if c.smb.layout == "per_camera" else filename
    data = {
        "camera": parsed.camera, "filename": filename, "rel_path": rel,
        "timestamp": local_ts.isoformat(timespec="seconds"), "duration": duration,
        "size_bytes": size, "description": desc or None, "alert": is_alert,
        "tags": (analysis or {}).get("tags", []),
        "smb_path": str(c.smb.mount_point / smb_rel) if c.smb.enabled else None,
        "smb_relative_path": smb_rel if c.smb.enabled else None,
        "web_url": f"http://{socket.gethostname()}:{c.web.listen_port}/clip/{rel}" if c.web.enabled else None,
    }
    attachments = [thumb] if thumb.exists() else []
    try:
        notifier.send(subject, body, event="new_clip", data=data, attachments=attachments,
                      email=want_email, webhook=want_hook)
    except Exception:  # noqa: BLE001
        log.exception("notification failed for %s", rel)


def _fail(c: cfg.Config, reason: str) -> int:
    """Record a failed pass for the watchdog and return exit code 1."""
    try:
        health.record_sync_result(c, False, reason)
        health.check(c)
    except Exception:  # noqa: BLE001
        log.exception("watchdog check failed")
    return 1


def run(c: cfg.Config) -> int:
    """Perform one sync pass. Returns process exit code."""
    if not c.backing_image_path.exists():
        log.error("backing image not found: %s", c.backing_image_path)
        return _fail(c, f"backing image not found: {c.backing_image_path}")

    destinations = Destination.from_config(c)
    if not destinations:
        log.error("no destinations enabled in config")
        return _fail(c, "no destinations enabled in config")

    # Pre-flight: drop caches so we get a fresh view of the SM2's writes.
    _drop_caches()

    if not _mount_ro(c.backing_image_path, c.mount_point):
        return _fail(c, "mount failed")

    tz = ZoneInfo(c.timezone)
    dest_names = [d.name for d in destinations]
    state = _load_state(c.state_file, dest_names)
    notifier = Notifier(c)

    counts = {f"new_{n}": 0 for n in dest_names}
    counts.update(skipped=0, unparseable=0)
    newest_clip_ts = 0.0  # newest clip time seen this pass (for the watchdog)

    try:
        for clip_path in _walk_clips(c.mount_point):
            rel = str(clip_path.relative_to(c.mount_point))
            stat = _stat(clip_path)
            if stat is None:
                continue
            size, mtime_ns = stat
            key = ClipKey(rel, size, mtime_ns).to_str()

            seen = sm2.parse(rel)
            if seen is not None:
                newest_clip_ts = max(newest_clip_ts, seen.timestamp_utc.timestamp())

            # If every destination already has this file, skip without
            # paying for the validation steps.
            done_for = {d.name: key in state[d.name] for d in destinations}
            if all(done_for.values()):
                counts["skipped"] += 1
                continue

            if not _is_ready(clip_path, c.validation):
                continue
            duration = _ffprobe_duration(clip_path, c.validation.ffprobe_timeout_seconds)
            if duration is None:
                log.info("ffprobe rejected: %s", rel)
                continue

            thumb = _thumbnail_path(c.thumbnail_dir, rel)
            if not thumb.exists():
                _make_thumbnail(clip_path, thumb, duration)

            parsed = sm2.parse(rel)
            if parsed is None:
                # Path doesn't match SM2's expected layout. Log and skip
                # rather than guessing — easier to debug surprises.
                counts["unparseable"] += 1
                log.warning("unparseable SM2 path: %s", rel)
                continue

            target_filename = _format_filename(parsed, tz)

            first_time = not any(done_for.values())
            analysis = _analyze(c, clip_path, rel, parsed, duration, tz) if first_time else None

            pushed_any = False
            for dest in destinations:
                if done_for[dest.name]:
                    continue
                if not dest.available():
                    continue
                if dest.push(clip_path, target_filename, parsed.camera):
                    state[dest.name].add(key)
                    counts[f"new_{dest.name}"] += 1
                    pushed_any = True

            # First time we've handled this clip: notify. A clip that was only
            # re-tried for a second destination is not new.
            if first_time and (pushed_any or not any(d.available() for d in destinations)):
                _notify_new_clip(c, notifier, analysis, rel, parsed, duration, thumb, size, tz)

        _save_state(c.state_file, state)
        # Format counts compactly for the summary line.
        summary = " ".join(f"{k}={v}" for k, v in counts.items())
        totals = " ".join(f"total_{n}={len(state[n])}" for n in dest_names)
        log.info("done. %s %s", summary, totals)
    finally:
        _unmount(c.mount_point)

    try:
        health.record_sync_result(c, True)
        if newest_clip_ts:
            health.record_clip(c, newest_clip_ts)
        health.check(c, notifier)
    except Exception:  # noqa: BLE001
        log.exception("watchdog check failed")

    return 0


def main() -> int:
    """CLI entry point. Reads config.yaml, runs one sync, exits."""
    logging.basicConfig(
        level=os.environ.get("BUB_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        c = cfg.load()
    except (FileNotFoundError, ValueError) as e:
        log.error("config error: %s", e)
        return 2

    # Add a file handler now that we know where the log goes.
    fh = logging.FileHandler(c.log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)

    return run(c)


if __name__ == "__main__":
    sys.exit(main())
