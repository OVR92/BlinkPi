<p align="center">
  <img width="800" height="800" alt="blinkpi logo" src="https://github.com/user-attachments/assets/c3e4f004-588b-4a1a-89bd-b6a3a3abd3c9" />
</p>

<p align="center">
  <strong>Automatically save every Blink motion clip to your local network or cloud - no subscription required!</strong>
</p>

<p align="center">
  ⚠️ <strong>Requires no active Blink subscription.</strong> See <a href="#subscription-compatibility">Subscription compatibility</a> below.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/status-beta-orange" alt="Beta">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License">
  <img src="https://img.shields.io/badge/hardware-Pi%20Zero%202%20W-red" alt="Pi Zero 2 W">
  <img src="https://img.shields.io/badge/polling-30s-green" alt="30s polling">
</p>


## How it works

A Raspberry Pi Zero 2 W plugs into your Sync Module 2's (SM2) USB port and pretends to be a USB flash drive. The SM2 happily writes motion clips to it. Every 30 seconds, the Pi reads the same backing file, finds new clips, validates them, and pushes them to your SMB share, rclone remote (Google Drive, S3, Dropbox, …), or both. There's also an optional local web UI to browse and play clips instantly on your home network.

![Physical setup](https://github.com/user-attachments/assets/0c2d01e4-f27c-4e7d-891c-478362e2072e)

The Pi uses Linux's `g_mass_storage` gadget to present a backing image file as a USB drive. The SM2 formats it as exFAT and writes motion clips to it. The Pi loop-mounts the same file read-only every 30 seconds, walks the filesystem for new clips, validates each one (size check → 500ms stability window → `ffprobe`), and pushes to your configured destinations.

The SM2 has no idea anything unusual is happening — from its perspective it's a normal USB flash drive.

> **Note on cloud dependency:** Blink cameras require an active connection to
> Blink's servers to function, and clips may travel via the cloud before reaching
> the Sync Module (depending on model). BlinkPi only handles clips *after* they land on local storage,
> it does not bypass any cloud capture pipelines.

---

## What you get

| | |
|---|---|
| 📁 **Automatic backup** | Every motion clip lands on your NAS or cloud within ~30 seconds. No manual downloads. |
| 🚫 **No subscription needed** | Works entirely off the SM2's free local USB storage feature. **Requires no active Blink subscription** — see below. |
| 🌐 **Local web UI** | Browse and play clips at `http://blinkpi.local:8080` — instant loads, no cloud roundtrip. |
| 🗂️ **Clean filenames** | Clips renamed to `2026-04-27_21-38-40_garage.mp4` — sortable and human-readable. |
| 🔌 **Pluggable destinations** | SMB and rclone built in. Adding a new destination is ~50 lines of Python. |
| 🧹 **Nightly cleanup** | Automatically wipes the backing image without triggering the SM2's format prompt. |
| 🔔 **Notifications** | Email or webhook for every clip, plus a watchdog that tells you when recording has stopped. |
| 🤖 **AI summaries** | Optional: a vision model of your choice describes each clip ("A delivery driver leaves a parcel at the door"). |

![Local web UI](https://github.com/user-attachments/assets/2fd18bc7-afb9-43bf-976e-e67ab414354e)


---

## Subscription compatibility

**BlinkPi only works on accounts without an active Blink subscription plan.**

When no subscription is active, the SM2 writes each motion clip directly to the USB drive as it is recorded — BlinkPi's 30-second polling loop picks these up in near-real time.

When a subscription *is* active, Blink enables "Clip Backup" and changes the local storage behavior: instead of writing clips continuously, the SM2 performs a **once-daily batch backup** of clips from your cloud storage to the USB drive. Backed-up clips are also written to a **different path** (`blink_backup/` instead of `blink/`), which the current BlinkPi code explicitly skips — so no clips would be detected at all regardless of timing.

If you have a subscription, you have two options:

- **Drop the subscription:** Cancel or let it lapse, then re-format the USB drive from the Blink app. BlinkPi will work as normal. See [Blink's Clip Backup support article](https://support.blinkforhome.com/en_US/video-clips/how-do-i-use-clip-backup) for details.
- **Keep the subscription and patch the code:** [@bagofgag](https://github.com/bagofgag) worked out how to modify `sm2.py` to also walk the `blink_backup/` directory — see [issue #1](https://github.com/OVR92/BlinkPi/issues/1) for the details. Note you'll only get clips once a day in this mode.
- **Use the Blink API instead:** The [blinkpy](https://github.com/fronzbot/blinkpy) project can fetch clips directly from Blink's cloud via API calls, which is a better fit if you have a subscription and want near-real-time access.

---

## Hardware

| | |
|---|---|
| **Pi** | Raspberry Pi Zero 2 W (~$15) — small, cheap, USB-OTG, ~0.5W idle |
| **OS** | Raspberry Pi OS Lite 64-bit (Bookworm or later) |
| **USB cable** | USB-A to Micro-USB, **data capable**. Connect to the Pi's **USB port** (middle, labeled "USB") — not PWR. Consider snipping the power wires if powering the Pi and SM2 separately. |
| **SD card** | 16 GB recommended (quality brand — it'll see heavy writes) |

> ⚠️ Only works with **Sync Module models that have a USB port**. Newer Sync Modules with SD card slots are not compatible.

---

## Quick start (SD card image)

The easiest install: flash the prebuilt image, no SSH or YAML editing.

1. Flash `BlinkPi-*.img.xz` (from the [releases page](https://github.com/OVR92/BlinkPi/releases), or build it yourself with `./image/build.sh`) using Raspberry Pi Imager.
2. Boot the Pi Zero 2 W. After about a minute it broadcasts the Wi-Fi network **`BlinkPi-Setup`** (password `blinkpi123`). Join it from your phone; the setup page opens as a captive portal (or browse to http://10.42.0.1/).
3. Pick your home Wi-Fi on the **Network** tab. Then rejoin your own network and open **http://blinkpi.local/**.
4. Enable an SMB share and/or an rclone remote on the **Storage** / **Cloud** tabs and press **Apply**.
5. Plug the Pi's **USB** port into the Sync Module and tap **Format** under *Sync Module → Local Storage* in the Blink app.
6. Set an admin password on the **Settings** tab.

See [image/README.md](image/README.md) for details and for building the image.

## Quick start (manual install)

**1. Flash & boot the Pi**

Raspberry Pi OS Lite 64-bit, SSH enabled. Then:

```bash
sudo apt update && sudo apt full-upgrade -y
```

**2. Install dependencies**

```bash
sudo apt install -y python3-venv python3-pip ffmpeg cifs-utils rclone git
```

**3. Clone and configure**

```bash
git clone https://github.com/OVR92/BlinkPi
cd blink-usb-bridge
cp config.example.yaml config.yaml
$EDITOR config.yaml   # set pi_user, timezone, destinations
```

**4. Run the installer**

```bash
sudo ./scripts/install.sh
```

Idempotent — sets up systemd units, USB gadget overlay, sudoers, and fstab.

**5. Reboot**

```bash
sudo reboot
```

Required to activate the `dwc2` USB gadget overlay.

**6. Plug into the SM2's USB port**

Once plugged in, open the **Blink app** on your phone. Tap your **Sync Module 2 → Local Storage**. You should see a prompt to format the new USB drive — tap **Format** and confirm. The SM2 will format the Pi's virtual drive and begin writing clips to it.

**7. Trigger a test clip**

Walk past a camera, wait ~30 seconds, then watch:

```bash
journalctl -u blink-sync.service -f
```
Or if you enabled the local webserver in config, go to http://blinkpi.local:8080 and watch for the clip to appear.

---

## Setup & maintenance page

Both install methods give you a web page at **http://blinkpi.local/** (port 80, runs as root) with:

| Tab | What you can do |
|---|---|
| **Status** | Setup checklist, network, whether the SM2 has formatted the drive, service health, last sync |
| **Network** | Scan and join Wi-Fi, set the country code, configure or disable the fallback hotspot |
| **Storage** | SMB share (with a connection test), which rclone remote to use, retention on the Pi |
| **Cloud** | Create rclone remotes without a terminal: Google Drive / Dropbox / Box / pCloud / OneDrive via token paste, S3, B2, SFTP, WebDAV, FTP, or a raw `rclone.conf` section. One-click test. |
| **Settings** | Hostname, timezone, poll interval, clip browser on/off, admin password |
| **Maintenance** | Sync now, run the nightly cleanup, re-plug the USB drive, update from git, reboot, view logs |

**Fallback hotspot:** if the Pi has no IP address for 60 seconds it starts the `BlinkPi-Setup` access point so you can always reach this page. While the hotspot is up it retries your saved Wi-Fi every 5 minutes. Disable it under *Network* if you don't want that (e.g. wired installs).

**Set an admin password.** Until you do, anyone on your LAN can open the page and it can reconfigure the Pi as root.

## Notifications, watchdog and AI summaries

All three are configured on the setup page's **Alerts & AI** tab (or under `notify:`, `watchdog:` and `ai:` in `config.yaml`; secrets go in `secrets.yaml`).

**Email** uses any SMTP server (for Gmail: `smtp.gmail.com`, port 587, STARTTLS, an app password). **Webhook** POSTs a JSON document per event; the new-clip payload looks like:

```json
{"event": "new_clip", "camera": "garage", "filename": "2026-05-03_17-08-41_garage.mp4",
 "timestamp": "2026-05-03T17:08:41-07:00", "duration": 12.0, "size_bytes": 1823311,
 "smb_path": "/mnt/blink-share/garage/2026-05-03_17-08-41_garage.mp4",
 "smb_relative_path": "garage/2026-05-03_17-08-41_garage.mp4",
 "description": "A person walks up the driveway carrying a box ...", "alert": true, "tags": ["person", "delivery"]}
```

**Watchdog** runs after every sync pass and alerts (once, with reminders and a recovery message) when:
- no clip has been recorded for `max_silence_hours` (outside optional quiet hours),
- sync has failed `failures_before_alert` times in a row,
- the USB gadget service is down, which is what the Sync Module reports as "no USB drive".

**AI summaries** extract a few frames from each new clip with ffmpeg and ask a vision model to describe them. Any **OpenAI-compatible endpoint** works: OpenAI, OpenRouter, Groq, Google Gemini, or a local Ollama / LM Studio for free. Pick a service preset (or a custom base URL), press *Fetch* to list its models, and choose one that accepts images. Choose either N evenly spaced frames or one frame every N seconds, cap clips per hour, and restrict to specific cameras. Descriptions appear in the clip browser and in notifications. Small hosted vision models cost well under a cent per clip; a local model costs nothing.

**Email as backup.** Setting the email option to *attach the clip itself* turns the mailbox into a simple off-site copy: every clip arrives as an attachment named like `2026-05-03_17-08-41_garage.mp4`, with the AI description in the body. It is a proper destination with its own retry state, so a failed send is retried on the next pass. Watch your provider's limits (Gmail: 25 MB per message, about 500 messages a day on personal accounts, 15 GB of storage).

**Home Assistant + LLM Vision:** if you already run [LLM Vision](https://llmvision.org) in Home Assistant, you can skip the built-in AI and analyse the clips there with any provider LLM Vision supports. Point the BlinkPi webhook at an HA webhook trigger; the payload carries the clip's path on the SMB share. See [examples/home-assistant-llmvision-automation.yaml](examples/home-assistant-llmvision-automation.yaml).

## Configuration

Everything lives in one `config.yaml`. Key fields:

| Field | Description |
|---|---|
| `pi_user` | Unprivileged user the sync service runs as |
| `timezone` | IANA timezone for output filenames, e.g. `America/Los_Angeles` |
| `backing_image_path` | Path to the 4 GB virtual USB drive file |
| `destinations.smb.*` | Server, share name, credentials file |
| `destinations.rclone.*` | Remote name, retention days, prune marker |

See [`config.example.yaml`](config.example.yaml) for every option with inline documentation.

---

## Architecture

Three systemd units do all the work:

| Unit | Schedule | What it does |
|---|---|---|
| `blink-gadget.service` | On boot | Loads `g_mass_storage` — Pi becomes a USB drive |
| `blink-sync.timer` | Every 30s | Drop page cache → mount RO → find new clips → validate → push |
| `blink-wipe.timer` | Nightly 3 AM | Final sync → stop gadget → delete month dirs → restart gadget |

**Why not reformat nightly?** The SM2 prompts for human confirmation whenever it sees an unfamiliar filesystem. Instead the wipe deletes only the monthly clip directories inside `/blink/`, leaving the skeleton intact. The SM2 resumes recording with zero human interaction.

**Clip filenames on the SM2** follow the pattern:
```
blink/26-04/26-04-28/04-38-40_garage_001.mp4
             ↑           ↑
          YY-MM-DD    HH-MM-SS (UTC) + camera + seq
```
The Pi converts UTC timestamps to your configured timezone in output filenames.

---

## Adding a destination

Destinations implement a simple ABC in `destinations.py`:

```python
class Destination(ABC):
    name: str
    def available(self) -> bool: ...
    def push(self, local_path: Path, target_filename: str, camera: str) -> bool: ...
```

Subclass it, add a config block in `config.example.yaml` and a dataclass in `config.py`, register in `Destination.from_config()`. PRs welcome.

---

## Troubleshooting

**Sync Module says no USB drive is connected.** The USB gadget is down. Press *Re-plug USB drive* on the Maintenance tab (or `sudo systemctl start blink-gadget.service`). Versions before 0.2.0 could leave the gadget down if the nightly wipe hit an error; see the CHANGELOG.


| Symptom | First thing to check |
|---|---|
| Blink app doesn't see the USB drive | `lsmod \| grep g_mass_storage` — if empty, check `/boot/firmware/config.txt` for `dtoverlay=dwc2,dr_mode=peripheral` and reboot |
| Sync runs but no clips appear | Run `bub-sync` manually for live output; look for mount errors or "unparseable SM2 path" |
| SMB destination fails | Check credentials file: no spaces around `=`, no quotes, mode `0600` owned by root |
| SM2 prompts for format every morning | Wipe deleted the filesystem skeleton — accept format once, then check `journalctl -u blink-wipe.service` |
| rclone / Google Drive fails | Service accounts need **Manager** role on a **Workspace Shared Drive** — personal Drives have zero SA quota |

Still stuck? Open an issue with your Pi model + OS, `config.yaml` (credentials redacted), and `journalctl -u blink-sync.service -n 100`.

---

## Home Assistant

Point HA's network storage at the same SMB share the Pi pushes to and browse clips through the Media browser or using a "gallery card". See [`examples/home-assistant-gallery-card.yaml`](examples/home-assistant-gallery-card.yaml) for a one-line example.

---

*MIT License · Not affiliated with Blink or Amazon · Most code is AI-assisted — use at your own risk*


<a href="https://www.buymeacoffee.com/olafreese" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" 
       alt="Buy Me A Coffee" height="50">
</a>
