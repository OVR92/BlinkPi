"""Prune old clips from rclone destinations.

Runs nightly after the wipe. Deletes files matching the prune marker
(only files this Pi pushed, never anything else) older than the
configured retention period.

Refuses to run if the prune marker is empty or the retention is 0 —
either of those would risk wiping a remote we shouldn't.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from . import config as cfg

log = logging.getLogger(__name__)


def run(c: cfg.Config) -> int:
    if not c.rclone.enabled:
        log.info("rclone destination not enabled; nothing to prune")
        return 0

    if not c.rclone.prune_marker:
        log.error("refusing to prune: rclone.prune_marker is empty")
        return 1
    if c.rclone.retention_days <= 0:
        log.info("rclone.retention_days is %d; pruning disabled", c.rclone.retention_days)
        return 0

    age = f"{c.rclone.retention_days}d"
    pattern = f"*{c.rclone.prune_marker}*"

    log.info(
        "pruning %s older than %s, matching %s, use_trash=%s",
        c.rclone.remote, age, pattern, c.rclone.use_trash,
    )

    cmd = [
        "rclone", "delete",
        "--config", str(c.rclone.config_file),
        "--include", pattern,
        "--min-age", age,
    ]
    if not c.rclone.use_trash:
        cmd.append("--drive-use-trash=false")
    cmd.append(c.rclone.remote)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("rclone delete failed: %s", result.stderr.strip())
        return 1
    if result.stdout.strip():
        log.info("rclone output: %s", result.stdout.strip())
    log.info("prune complete")
    return 0


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
