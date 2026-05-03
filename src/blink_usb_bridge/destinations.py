"""Destination plugins.

Each destination knows how to push a single local file to its remote
storage. The sync loop calls .push(local_path, target_filename, camera)
and tracks (rel_path, size, mtime) per destination for dedup.

Adding a new destination
------------------------
1. Subclass Destination.
2. Implement .name, .push(), and .available().
3. Wire it up in Destination.from_config().
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from . import config as cfg

log = logging.getLogger(__name__)


class Destination(ABC):
    """Common interface for clip destinations."""

    name: str  # short identifier used in logs and state

    @abstractmethod
    def push(self, local_path: Path, target_filename: str, camera: str) -> bool:
        """Send local_path to this destination as target_filename.

        target_filename is the canonical YYYY-MM-DD_HH-MM-SS_<camera>.mp4.
        Subclasses may add markers/subfolders. Returns True on success.
        """

    @abstractmethod
    def available(self) -> bool:
        """Cheap pre-flight check: is this destination ready right now?"""

    @staticmethod
    def from_config(c: cfg.Config) -> list["Destination"]:
        """Build the list of enabled destinations from a Config."""
        out: list[Destination] = []
        if c.smb.enabled:
            out.append(SmbDestination(c.smb))
        if c.rclone.enabled:
            out.append(RcloneDestination(c.rclone))
        return out


# ───────────────────────────── SMB ──────────────────────────────

class SmbDestination(Destination):
    name = "smb"

    def __init__(self, opts: cfg.SmbDestination):
        self.opts = opts

    def available(self) -> bool:
        if not self.opts.mount_point.exists():
            log.warning("smb: mount point %s missing", self.opts.mount_point)
            return False
        # Probe the mount; CIFS automount usually mounts on first access.
        try:
            list(self.opts.mount_point.iterdir())
            return True
        except OSError as e:
            log.error("smb: mount check failed: %s", e)
            return False

    def push(self, local_path: Path, target_filename: str, camera: str) -> bool:
        if self.opts.layout == "per_camera":
            target = self.opts.mount_point / camera / target_filename
        else:
            target = self.opts.mount_point / target_filename

        if target.exists():
            log.debug("smb: already present at %s", target)
            return True

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.partial")
        try:
            shutil.copy2(local_path, tmp)
            tmp.rename(target)
            log.info("smb: copied -> %s", target.relative_to(self.opts.mount_point))
            return True
        except OSError as e:
            log.error("smb: copy failed for %s: %s", target_filename, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False


# ───────────────────────────── rclone ──────────────────────────────

class RcloneDestination(Destination):
    name = "rclone"

    def __init__(self, opts: cfg.RcloneDestination):
        self.opts = opts

    def available(self) -> bool:
        # rclone failures show up at copy time; we don't probe the network here.
        return True

    def _target_filename(self, base_filename: str) -> str:
        """Add the prune marker to the filename so the pruner can identify
        files this Pi pushed (and refuses to touch anything else)."""
        # Convert "YYYY-MM-DD_HH-MM-SS_camera.mp4"
        # into    "YYYY-MM-DD_HH-MM-SS_camera<MARKER>.mp4"
        if not base_filename.endswith(".mp4"):
            return base_filename + self.opts.prune_marker
        stem = base_filename[:-4]
        return stem + self.opts.prune_marker + ".mp4"

    def push(self, local_path: Path, target_filename: str, camera: str) -> bool:
        marked = self._target_filename(target_filename)
        if self.opts.layout == "per_camera":
            remote_target = f"{self.opts.remote.rstrip('/')}/{camera}/{marked}"
        else:
            remote_target = f"{self.opts.remote.rstrip('/')}/{marked}"

        cmd = [
            "rclone", "copyto",
            "--config", str(self.opts.config_file),
            "--low-level-retries", "3",
            "--retries", "2",
            "--contimeout", "30s",
            "--timeout", "120s",
            str(local_path), remote_target,
        ]
        log.info("rclone: copy -> %s", marked)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("rclone: failed for %s: %s", marked, result.stderr.strip())
            return False
        return True
