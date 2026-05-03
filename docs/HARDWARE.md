# Hardware setup

## Pi model

A **Raspberry Pi Zero 2 W** is the recommended hardware. It's:

- Cheap (~$15)
- Small enough to tape behind the SM2
- Has USB-OTG support out of the box
- Plenty fast for the polling and rsync workload

A regular **Pi 4** or **Pi 5** also works fine. Pi 3 is borderline — it has USB-OTG but the host port wiring is shared, which complicates things.

A first-gen **Pi Zero W** technically works but is noticeably slower for the periodic work.

## OS

**Raspberry Pi OS Lite (Bookworm or later)** is what the install script targets. The 64-bit Lite image is recommended.

Anything Debian-based with systemd should work with minor tweaks. The install script assumes `/boot/firmware/cmdline.txt` and `/boot/firmware/config.txt` (Bookworm paths) but falls back to `/boot/cmdline.txt` and `/boot/config.txt` if those don't exist.

## USB-OTG cable

This is the part most people get wrong.

The Pi Zero 2 W has two micro-USB ports. **The one labeled "USB" (closer to the middle of the board) is the USB-OTG port.** The one labeled "PWR" is power-only.

You need a cable that:

1. Plugs into the Pi's USB-OTG port (micro-USB male)
2. Terminates in a USB-A male connector that plugs into the SM2's USB-A port

So both ends of the cable are male. These are "USB-OTG host adapters" or "USB-OTG cables." Search those terms and you'll find them for a few dollars. Some have a separate power input pigtail; you don't need that variant for this project, but it doesn't hurt.

For Pi 4 / Pi 5, the USB-OTG port is one of the USB-C ports — same idea, different cable.

## Powering the Pi

The Pi needs power **separately from the SM2 connection**. The Pi-OTG cable carries data, not enough power for the Pi itself.

Plug a normal Pi power supply (5V, ~2A for Zero 2 W) into the Pi's "PWR" micro-USB port.

If you skip this and try to power the Pi from the SM2's USB port, two things happen:
- The SM2 doesn't put out enough current
- More importantly, USB peripherals don't power their hosts; they're powered by their hosts

## Physical layout

I have my Pi taped to the back of the SM2, with the Pi's power cable going to a wall wart and a short OTG cable looping into the SM2's USB port. Looks like this:

```
   Wall wart
      │
      │ (USB-A → micro-USB power)
      ▼
   ┌─────┐                ┌─────────────────┐
   │ Pi  │── OTG cable ──▶│ Sync Module 2   │
   │     │                │  (USB-A port)   │
   └─────┘                └─────────────────┘
```

If you have your SM2 in an awkward location, a longer OTG cable is fine — USB 2.0 will reliably do up to 5m.

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

The Pi handles all the work locally; if your internet drops, the SMB destination still works (assuming the kitchen PC is on the same LAN). If both internet and LAN drop, sync just retries next cycle. Nothing is lost from the SM2 — it keeps writing to the backing image.

The SM2 has no idea anything unusual is happening. From its perspective, you've just plugged in a perfectly normal USB drive.
