# Installation

This walks you through setting up blink-usb-bridge from scratch. Budget about 30 minutes.

## Prerequisites

- A Pi Zero 2 W (or other USB-OTG-capable Pi) running Raspberry Pi OS Lite
- A USB cable (see [HARDWARE.md](HARDWARE.md))
- A Sync Module 2
- SD Card 
- An SMB-shared folder on your network *or* an rclone-configured remote (or both)
- About 30 minutes

## 1. Prepare the Pi

Flash Raspberry Pi OS Lite (64-bit, Bookworm or later) to an SD card. Configure SSH access and a non-root user when flashing — call them whatever you like; the install script will use whichever username you set in `config.yaml`.

Boot the Pi and SSH in. Update the system:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-pip ffmpeg cifs-utils rclone git
```

## 2. Set up your destinations

### SMB share (Windows or NAS)

On the machine that will host the share:

1. Create a folder, e.g. `C:\BlinkClips` (Windows) or `/mnt/big_drive/blink` (Linux).
2. Share it with read-write permissions for a dedicated user.
3. On the Pi, create a credentials file:

```bash
sudo mkdir -p /etc/samba/credentials
sudo tee /etc/samba/credentials/blink > /dev/null <<'EOF'
username=blinkpi
password=YOUR_SECRET_PASSWORD
EOF
sudo chmod 0600 /etc/samba/credentials/blink
sudo chown root:root /etc/samba/credentials/blink
```

### rclone (optional, for cloud backup)

```bash
rclone config
```

Walk through the prompts to set up your remote. Detailed rclone docs at https://rclone.org/docs/. **A few specifics that matter:**

- For Google Drive, you almost certainly want a **Service Account** with **Manager** permission on a **Workspace Shared Drive**. Personal Drives don't allow service-account uploads (zero-quota issue), and lower permission levels don't allow permanent-delete for pruning old clips.
- The `remote:` you reference in `config.yaml` should match the name you used in `rclone config`.
- For permanent-delete (skipping trash), Google Drive needs Manager role on the Shared Drive.

Verify it works:

```bash
echo "test" > /tmp/test.txt
rclone copy /tmp/test.txt myremote:
rclone ls myremote:
rclone delete myremote:test.txt
```

## 3. Clone and configure

```bash
cd ~
git clone https://github.com/OVR92/BlinkPi
cd blink-usb-bridge
cp config.example.yaml config.yaml
```

Open `config.yaml` in your favorite editor. **The minimum fields you need to update:**

- `pi_user` — your Pi username
- `project_dir` — usually `/home/<pi_user>/blink-usb-bridge` (must match where you cloned)
- `backing_image_path` — usually inside `project_dir`
- `mount_point` — usually inside `project_dir`
- `timezone` — your IANA timezone, e.g. `America/Los_Angeles`
- Under `destinations.smb`, set `enabled: true` and fill in `server`, `share`, `credentials_file`
- Under `destinations.rclone` if used, set `enabled: true`, `remote`, `config_file`

Every option is documented inline in [config.example.yaml](../config.example.yaml).

## 4. Run the installer

```bash
sudo ./scripts/install.sh
```

The installer will:

1. Create a Python virtualenv in `.venv/` and install the package
2. Generate systemd unit files from your config
3. Add the dwc2 USB-OTG overlay to `/boot/firmware/config.txt`
4. Add `modules-load=dwc2` to `/boot/firmware/cmdline.txt`
5. Create the empty backing image file
6. Add a sudoers entry for the unprivileged mount/umount calls
7. Add an `/etc/fstab` entry for SMB automount (if SMB enabled)
8. Enable the systemd timers

If anything fails it'll tell you. The installer is idempotent — running it again won't break things.

## 5. Reboot

```bash
sudo reboot
```

The reboot is needed to load the dwc2 module (gadget mode).

## 6. Plug in to the SM2

Plug the Pi's USB port into the SM2's USB port. The SM2 should light up and the Blink app should show a notification or prompt.

Open the Blink app on your phone:
- Go to **Sync Module 2 → Local Storage**
- You should see a "Connect Local Storage" prompt for a new USB drive
- Tap **Format** to let the SM2 format the drive
- After formatting, the SM2 starts using it for motion clips

## 7. Trigger a clip

Walk past one of your cameras. Wait ~30 seconds, then on the Pi:

```bash
journalctl -u blink-sync.service -f
```

You should see a sync run pick up the clip and push it to your destination(s). Check the destination too — you should see a file like `2026-04-27_21-38-40_garage.mp4`.

## 8. Verify the nightly cleanup

```bash
sudo systemctl list-timers blink-*
```

You should see `blink-wipe.timer` scheduled for the time you set (3 AM by default). Don't worry about testing the wipe right now — it'll run on its own and the next morning you can check `journalctl -u blink-wipe.service` to see what it did.
