# Architecture

This is the deeper "why" doc. If you just want to set up the project, see [INSTALL.md](INSTALL.md). If you want to understand how the pieces fit together — or you're modifying the project — read on.

## Goals and non-goals

**Goals:**

1. Get every motion clip the SM2 records onto local or cloud storage automatically
2. Don't lose clips on crash, network outage, power blip
3. Don't require a Blink subscription
4. Don't run forever — automatic cleanup so the Pi doesn't fill up
5. Easy to extend with new destinations
6. One config file, one install script

**Non-goals:**

- Real-time streaming (the SM2 doesn't support it anyway)
- Replacing the Blink app, the Blink integration, or Blink's cloud
- Working with Blink hardware that isn't a Sync Module 2

## Overview

```
                         ┌────────────────────────────┐
                         │    Sync Module 2           │
                         │                            │
                         │  - cameras connect via Wi-Fi │
                         │  - records motion clips     │
                         │  - writes to "USB drive"    │
                         └────────────┬───────────────┘
                                      │ USB
                         ┌────────────▼───────────────┐
                         │  Raspberry Pi              │
                         │                            │
                         │  ┌──────────────────────┐  │
                         │  │ g_mass_storage       │  │
                         │  │ (presents image as   │  │
                         │  │  USB drive to SM2)   │  │
                         │  └──────────┬───────────┘  │
                         │             │ same file    │
                         │  ┌──────────▼───────────┐  │
                         │  │ usb_backing.img      │  │
                         │  │ (4 GB sparse exFAT)  │  │
                         │  └──────────┬───────────┘  │
                         │             │ ro loop mount │
                         │  ┌──────────▼───────────┐  │
                         │  │ bub-sync (every 30s) │  │
                         │  │  - parse SM2 layout  │  │
                         │  │  - validate clips    │  │
                         │  │  - push to dests     │  │
                         │  └──────────┬───────────┘  │
                         │             │              │
                         │  ┌──────────▼───────────┐  │
                         │  │ bub-wipe (nightly)   │  │
                         │  │  - delete YY-MM dirs │  │
                         │  └──────────────────────┘  │
                         └────────────┬───────────────┘
                                      │ network
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
       ┌──────────┐            ┌──────────┐            ┌──────────┐
       │ SMB      │            │ rclone   │            │ ...      │
       │ share    │            │ remote   │            │          │
       └──────────┘            └──────────┘            └──────────┘
```

## Components

### `g_mass_storage` kernel module

Linux's USB-gadget framework includes `g_mass_storage`, which makes any USB-OTG-capable system present an arbitrary file as a USB mass-storage device. We use it to present `usb_backing.img` to the SM2.

The gadget service (`blink-gadget.service`) starts at boot. The wipe service stops it nightly, reformats the contents, and restarts it.

### Backing image

A pre-allocated sparse file. The SM2 sees it as a flash drive and writes its filesystem to it. We treat it as a regular file and read from it via loop-mount.

Default size is 4 GB. See [SM2_FILESYSTEM.md](SM2_FILESYSTEM.md) for sizing notes.

### `bub-sync` (the sync engine)

The main work loop, scheduled every 30 seconds via `blink-sync.timer`. Each run:

1. Drops kernel page caches (because exFAT has no multi-host coordination)
2. Loop-mounts the backing image read-only at `mount_point`
3. Walks `/blink/`, skipping `.tmp/` (in-progress) and `blink_backup/` (paid)
4. For each `.mp4`, computes a `(rel_path, size, mtime)` key and checks state for each destination
5. If new for at least one destination:
   - Validates: size > MIN_BYTES, stable across 500ms settle window, ffprobe parses
   - Parses the SM2's filename → timestamp + camera + sequence
   - Builds canonical filename `YYYY-MM-DD_HH-MM-SS_<camera>.mp4`
   - Pushes to each destination that doesn't already have it; updates state on success
6. Unmounts and exits

State is per-destination. A failure pushing to one destination doesn't block others, and a retry next cycle only re-tries the failed ones.

### `bub-wipe` (nightly cleanup)

Runs once per night via `blink-wipe.timer`. Each run:

1. Triggers a final sync (best effort)
2. Stops `blink-gadget.service` so we can mount the image read-write
3. Mounts the image RW
4. Deletes `YY-MM/` directories under `/blink/` (the actual clip data)
5. Preserves `/blink/`, `/blink/.tmp/`, `/blink_backup/` — the SM2's filesystem skeleton
6. Unmounts
7. Restarts `blink-gadget.service`
8. Resets `sync_state.json` (the keys reference now-deleted files)

Critical: we **don't reformat**. The SM2 prompts for human confirmation on unrecognized filesystems; reformatting nightly would mean a confirmation prompt every morning.

### `bub-prune` (rclone retention)

Runs nightly, 30 minutes after the wipe, via `bub-prune.timer`. Calls `rclone delete` with:

- `--include "*<prune_marker>*"` — only files this Pi pushed (the marker is in every filename)
- `--min-age <retention_days>d` — only files older than retention
- `--drive-use-trash=false` if `use_trash: false`

The prune marker double-protects against accidentally wiping a remote we shouldn't.

## Validation strategy

The Pi reads from a filesystem the SM2 is actively writing to. Multi-host exFAT has no coordination — the kernel can't know when SM2's writes have settled. We use layered defenses:

| Layer | What it catches |
|---|---|
| **Skip `.tmp/`** | In-progress writes (the SM2's own atomic-write boundary) |
| **Drop page caches** | Stale metadata reads |
| **`MIN_BYTES`** | 0-byte stubs and partial directory entries |
| **500ms settle** | Files still being written to mid-scan |
| **`ffprobe`** | Files with broken MP4 containers (rare but possible) |
| **State dedup** | Re-pushing already-pushed clips |

The main concession to performance is the 500ms settle: it adds 0.5s per *new* clip, not per cycle. Empty cycles complete in 1-2 seconds.

## Destinations

Destinations are pluggable via the `Destination` ABC in `src/blink_usb_bridge/destinations.py`:

```python
class Destination(ABC):
    name: str  # short identifier, used in logs and state

    def available(self) -> bool: ...
    def push(self, local_path: Path, target_filename: str, camera: str) -> bool: ...
```

Three methods (one is just a property), 50ish lines of code per destination. The two built-in destinations are `SmbDestination` and `RcloneDestination`.

To add a new destination:

1. Subclass `Destination` in a new file or in `destinations.py`
2. Add a config section in `config.example.yaml` and a corresponding dataclass in `config.py`
3. Register it in `Destination.from_config()`

The state file tracks which clips have been successfully pushed to each named destination. So if you add a new destination, the first sync after enabling it will push everything currently on the SM2 to the new destination — even clips already pushed elsewhere.

## Filename scheme

All destinations use the same base format:

```
YYYY-MM-DD_HH-MM-SS_<camera>.mp4
```

- Timestamp is in the configured local timezone (not UTC like the SM2's filenames)
- Camera name comes from the SM2 filename, sanitized to safe characters

The rclone destination additionally appends a marker:

```
YYYY-MM-DD_HH-MM-SS_<camera><MARKER>.mp4
```

So `2026-04-27_21-38-40_garage__blinkpi__.mp4` for the default marker. The marker exists so the prune job can safely identify files this Pi pushed and refuse to touch anything else.

The SMB destination doesn't use the marker — it's expected to be your archive, with no auto-pruning.

## State file

`sync_state.json` in `project_dir`:

```json
{
  "smb": [
    "blink/26-04/26-04-28/04-38-40_garage_001.mp4|761772|1777351140000000000",
    ...
  ],
  "rclone": [
    "blink/26-04/26-04-28/04-38-40_garage_001.mp4|761772|1777351140000000000",
    ...
  ]
}
```

Keys are `<rel_path>|<size>|<mtime_ns>`. The mtime makes the key change if the SM2 ever overwrites a file (which we've never seen, but defensively we'd re-push the new version).

The wipe resets the state file because the keys reference clips that no longer exist on the backing image.

## What runs as what user

| Component | User | Why |
|---|---|---|
| `blink-gadget.service` | root | needs `modprobe` |
| `blink-sync.service` | unprivileged | uses sudoers for the specific mount/umount/drop_caches it needs |
| `blink-wipe.service` | root | needs full mount RW + systemctl |
| `bub-prune.service` | unprivileged | just calls rclone |

The unprivileged user needs three NOPASSWD sudoers rules — the installer adds them with strict argument matching so it only allows the specific commands we need.
