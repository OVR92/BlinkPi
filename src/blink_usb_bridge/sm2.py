"""Everything we know about the Sync Module 2's on-disk format.

Discovered empirically by attaching a Pi-as-USB-mass-storage to an SM2,
letting it reformat the drive, then poking at the result. None of this
is documented by Blink — if SM2 firmware ever changes, the constants
here may need to update.

LAYOUT
------
SM2 writes an MBR partition table, then a single exFAT partition starting
at sector 32 (offset 16384 bytes). To mount:
    mount -o loop,offset=16384 backing.img /mnt/...

Inside the partition:

    /blink/                                  # main clip storage
        .tmp/                                # in-progress writes  (SKIP)
        YY-MM/                               # year-month, e.g. "26-04"
            YY-MM-DD/                        # year-month-day
                HH-MM-SS_<camera>_<seq>.mp4  # the clip
    /blink_backup/                           # paid Clip Backup feature (SKIP)

ATOMIC WRITES
-------------
SM2 records to .tmp/<id>.mp4, finalizes the MP4 container, then renames
into the final date folder. So any file in YY-MM/YY-MM-DD/ is guaranteed
complete. (See validation.py for the tiny settle window we still apply
out of an abundance of caution for multi-host exFAT.)

TIMESTAMPS
----------
Times in the path and filename are UTC, not local. We convert to the
configured local timezone at sync time.

CAMERA NAMES
------------
The camera name is embedded in the filename and is whatever you named
the camera in the Blink app, with spaces removed (Blink itself enforces
this). It can contain underscores (e.g. "front_door"), so the parser
anchors on the leading time and the trailing _NNN.mp4 sequence.

SEQUENCE NUMBERS
----------------
Increment within the same UTC second when multiple events fire close
together. Almost always _001 in practice.

UNRECOGNIZED FILESYSTEM
-----------------------
If the SM2 sees a USB drive it doesn't recognize as SM2-formatted, it
prompts in the Blink mobile app to format it. This means: do NOT mkfs
the backing image as part of any maintenance flow — only delete the
contents and leave the SM2's filesystem skeleton intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Bytes from start of the disk image to the start of the exFAT partition.
PARTITION_OFFSET = 16384

# Top-level directory the SM2 stores clips in.
CLIPS_ROOT = "blink"

# Subdirectories inside CLIPS_ROOT we must skip.
SKIP_DIRS = frozenset({".tmp"})

# Top-level directories alongside CLIPS_ROOT we must skip.
SKIP_TOPLEVEL = frozenset({"blink_backup"})


# Filename pattern: HH-MM-SS_<camera>_<seq>.mp4
# Camera may contain underscores; we anchor on the leading time and the
# trailing _<digits>.mp4 to extract the camera in between.
FILENAME_RE = re.compile(
    r"^(\d{2})-(\d{2})-(\d{2})_(.+)_(\d+)\.mp4$",
    re.IGNORECASE,
)

# Date directory: YY-MM-DD inside YY-MM
DATE_DIR_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{2})$")

# Year-month directory: YY-MM
MONTH_DIR_RE = re.compile(r"^(\d{2})-(\d{2})$")

# Sanitise camera/filename for use in destination paths.
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize(name: str) -> str:
    """Replace unsafe characters in a name with underscores."""
    return _SAFE_RE.sub("_", name)


@dataclass(frozen=True)
class Clip:
    """A parsed SM2 clip."""

    rel_path: str          # e.g. "blink/26-04/26-04-28/04-38-40_garage_001.mp4"
    timestamp_utc: datetime
    camera: str            # sanitized, safe for filesystem paths
    sequence: int

    @property
    def filename(self) -> str:
        return Path(self.rel_path).name


def parse(rel_path: str) -> Optional[Clip]:
    """Parse an SM2 path of the form blink/YY-MM/YY-MM-DD/HH-MM-SS_cam_NNN.mp4.

    Returns None if the path doesn't match SM2's expected layout. Callers
    can fall back to mtime-based defaults in that case.
    """
    parts = Path(rel_path).parts
    if len(parts) != 4 or parts[0] != CLIPS_ROOT:
        return None

    if not MONTH_DIR_RE.match(parts[1]):
        return None
    date_m = DATE_DIR_RE.match(parts[2])
    if not date_m:
        return None
    name_m = FILENAME_RE.match(parts[3])
    if not name_m:
        return None

    yy, mm, dd = date_m.groups()
    hh, mi, ss, camera, seq = name_m.groups()

    try:
        ts = datetime(
            year=2000 + int(yy),
            month=int(mm),
            day=int(dd),
            hour=int(hh),
            minute=int(mi),
            second=int(ss),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None

    return Clip(
        rel_path=rel_path,
        timestamp_utc=ts,
        camera=sanitize(camera),
        sequence=int(seq),
    )


def is_skippable(rel_path: str) -> bool:
    """True if this path is inside a SKIP_DIRS or SKIP_TOPLEVEL directory."""
    parts = Path(rel_path).parts
    if not parts:
        return True
    if parts[0] in SKIP_TOPLEVEL:
        return True
    return any(p in SKIP_DIRS for p in parts)
