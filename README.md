# blink-usb-bridge

**Relay Blink Sync Module 2 motion clips off the device, automatically, with no Blink subscription required.**

A Raspberry Pi (Zero 2 W is plenty) plugs into your Sync Module 2's USB port and pretends to be a USB hard drive. The SM2 happily writes motion clips to that "drive." Every 30 seconds, the Pi reads the same backing file from inside, finds new clips, and pushes them anywhere you want — an SMB share on your Windows PC or NAS, an rclone remote (Google Drive, Dropbox, S3, Backblaze, ...), or both.

You get full local clip access, all your motion footage at any retention you want, and you keep using the Blink app and integrations as normal.

## What this gives you

- **Local copies of every motion clip**, automatically, within ~30 seconds of recording
- **No Blink subscription needed** for clip access (you still need one for cloud backup, but you don't need cloud backup if you have this)
- **Two destinations out of the box**: SMB share (for archiving) and rclone (for cloud backup with retention)
- **Pluggable destinations** — adding S3 or MQTT or whatever is a 50-line file
- **Optional local web UI** at `http://blinkpi.local:8080` — browse and play clips on the Pi itself, no external service required
- **Nightly cleanup** with configurable retention (delete everything, or keep N days as a buffer)
- **Clean filenames** — `2026-04-27_21-38-40_garage.mp4` — sortable, human-readable

## What this is not

- Not real-time. There's a 30-second polling delay (configurable).
- Not a replacement for the Blink integration in Home Assistant. Use both — Blink for motion sensors and live view, this for the actual clip files.
- Not magic. The SM2's clip recording is still subject to all the SM2's quirks — battery, camera connectivity, Wi-Fi, etc.

## How it works

```
┌─────────────────┐  USB-OTG cable  ┌──────────────┐
│ Sync Module 2   │◀────────────────│ Raspberry Pi │
│ (sees a USB     │                 │ (presents    │
│  flash drive)   │                 │  itself as   │
└─────────────────┘                 │  USB drive)  │
                                    └──────┬───────┘
                                           │ loop-mount the same backing
                                           │ image read-only every 30s
                                           ▼
                                    ┌──────────────┐
                                    │  sync_clips  │
                                    └──────┬───────┘
                                           │
                          ┌────────────────┼────────────────┐
                          ▼                ▼                ▼
                   ┌────────────┐   ┌────────────┐   ┌────────────┐
                   │ SMB share  │   │  rclone    │   │   ...      │
                   │ (Windows,  │   │ (any of    │   │ (your own  │
                   │  NAS, ...) │   │  70+       │   │  plugin)   │
                   └────────────┘   │ backends)  │   └────────────┘
                                    └────────────┘
```

The Pi acts as a USB mass-storage device using `g_mass_storage`. The SM2 sees it as a regular USB drive, formats it as exFAT (with a partition table starting at sector 32 — see [docs/SM2_FILESYSTEM.md](docs/SM2_FILESYSTEM.md)), and writes motion clips to it.

The Pi also loop-mounts that same backing image read-only and walks the filesystem to find new clips. Because exFAT has no multi-host coordination, we drop page caches every cycle, and we use the SM2's `.tmp/` staging convention as our atomic-write boundary.

## Getting started

You'll need:

- **Raspberry Pi Zero 2 W**, Pi 4, or any Pi with USB-OTG support. Pi Zero 2 W is recommended — small, cheap, low-power.
- **A USB-OTG cable** (USB-A male on the SM2 side, USB-A male on the Pi's USB-OTG port — yes, both ends are male; the cable converts the Pi's micro-USB-OTG port to USB-A male)
- **Raspberry Pi OS Lite** (Bookworm or later)
- **A Blink Sync Module 2**

See [docs/INSTALL.md](docs/INSTALL.md) for the full step-by-step guide. The short version:

```bash
# On the Pi
git clone https://github.com/OVR92/BlinkPi
cd blink-usb-bridge
cp config.example.yaml config.yaml
$EDITOR config.yaml          # set destinations, paths, timezone
sudo ./scripts/install.sh
sudo reboot                  # picks up the dwc2 overlay change
```

Plug the cable into the SM2's USB port. The Blink app will prompt you to format the new "USB drive" — accept it. From then on, motion clips flow.

## Configuration

Everything lives in one `config.yaml`. See [config.example.yaml](config.example.yaml) — every option is documented inline. The bits you actually need to change to get going:

- `pi_user` — the unprivileged user the Pi service runs as
- `timezone` — your local IANA timezone
- `destinations.smb.*` — your SMB server, share, credentials
- (optional) `destinations.rclone.*` — your rclone remote name and retention

## Documentation

- [INSTALL.md](docs/INSTALL.md) — full setup walkthrough including hardware
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces fit together, design choices
- [SM2_FILESYSTEM.md](docs/SM2_FILESYSTEM.md) — what we discovered about the SM2's on-disk format (the part that took the longest to figure out)
- [HARDWARE.md](docs/HARDWARE.md) — Pi setup, cable type, power, gotchas
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common issues and how to fix them

## Home Assistant

If you use Home Assistant, you can browse clips through the Media browser by pointing HA's network storage at the same SMB share the Pi pushes to. There's a one-line example in [examples/home-assistant-gallery-card.yaml](examples/home-assistant-gallery-card.yaml). HA configuration is otherwise outside the scope of this project.

## Adding your own destination

The destination plugin pattern is documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#destinations). Three methods to implement, register it in `Destination.from_config()`, done. Pull requests welcome.

## License

MIT. See [LICENSE](LICENSE).

## Caveats and credits

This project came from one person's weekend curiosity about whether the SM2's USB port could be tricked into letting them read their own footage. None of the SM2's USB filesystem is documented by Blink — the format details in [docs/SM2_FILESYSTEM.md](docs/SM2_FILESYSTEM.md) were figured out empirically. If a future SM2 firmware update changes the layout, this project will need an update too.

Use at your own risk. This isn't endorsed by Blink or Amazon. The SM2 doesn't seem to mind being USB-spoofed (it just sees a USB drive), but if Blink ever decides they care, your mileage may vary.
