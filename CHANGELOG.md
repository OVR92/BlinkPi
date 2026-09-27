# Changelog

All notable changes will be documented here. Versioning is based on
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-09-25

### Fixed

- **Sync Module reported "no USB drive" after the nightly wipe.** The wipe
  stopped the USB gadget but only restarted it on the happy path; any error
  during deletion (or the early return when `blink/` was missing) left the
  gadget down until reboot. The restart now lives in a `finally` block.
- The 30-second sync timer could fire mid-wipe, find the wipe's read-write
  mount at the shared mount point, and unmount it from under the wipe -
  the usual trigger for the crash above. The wipe now pauses the sync
  timer for its duration and uses its own mount point (`<mount_point>_wipe`).
- The wipe aborts (and restarts the gadget) if stopping the gadget fails,
  instead of modifying the image while the SM2 may still be writing to it.
- With `wipe.retention_days > 0` the sync state was reset every night, so
  surviving clips were re-validated and re-pushed to rclone each morning.
  State entries are now pruned to the clips that survived the wipe.
- The clip browser's read-only mount is dropped after the wipe so it does
  not serve stale directory listings.
- Sync remounts a stale leftover mount instead of reusing it, logs unmount
  failures, and no longer crashes if a directory vanishes mid-walk.

### Added

- **Setup & maintenance page** (`bub-setup`, port 80): Wi-Fi, SMB and
  rclone configuration with connection tests, status, logs, update and
  reboot. Optional admin password. Installed by `install.sh`.
- **Fallback Wi-Fi hotspot**: when the Pi has no network for a minute it
  broadcasts `BlinkPi-Setup` with the setup page as a captive portal.
- **rclone GUI**: create remotes for Google Drive, Dropbox, Box, pCloud,
  OneDrive (token paste), S3, B2, SFTP, WebDAV, FTP, or paste a raw
  `rclone.conf` section; test with one click.
- **SD card image build** (`image/build.sh`, pi-gen) for flash-and-go installs.
- `setup:` section in `config.yaml` (hotspot SSID/password, page port).
- **Notifications**: email (SMTP) and JSON webhook channels, optionally for
  every new clip (thumbnail attached), configured and testable from the
  new *Alerts & AI* tab. Secrets live in `secrets.yaml` (0600).
- **Watchdog**: alerts when no clip has been recorded for N hours (with
  quiet hours), when sync keeps failing, and when the USB gadget is down;
  sends recovery messages; runs after every sync pass.
- **AI clip summaries**: frames are extracted from each new clip with
  ffmpeg and described by a vision model behind any OpenAI-compatible
  endpoint (OpenAI, OpenRouter, Groq, Gemini, local Ollama / LM Studio),
  with a live model picker. Descriptions show in the clip browser and in
  notifications; hourly cap and per-camera filter.
- **Email as a destination**: `notify.email.on_new_clip: clip` mails each
  clip as an attachment (with retry state like SMB/rclone), a zero-setup
  off-site backup. `summary` sends the thumbnail and description instead.
  Home Assistant users can alternatively feed the webhook into LLM Vision
  (see `examples/home-assistant-llmvision-automation.yaml`).

### Changed

- `install.sh` always installs the `[web]` extras (needed by the setup page)
  and replaces its own fstab entry when the SMB share changes.

## [0.1.0] - Initial release

- Pi-as-USB-mass-storage gadget mode for SM2
- Periodic sync of clips from the backing image to configured destinations
- SMB and rclone destinations built in; pluggable architecture for more
- Nightly cleanup that preserves the SM2's filesystem skeleton (no format prompt)
- Optional retention-based pruning on rclone destinations
- Single-config-file installation
- Documented SM2 filesystem layout for community knowledge
