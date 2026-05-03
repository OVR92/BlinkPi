# Hardware setup

## Pi model

A **Raspberry Pi Zero 2 W** is the recommended hardware. It's:

- Cheap (~$15)
- Small enough to tape behind the SM2
- Has USB-OTG support out of the box
- Plenty fast for the polling and rsync workload

## OS

**Raspberry Pi OS Lite (Bookworm or later)** is what the install script targets. The 64-bit Lite image is recommended.

Anything Debian-based with systemd should work with minor tweaks. The install script assumes `/boot/firmware/cmdline.txt` and `/boot/firmware/config.txt` (Bookworm paths) but falls back to `/boot/cmdline.txt` and `/boot/config.txt` if those don't exist.

## USB cable

The Pi Zero 2 W has two micro-USB ports. **The one labeled "USB" (closer to the middle of the board) is the one that you connect to the blink sync module.** The one labeled "PWR" is power-only.
If you power the sync module and Pi seperately from different usb power supplies you might want to cut the power wires (usually red and black) in the cable that connects the Pi and the sync module. That way you dont risk them intefering. I used a splitter charger cable (USB-A to two micro USB) to power both devices from one USB PSU, so probably not necesarry then, but I wanted to shorten up my USB cable for connecting the Pi and Sync anyway to make it more tidy, so when I did that I made it data only just to be safe too.

## Physical layout
<img width="807" height="640" alt="image" src="https://github.com/user-attachments/assets/0c2d01e4-f27c-4e7d-891c-478362e2072e" />


## Network

The Pi needs network access to push clips to destinations. Wi-Fi on the Zero 2 W works fine. If you have ethernet via a USB-OTG hub (since the OTG port is consumed), you can do that, but it's overkill for this workload.

The Pi only sends clips out — it doesn't need to receive any inbound traffic, so no port forwarding or anything special.

## Storage

The Pi's SD card needs to fit:

- The OS (~2 GB)
- The backing image you'll create (default 4 GB)
- Logs and the project itself (negligible, < 100 MB)

A 16 GB SD card has plenty of headroom. 8 GB works if you keep the backing image at 2 GB.

## Power consumption and reliability

The Pi Zero 2 W idles around 0.5W and peaks around 1.5W during sync runs. Annual electricity cost is on the order of a dollar.

The Pi handles all the work locally; if your internet drops, the SMB destination still works (assuming the SMB share is on the same LAN). If both internet and LAN drop, sync just retries next cycle. Nothing is lost from the SM2 — it keeps writing to the backing image.

The SM2 has no idea anything unusual is happening. From its perspective, you've just plugged in a perfectly normal USB drive.
