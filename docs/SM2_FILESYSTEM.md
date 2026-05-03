# SM2 USB filesystem reverse-engineering notes

Everything in this document was figured out empirically by attaching a Pi-as-USB-mass-storage to a Sync Module 2, letting it format and use the drive, then poking at the result. **None of this is documented by Blink.** If a future SM2 firmware update changes anything, this project's parser will need updating.

## Partition layout

The SM2 writes an MBR partition table at the start of the disk, then a single exFAT partition starting at **sector 32 (offset 16384 bytes)**. To mount the partition directly from a backing image:

```bash
mount -o loop,offset=16384 backing.img /mnt/somewhere
```

Without the offset, the kernel sees the partition table and refuses to mount the disk image as exFAT. This is the single biggest gotcha.

## Filesystem skeleton

After the SM2 formats a drive, the partition contains:

```
/blink/                                  # main clip storage
    .tmp/                                # in-progress writes
    YY-MM/                               # year-month, e.g. "26-04"
        YY-MM-DD/                        # year-month-day
            HH-MM-SS_<camera>_<seq>.mp4  # the clip
/blink_backup/                           # paid Blink Clip Backup feature
```

The empty `.tmp/` and `blink_backup/` directories are part of the skeleton even immediately after format, before any clips have been recorded.

**The `.tmp/` directory is the SM2's atomic-write staging area.** When a motion event triggers, the SM2 records the clip into `/blink/.tmp/<some-id>.mp4`, finalizes the MP4 container, and only then renames the file into the date folder. Files in date folders are therefore guaranteed to be complete.

`blink_backup/` is associated with Blink's paid Clip Backup subscription, which we don't use. We skip it entirely.

## Filename format

Files in date folders are named like:

```
HH-MM-SS_<camera>_<seq>.mp4
```

For example: `04-38-40_garage_001.mp4`

- **HH-MM-SS** — UTC time (not local!) when the clip started recording
- **\<camera\>** — the camera's name as configured in the Blink app, with spaces stripped (Blink itself enforces this). Can contain underscores (e.g. `front_door`).
- **\<seq\>** — sequence number that increments within the same UTC second when multiple events fire close together. Almost always `001` in practice.

Because camera names can contain underscores, our parser anchors on the leading time and the trailing `_<digits>.mp4` to extract the camera in between.

## Path format

The path containing the file encodes the date in two-digit form:

```
blink/26-04/26-04-28/04-38-40_garage_001.mp4
       │     │
       │     └── YY-MM-DD: 2026-04-28
       └──────── YY-MM:    2026-04
```

The two-digit year is assumed to be 21st century (2000+YY). This will technically break in 2100. We're comfortable with that.

## Time zone

Times in the path and filename are **UTC**. The SM2 doesn't know your local timezone. When syncing, we convert to the user's configured timezone for output filenames.

## What the SM2 does on attach

When you plug the Pi-as-USB-drive into the SM2's USB port:

1. SM2 reads the partition table.
2. If the partition is exFAT *and* the filesystem skeleton above is intact, the SM2 silently treats it as one of its own and starts using it.
3. If the partition is exFAT but the skeleton is missing/corrupt, the SM2 prompts in the Blink mobile app to format it.
4. If the partition is anything else (ext4, FAT32, missing partition table, ...), the SM2 prompts in the Blink mobile app to format it.

This is critical for the nightly cleanup design. **Reformatting the backing image** (e.g. `mkfs.exfat`) **would trigger the format prompt every morning.** Instead, we delete only the `YY-MM/` month directories inside `/blink/`, preserving everything else. The SM2 sees the same filesystem skeleton come back online and resumes recording with no human interaction.

## Concurrent access

exFAT has **no multi-host coordination.** When both the SM2 and the Pi mount the same filesystem (the SM2 read-write, the Pi read-only via loop), there's no locking, no journaling visible to the other side, and no guarantees about cache coherency.

In practice we've found this works fine because:

1. We mount **read-only** from the Pi. We never write.
2. We **drop kernel page caches** before each scan so the Pi re-reads metadata from disk fresh.
3. We **skip `.tmp/`** so we never see in-progress writes.
4. We **wait 500ms and re-stat** each candidate clip. If size or mtime changed in that window, we skip and try next cycle.
5. We **run ffprobe** as a final guard — anything that doesn't parse as a valid MP4 with non-zero duration is rejected.

In ~24 hours of real-world running we've never seen a corrupt clip make it through these layers, but the layers exist because exFAT-without-coordination could theoretically produce one.

## Backing image size

The SM2 will use as much of the drive as you give it. Real-world clips are usually 700KB–4MB. A typical household with a few cameras generates maybe 50-200MB/day of clips. The recommended backing image size is **2-4 GB**, which gives you ~1-2 weeks of headroom even without the nightly wipe — well past any plausible failure-recovery scenario.

A 10 GB image works too, but the larger the image, the more the Pi has to read on each scan (because the SM2 may have written anywhere in the partition). 4 GB is a good default.

## What we don't know

A few things we've intentionally not tested:

- **Multiple SM2s sharing one Pi** — we only have one SM2.
- **What happens if the backing image fills up** — should be a "drive full" error from the SM2's perspective, but we haven't induced it.
- **What firmware version we're running against.** Blink doesn't expose the SM2's firmware version through any consumer API I'm aware of. If you find a version mismatch, please open an issue.
- **Whether the SM2 ever writes outside `/blink/` and `/blink_backup/`** during normal operation. We assume not. If it does, we'd miss it.

If you find anything different in your own SM2, **please open an issue or PR** — these are the kinds of contributions that make this project better.
