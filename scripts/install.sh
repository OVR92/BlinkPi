#!/usr/bin/env bash
#
# install.sh — set up blink-usb-bridge from the project directory.
#
# What this does:
#   1. Sanity checks: root, config.yaml exists, dependencies installed
#   2. Creates a Python virtualenv in .venv/ and installs the package
#   3. Reads config.yaml and substitutes its values into systemd unit templates
#   4. Copies the generated units to /etc/systemd/system/
#   5. Sets up the boot-time USB gadget config (dwc2 overlay)
#   6. Creates the backing image if it doesn't exist yet
#   7. Adds an /etc/fstab entry for the SMB destination if enabled
#   8. Adds a sudoers entry so the unprivileged user can mount the image
#   9. (optional) Installs avahi-daemon for .local mDNS discovery
#  10. (optional) Installs the web UI service if web.enabled = true
#  11. Enables and starts the timers
#
# What this does NOT do:
#   - Set up rclone (run `rclone config` separately)
#   - Configure your Pi's USB-OTG cable, which side is host vs device, etc.
#   - Format the SM2's USB drive (let the SM2 do it via the Blink app)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${PROJECT_DIR}/config.yaml"
GENERATED="${PROJECT_DIR}/systemd/generated"

# ─────────────────────── helpers ───────────────────────

log()  { printf "\033[1;34m[install]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n"  "$*"; }
die()  { printf "\033[1;31m[error]\033[0m %s\n" "$*"; exit 1; }

read_yaml_value() {
    # Pull a single scalar from config.yaml using Python (no yq dependency).
    # Usage: read_yaml_value <dotted.key.path>
    python3 - "$1" <<'PY'
import sys, yaml
key = sys.argv[1]
with open("config.yaml") as f:
    data = yaml.safe_load(f)
for part in key.split("."):
    if data is None:
        sys.exit(0)
    data = data.get(part) if isinstance(data, dict) else None
if data is None:
    sys.exit(0)
print(data)
PY
}

substitute() {
    # Substitute @VAR@ placeholders in a template file. Reads pairs from stdin
    # as "VAR=value" lines and writes the substituted file to stdout.
    local template="$1"; shift
    python3 - "$template" "$@" <<'PY'
import sys, re
path, *pairs = sys.argv[1:]
mapping = {}
for p in pairs:
    k, _, v = p.partition("=")
    mapping[k] = v
text = open(path).read()
for k, v in mapping.items():
    text = text.replace(f"@{k}@", v)
sys.stdout.write(text)
PY
}

# ─────────────────────── pre-flight ───────────────────────

[[ $EUID -eq 0 ]] || die "must run as root: sudo ./scripts/install.sh"

[[ -f "$CONFIG" ]] || die "config.yaml not found. Copy config.example.yaml to config.yaml and edit it first."

for cmd in python3 modprobe systemctl mount; do
    command -v "$cmd" >/dev/null || die "required command not found: $cmd"
done

if ! python3 -c "import yaml" 2>/dev/null; then
    log "installing PyYAML system-wide for config parsing"
    apt-get update -qq && apt-get install -y python3-yaml || pip3 install --break-system-packages PyYAML
fi

# Read config values we need (from project dir so relative paths work).
cd "$PROJECT_DIR"

PI_USER=$(read_yaml_value "pi_user")
PROJECT_DIR_CFG=$(read_yaml_value "project_dir")
BACKING_IMAGE_PATH=$(read_yaml_value "backing_image_path")
BACKING_IMAGE_SIZE=$(read_yaml_value "backing_image_size")
MOUNT_POINT=$(read_yaml_value "mount_point")
POLL_INTERVAL=$(read_yaml_value "poll_interval_seconds")
WIPE_TIME=$(read_yaml_value "nightly_wipe_time")

# Compute prune time = wipe time + 30 min.
PRUNE_TIME=$(python3 -c "
import sys
h, m = map(int, '$WIPE_TIME'.split(':'))
m += 30
h = (h + m // 60) % 24
m %= 60
print(f'{h:02d}:{m:02d}')
")

SMB_ENABLED=$(read_yaml_value "destinations.smb.enabled" || true)
RCLONE_ENABLED=$(read_yaml_value "destinations.rclone.enabled" || true)
WEB_ENABLED=$(read_yaml_value "web.enabled" || true)

[[ "$PROJECT_DIR" == "$PROJECT_DIR_CFG" ]] || warn \
"config.yaml says project_dir=$PROJECT_DIR_CFG, but you're running from $PROJECT_DIR.
The generated units will reference $PROJECT_DIR_CFG. If that's not where the project
actually lives, edit config.yaml or rerun install from the right directory."

id -u "$PI_USER" >/dev/null 2>&1 || die "pi_user '$PI_USER' from config.yaml does not exist on this system"

# ─────────────────────── Python venv ───────────────────────

log "creating virtualenv at $PROJECT_DIR/.venv"
sudo -u "$PI_USER" python3 -m venv "$PROJECT_DIR/.venv"
sudo -u "$PI_USER" "$PROJECT_DIR/.venv/bin/pip" install --upgrade pip wheel

if [[ "$WEB_ENABLED" == "True" || "$WEB_ENABLED" == "true" ]]; then
    log "installing project with web extras"
    sudo -u "$PI_USER" "$PROJECT_DIR/.venv/bin/pip" install -e "$PROJECT_DIR[web]"
else
    sudo -u "$PI_USER" "$PROJECT_DIR/.venv/bin/pip" install -e "$PROJECT_DIR"
fi

# ─────────────────────── boot config ───────────────────────

CMDLINE=/boot/firmware/cmdline.txt
CONFIGTXT=/boot/firmware/config.txt
[[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
[[ -f $CONFIGTXT ]] || CONFIGTXT=/boot/config.txt

if ! grep -q "modules-load=dwc2" "$CMDLINE"; then
    log "adding modules-load=dwc2 to $CMDLINE"
    cp "$CMDLINE" "${CMDLINE}.bak.$(date +%s)"
    sed -i 's/$/ modules-load=dwc2/' "$CMDLINE"
fi

if ! grep -q "dtoverlay=dwc2,dr_mode=peripheral" "$CONFIGTXT"; then
    log "adding dwc2 overlay to $CONFIGTXT"
    cp "$CONFIGTXT" "${CONFIGTXT}.bak.$(date +%s)"
    cat >> "$CONFIGTXT" <<'EOF'

# blink-usb-bridge: Pi-as-USB-gadget mode for SM2
[all]
dtoverlay=dwc2,dr_mode=peripheral
EOF
fi

# ─────────────────────── backing image ───────────────────────

if [[ ! -f "$BACKING_IMAGE_PATH" ]]; then
    log "creating sparse backing image at $BACKING_IMAGE_PATH ($BACKING_IMAGE_SIZE)"
    mkdir -p "$(dirname "$BACKING_IMAGE_PATH")"
    truncate -s "$BACKING_IMAGE_SIZE" "$BACKING_IMAGE_PATH"
    chown "$PI_USER:$PI_USER" "$BACKING_IMAGE_PATH"
    log "backing image created. The SM2 will format it automatically when first attached."
else
    log "backing image already exists at $BACKING_IMAGE_PATH; leaving it alone"
fi

mkdir -p "$MOUNT_POINT"
chown "$PI_USER:$PI_USER" "$MOUNT_POINT"

# ─────────────────────── sudoers ───────────────────────

SUDOERS=/etc/sudoers.d/blink-usb-bridge
log "writing sudoers entry to $SUDOERS"
cat > "$SUDOERS" <<EOF
# Allow blink-usb-bridge to mount/unmount the backing image and drop caches
$PI_USER ALL=(root) NOPASSWD: /bin/mount -o ro\,loop\,offset=*\,noatime\,nodev\,nosuid\,noexec $BACKING_IMAGE_PATH $MOUNT_POINT
$PI_USER ALL=(root) NOPASSWD: /bin/umount $MOUNT_POINT
$PI_USER ALL=(root) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS" >/dev/null

# ─────────────────────── web UI prerequisites ───────────────────────

if [[ "$WEB_ENABLED" == "True" || "$WEB_ENABLED" == "true" ]]; then
    WEB_MOUNT=$(read_yaml_value "web.mount_point")
    if [[ -n "$WEB_MOUNT" ]]; then
        log "creating web UI mount point at $WEB_MOUNT"
        mkdir -p "$WEB_MOUNT"
        chown "$PI_USER:$PI_USER" "$WEB_MOUNT"

        # Append the web mount sudoers rule. The web service uses the
        # same sudo entry pattern as the sync service.
        WEB_SUDOERS=/etc/sudoers.d/blink-usb-bridge-web
        log "writing web sudoers entry to $WEB_SUDOERS"
        cat > "$WEB_SUDOERS" <<EOF
# Allow blink-usb-bridge web UI to mount/unmount its own copy of the backing image
$PI_USER ALL=(root) NOPASSWD: /bin/mount -o ro\,loop\,offset=*\,noatime\,nodev\,nosuid\,noexec $BACKING_IMAGE_PATH $WEB_MOUNT
$PI_USER ALL=(root) NOPASSWD: /bin/umount $WEB_MOUNT
EOF
        chmod 0440 "$WEB_SUDOERS"
        visudo -cf "$WEB_SUDOERS" >/dev/null
    fi

    if ! command -v avahi-daemon >/dev/null 2>&1; then
        log "installing avahi-daemon for .local mDNS discovery"
        apt-get install -y avahi-daemon avahi-utils
    fi
    systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
fi

# ─────────────────────── SMB fstab entry ───────────────────────

if [[ "$SMB_ENABLED" == "True" || "$SMB_ENABLED" == "true" ]]; then
    SMB_SERVER=$(read_yaml_value "destinations.smb.server")
    SMB_SHARE=$(read_yaml_value "destinations.smb.share")
    SMB_MOUNT=$(read_yaml_value "destinations.smb.mount_point")
    SMB_CREDS=$(read_yaml_value "destinations.smb.credentials_file")
    SMB_VERSION=$(read_yaml_value "destinations.smb.smb_version")

    if [[ ! -f "$SMB_CREDS" ]]; then
        warn "SMB credentials file $SMB_CREDS does not exist."
        warn "Create it before the first sync run. Format (mode 0600 root:root):"
        warn "    username=YOUR_USER"
        warn "    password=YOUR_PASS"
    fi

    mkdir -p "$SMB_MOUNT"

    UID_NUM=$(id -u "$PI_USER")
    GID_NUM=$(id -g "$PI_USER")
    FSTAB_ENTRY="//${SMB_SERVER}/${SMB_SHARE}  ${SMB_MOUNT}  cifs  noauto,x-systemd.automount,x-systemd.idle-timeout=300,credentials=${SMB_CREDS},vers=${SMB_VERSION},uid=${UID_NUM},gid=${GID_NUM},file_mode=0644,dir_mode=0755  0  0"
    if grep -qF "//${SMB_SERVER}/${SMB_SHARE}" /etc/fstab; then
        log "fstab already has an entry for //${SMB_SERVER}/${SMB_SHARE}; leaving alone"
    else
        log "adding SMB automount to /etc/fstab"
        cp /etc/fstab "/etc/fstab.bak.$(date +%s)"
        echo "$FSTAB_ENTRY" >> /etc/fstab
        systemctl daemon-reload
    fi
fi

# ─────────────────────── systemd units ───────────────────────

log "generating systemd units"
mkdir -p "$GENERATED"
chown "$PI_USER:$PI_USER" "$GENERATED"

generate() {
    local name="$1"; shift
    substitute "${PROJECT_DIR}/systemd/${name}.template" \
        "PI_USER=$PI_USER" \
        "PROJECT_DIR=$PROJECT_DIR" \
        "BACKING_IMAGE_PATH=$BACKING_IMAGE_PATH" \
        "POLL_INTERVAL_SECONDS=$POLL_INTERVAL" \
        "NIGHTLY_WIPE_TIME=$WIPE_TIME" \
        "PRUNE_TIME=$PRUNE_TIME" \
        > "${GENERATED}/${name}"
    install -m 0644 "${GENERATED}/${name}" "/etc/systemd/system/${name}"
}

generate blink-gadget.service
generate blink-sync.service
generate blink-sync.timer
generate blink-wipe.service
generate blink-wipe.timer
if [[ "$RCLONE_ENABLED" == "True" || "$RCLONE_ENABLED" == "true" ]]; then
    generate bub-prune.service
    generate bub-prune.timer
fi
if [[ "$WEB_ENABLED" == "True" || "$WEB_ENABLED" == "true" ]]; then
    generate bub-web.service
fi

systemctl daemon-reload

log "enabling units"
systemctl enable --now blink-gadget.service
systemctl enable --now blink-sync.timer
systemctl enable --now blink-wipe.timer
if [[ "$RCLONE_ENABLED" == "True" || "$RCLONE_ENABLED" == "true" ]]; then
    systemctl enable --now bub-prune.timer
fi
if [[ "$WEB_ENABLED" == "True" || "$WEB_ENABLED" == "true" ]]; then
    systemctl enable --now bub-web.service
fi

# ─────────────────────── done ───────────────────────

log "install complete."
log ""
log "Next steps:"
log "  1. Reboot the Pi if you weren't already in gadget mode:  sudo reboot"
log "  2. Plug the Pi's USB-OTG port into the SM2's USB port"
log "  3. Open the Blink app, you'll see a prompt to format the new USB drive — accept it"
log "  4. Trigger a motion event on a camera and watch:"
log "       journalctl -u blink-sync.service -f"
log ""
if [[ "$WEB_ENABLED" == "True" || "$WEB_ENABLED" == "true" ]]; then
    HOSTNAME_LOCAL=$(hostname).local
    WEB_PORT=$(read_yaml_value "web.listen_port")
    log "Web UI: http://${HOSTNAME_LOCAL}:${WEB_PORT:-8080}/"
    log ""
fi
if [[ "$RCLONE_ENABLED" == "True" || "$RCLONE_ENABLED" == "true" ]]; then
    log "rclone is enabled — make sure you've run 'rclone config' to set up the remote."
fi
