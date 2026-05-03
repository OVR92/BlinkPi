# Troubleshooting

## "The Blink app doesn't see the USB drive"

The Pi isn't presenting itself as USB mass storage. Common causes:

```bash
# Is the gadget module loaded?
lsmod | grep g_mass_storage
```

If nothing returns, the dwc2 overlay isn't active. Check:

```bash
grep dwc2 /boot/firmware/cmdline.txt
grep dwc2 /boot/firmware/config.txt
```

You should see `modules-load=dwc2` in cmdline and `dtoverlay=dwc2,dr_mode=peripheral` in config. If either is missing, the install script didn't apply correctly. Re-run it, or add by hand and reboot.

If the overlay is active but `g_mass_storage` isn't loaded:

```bash
sudo systemctl status blink-gadget.service
```

Common error: backing image path doesn't exist. The install script creates it, but if you changed paths in `config.yaml` after install, you'll need to recreate it:

```bash
truncate -s 4G /home/<pi_user>/blink-usb-bridge/usb_backing.img
sudo systemctl restart blink-gadget.service
```

If everything looks right but the SM2 still doesn't see the drive: check your USB-OTG cable. The Pi has two micro-USB ports; the OTG port is the one labeled "USB" (closer to the middle), not "PWR". And the cable needs to be a true USB-OTG cable (sometimes called a "USB host adapter") with USB-A male on the other end.

## "Sync runs but no clips appear at the destination"

```bash
# Run sync manually to see fresh logs
sudo -u <pi_user> ~/blink-usb-bridge/.venv/bin/bub-sync

# Or watch the systemd timer's runs
journalctl -u blink-sync.service -n 50 --no-pager
```

Common causes:

**Backing image isn't mounted.** Check for "mount failed" in the log. If `mount` fails with `wrong fs type, bad option, bad superblock`: you probably forgot the `offset=16384` in your mount command (the script handles this automatically — if you're seeing it, the script's MIN_BYTES guard or path is wrong). Verify the backing image actually has the SM2's filesystem:

```bash
sudo file /path/to/usb_backing.img
# should mention "DOS/MBR boot sector"
```

If it shows zeros or wrong format, the SM2 hasn't formatted it yet. Make sure the gadget service is running and the SM2 is connected; open the Blink app and accept the format prompt.

**Path doesn't match expected SM2 layout.** Look for "unparseable SM2 path" warnings. This usually means a future SM2 firmware changed the layout. Open an issue with the path that didn't parse.

**Clips fail validation.** Look for "ffprobe rejected" or "below min_bytes". The clip is corrupt or truncated. Usually transient — next motion event records a complete one.

## "SMB destination fails"

```bash
# Test the mount manually
ls /mnt/blink-share

# If empty/error: check the automount
sudo systemctl status mnt-blink\\x2dshare.automount

# Check creds file
sudo cat /etc/samba/credentials/blink
```

Common causes:

**Wrong credentials format.** The file must be:
```
username=USER
password=PASS
```
No spaces around `=`, no quotes, mode 0600 owned by root.

**Share not actually shared with that user.** From the Pi, try:
```bash
sudo apt install -y smbclient
smbclient -L //YOUR_SERVER -U YOUR_USER
```

You should see your share in the list. If not, the share permissions on the server side are wrong.

**Firewall blocking SMB.** Port 445/tcp needs to be open between the Pi and the server. Most home networks don't filter this; corporate networks often do.

**Wrong SMB protocol version.** Some older NAS devices need `vers=2.0` or `vers=1.0`. Edit `destinations.smb.smb_version` in config and re-run the install script.

## "rclone destination fails"

```bash
# Test rclone directly
rclone ls myremote:

# If permission denied: re-run rclone config
rclone config
```

Common causes for Google Drive specifically:

**Service account on a personal Drive.** Service accounts have **zero quota** on personal Drives. You need a Workspace account and a Shared Drive.

**Service account doesn't have Manager role.** Permanent-delete (`--drive-use-trash=false`) requires Manager role on the Shared Drive.

**Wrong scope.** The rclone config should use `scope=drive` (full Drive access). The narrower `scope=drive.file` only lets the SA see files it created itself — which works initially but breaks pruning later.

## "Wipe didn't run / SM2 prompts for format"

```bash
# Check the wipe ran
journalctl -u blink-wipe.service --since "yesterday" --no-pager

# Should end with "wipe complete"
```

If wipe ran but the SM2 prompts for format, something deleted the filesystem skeleton. Check the log for:
- "removed N month directories" — should be a small number, not "removed everything"
- Warnings about "expected directory missing after wipe"

If the skeleton is gone, you'll need to let the SM2 reformat the drive (just accept the prompt in the Blink app once). Then re-enable the wipe and it should work normally.

## "Pi runs out of disk space"

The backing image is the largest file. Check:

```bash
du -sh /home/<pi_user>/blink-usb-bridge/
df -h /
```

If disk is full, the wipe didn't run or didn't run successfully. After fixing, you may need to manually trigger one:

```bash
sudo systemctl start blink-wipe.service
```

## "Everything looks OK but clips are out of date"

The Pi might be polling but not pushing. Check:

```bash
# When was the last successful sync run?
journalctl -u blink-sync.service -n 20 --no-pager | grep "done"
```

If runs are happening but pushing 0 files: are clips actually appearing on the SM2 side? The most reliable test is to walk in front of a camera, wait a full minute, and watch:

```bash
sudo mount -o ro,loop,offset=16384 \
    /home/<pi_user>/blink-usb-bridge/usb_backing.img \
    /tmp/sm2-debug
ls -la /tmp/sm2-debug/blink/$(date +%y-%m)/$(date +%y-%m-%d)/
sudo umount /tmp/sm2-debug
```

If you see the clip there but sync isn't picking it up: there's a state file or path mismatch. Stop the sync timer, delete `sync_state.json`, restart the timer.

If you DON'T see the clip: the SM2 isn't recording for this event. That's a Blink/SM2-side problem, not something this project can fix.

## "I want to start fresh"

Stop everything, wipe the backing image, reset state:

```bash
sudo systemctl stop blink-sync.timer blink-wipe.timer bub-prune.timer
sudo systemctl stop blink-gadget.service
sudo rm /home/<pi_user>/blink-usb-bridge/sync_state.json
truncate -s 0 /home/<pi_user>/blink-usb-bridge/usb_backing.img
truncate -s 4G /home/<pi_user>/blink-usb-bridge/usb_backing.img  # or your size
sudo systemctl start blink-gadget.service
sudo systemctl start blink-sync.timer blink-wipe.timer bub-prune.timer
```

Open the Blink app and accept the format prompt for the new "USB drive" the SM2 sees.

## Still stuck?

Open an issue on GitHub with:
- Pi model and OS version
- `cat config.yaml` (with credentials redacted!)
- `journalctl -u blink-sync.service -n 100 --no-pager`
- What you did, what happened, what you expected

The more specific, the better.
