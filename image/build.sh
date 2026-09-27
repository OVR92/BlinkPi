#!/usr/bin/env bash
#
# build.sh — build a ready-to-flash BlinkPi SD card image with pi-gen.
#
# Produces Raspberry Pi OS Lite (64-bit, Trixie) with BlinkPi
# pre-installed, the USB gadget overlay enabled, and the setup page
# (bub-setup) running on port 80. On first boot with no network the Pi
# raises the "BlinkPi-Setup" hotspot; see image/README.md.
#
# Requirements on the build host (Linux, or WSL2 with Docker Desktop):
#   - Docker (used via pi-gen's build-docker.sh), OR
#   - a Debian/Ubuntu host with pi-gen's native deps (see pi-gen README)
#     and run with USE_DOCKER=0
#
# Usage:
#   ./image/build.sh                # docker build, output in image/deploy/
#   USE_DOCKER=0 sudo ./image/build.sh
#   WPA_COUNTRY=DE ./image/build.sh # pre-set the Wi-Fi regulatory domain
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$HERE/.." && pwd)"
WORK="${WORK:-$HERE/work}"
PIGEN_DIR="$WORK/pi-gen"
PIGEN_REPO="${PIGEN_REPO:-https://github.com/RPi-Distro/pi-gen.git}"
# pi-gen's "arm64" branch builds 64-bit trixie; 64-bit bookworm lives on
# "bookworm-arm64". Keep PIGEN_BRANCH and RELEASE in sync.
PIGEN_BRANCH="${PIGEN_BRANCH:-arm64}"
RELEASE="${RELEASE:-trixie}"
USE_DOCKER="${USE_DOCKER:-1}"
WPA_COUNTRY="${WPA_COUNTRY:-}"
IMG_NAME="${IMG_NAME:-BlinkPi}"

log() { printf "\033[1;34m[image]\033[0m %s\n" "$*"; }

mkdir -p "$WORK"
# Switching pi-gen branch (e.g. bookworm -> trixie): start from scratch.
# The old clone won't change branch on `git pull`, and with CONTINUE=1 the
# preserved docker container would reuse the previous release's rootfs.
if [[ -d "$PIGEN_DIR/.git" ]] && \
   [[ "$(git -C "$PIGEN_DIR" rev-parse --abbrev-ref HEAD)" != "$PIGEN_BRANCH" ]]; then
    log "pi-gen branch changed to $PIGEN_BRANCH; discarding old clone and build container"
    if [[ "$USE_DOCKER" == "1" ]]; then
        docker rm -v pigen_work >/dev/null 2>&1 || true
    fi
    rm -rf "$PIGEN_DIR"
fi
if [[ ! -d "$PIGEN_DIR/.git" ]]; then
    log "cloning pi-gen ($PIGEN_BRANCH)"
    git clone --depth 1 --branch "$PIGEN_BRANCH" "$PIGEN_REPO" "$PIGEN_DIR"
else
    log "updating pi-gen"
    git -C "$PIGEN_DIR" pull --ff-only || true
fi

# Our custom stage. Copied fresh each build so edits are picked up.
log "installing stage-blinkpi"
rm -rf "$PIGEN_DIR/stage-blinkpi"
cp -r "$HERE/stage-blinkpi" "$PIGEN_DIR/stage-blinkpi"
chmod +x "$PIGEN_DIR"/stage-blinkpi/prerun.sh "$PIGEN_DIR"/stage-blinkpi/*/*.sh

# Ship a copy of the project, including .git so the setup page's
# "Update BlinkPi" button can `git pull` later. Local config, images and
# runtime state are excluded.
SRC="$PIGEN_DIR/stage-blinkpi/01-blinkpi/files/BlinkPi"
rm -rf "$SRC"; mkdir -p "$SRC"
rsync -a --exclude .venv --exclude 'config.yaml' --exclude '*.img' \
      --exclude 'sync_state.json' --exclude 'setup_state.json' --exclude '*.log' \
      --exclude thumbnails --exclude image/work --exclude image/deploy \
      --exclude 'systemd/generated' --exclude '__pycache__' "$PROJECT_DIR/" "$SRC/"
if [[ -d "$SRC/.git" ]]; then
    # Make sure the image pulls from GitHub, whatever the build host's remote was.
    git -C "$SRC" remote set-url origin "${BLINKPI_REPO:-https://github.com/OVR92/BlinkPi.git}" 2>/dev/null || \
    git -C "$SRC" remote add origin "${BLINKPI_REPO:-https://github.com/OVR92/BlinkPi.git}"
else
    echo "warning: $PROJECT_DIR is not a git clone; the GUI update button will not work in this image" >&2
fi

# Don't export the plain Lite image from stage2, only ours.
touch "$PIGEN_DIR/stage2/SKIP_IMAGES"

log "writing pi-gen config"
cat > "$PIGEN_DIR/config" <<EOF
IMG_NAME="$IMG_NAME"
RELEASE=$RELEASE
DEPLOY_COMPRESSION=xz
TARGET_HOSTNAME=blinkpi
FIRST_USER_NAME=blink
FIRST_USER_PASS=blinkpi
DISABLE_FIRST_BOOT_USER_RENAME=1
ENABLE_SSH=1
LOCALE_DEFAULT=en_US.UTF-8
KEYBOARD_KEYMAP=us
KEYBOARD_LAYOUT="English (US)"
TIMEZONE_DEFAULT=UTC
STAGE_LIST="stage0 stage1 stage2 stage-blinkpi"
EOF
[[ -n "$WPA_COUNTRY" ]] && echo "WPA_COUNTRY=$WPA_COUNTRY" >> "$PIGEN_DIR/config"

cd "$PIGEN_DIR"
if [[ "$USE_DOCKER" == "1" ]]; then
    log "building with docker (this takes 30-90 minutes on first run)"
    CONTINUE=1 PRESERVE_CONTAINER=1 ./build-docker.sh
else
    log "building natively"
    ./build.sh
fi

mkdir -p "$HERE/deploy"
cp -v "$PIGEN_DIR"/deploy/*.xz "$HERE/deploy/" 2>/dev/null || cp -v "$PIGEN_DIR"/deploy/*.img "$HERE/deploy/"
log "done. Flash image/deploy/*.img.xz with Raspberry Pi Imager (no customisation needed)."
