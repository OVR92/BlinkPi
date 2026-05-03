"""blink-usb-bridge: relay Blink Sync Module 2 motion clips off the device.

The Pi presents itself as USB mass storage to a Blink Sync Module 2.
The SM2 records motion clips to that storage. We loop-mount the same
backing image read-only, find new clips, validate them, and push them
to one or more destinations (SMB share, rclone remote, ...).

Public CLI entry points (installed as console_scripts):
  bub-sync     run one sync pass; suitable for cron / systemd timers
  bub-wipe     nightly cleanup of old clips on the backing image
  bub-prune    age out old clips on rclone destinations
  bub-web      optional local web UI for browsing clips on the Pi

Library entry point:
  from blink_usb_bridge import config
  c = config.load()
"""

__version__ = "0.1.0"
