# Contributing

Thanks for taking a look. This started as a personal project that turned out useful enough to share, so contributions are welcome.

## Useful contributions

- **A new destination plugin** (S3, MQTT, an HTTP webhook, etc). See [docs/ARCHITECTURE.md#destinations](docs/ARCHITECTURE.md#destinations) for the pattern. ~50 lines of code per destination.
- **Confirmation that this works on a different SM2 firmware version**, or notes about layout differences if it doesn't. The SM2 filesystem details in [docs/SM2_FILESYSTEM.md](docs/SM2_FILESYSTEM.md) were figured out empirically against one specific firmware.
- **Improvements to the docs** — especially "I tried this and got stuck on X" stories that could go into [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
- **Tests.** This project doesn't have any yet because everything depends on hardware and a running SM2. PRs that mock those interfaces would be valuable.

## Issues

If something doesn't work for you, please open an issue with:

- Pi model and OS version
- Output of `cat config.yaml` with credentials redacted
- Output of `journalctl -u blink-sync.service -n 100 --no-pager`
- What you tried, what happened, what you expected

If you're unsure whether a behaviour is a bug or a config issue, file it anyway — the answer will help the docs.

## Pull requests

- Keep PRs focused — one feature or bug fix per PR
- Match the existing code style (it's just Python; we use ruff with the default config)
- Update docs when behaviour changes
- For new destinations, include an example config block in `config.example.yaml`

## Security

If you find a security issue (credential leak, privilege escalation, etc), please email rather than opening a public issue. See [README.md](README.md) for contact.

## Code of conduct

Be kind. We're all just trying to get our motion clips off a Sync Module 2.
