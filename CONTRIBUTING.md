# Contributing

Thanks for taking a look. This started as a personal project that turned out useful enough to share, so contributions are welcome.

## Useful contributions

The biggest gap right now is accessibility — the setup process requires SSH and manual config file editing, which is a barrier for most users. Contributions that close that gap are most welcome:

- **A GUI config page served at `blinkpi.local`** — a web interface for editing `config.yaml` without SSH. Ideally covers all fields (credentials, destinations, schedule) with validation and a save button that restarts the service.
- **A pre-built SD card image** — package the project so it can be written straight to an SD card and boot into a working state with no SSH required. Something like Pi Imager compatibility with first-boot Wi-Fi and credential prompts would make this accessible to non-technical users.

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
