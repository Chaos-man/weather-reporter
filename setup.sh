#!/usr/bin/env bash
# weather-reporter setup for a Debian/Ubuntu LXC.
# Safe to re-run: it overwrites the cron job and log rotation
# with whatever is currently in config.env.
#
# Usage (as root, from the repo directory):  ./setup.sh
#
# RUN_TIME is interpreted in the container's system time zone (check with: date).

set -euo pipefail

APP="weather-reporter"
APP_USER="weather-reporter"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$REPO_DIR/config.env"
SCRIPT="$REPO_DIR/weather_report.py"
CRON_FILE="/etc/cron.d/$APP"
LOG_DIR="/var/log/$APP"
LOG_FILE="$LOG_DIR/$APP.log"
LOGROTATE_FILE="/etc/logrotate.d/$APP"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo ./setup.sh)."
[[ -f "$SCRIPT" ]] || die "weather_report.py not found in $REPO_DIR."
[[ -f "$CONFIG" ]] || die "config.env not found. Run: cp config.env.example config.env && nano config.env"

# --- Load and validate config ------------------------------------------------
# shellcheck disable=SC1090
source "$CONFIG"

RUN_TIME="${RUN_TIME:-08:00}"
SYS_TZ="$(date +%Z)"

[[ "$RUN_TIME" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]] \
  || die "RUN_TIME '$RUN_TIME' is not a valid 24-hour HH:MM time."
HOUR=$((10#${BASH_REMATCH[1]}))
MINUTE=$((10#${BASH_REMATCH[2]}))

if [[ -z "${DISCORD_WEBHOOK_URL:-}" || "$DISCORD_WEBHOOK_URL" == *REPLACE_ME* ]]; then
  warn "DISCORD_WEBHOOK_URL is not set in config.env - the cron job will fail until it is."
fi

# --- Packages ----------------------------------------------------------------
PKGS=()
command -v python3 >/dev/null || PKGS+=(python3)
command -v cron    >/dev/null || PKGS+=(cron)
[[ -f /etc/ssl/certs/ca-certificates.crt ]] || PKGS+=(ca-certificates)
if ((${#PKGS[@]})); then
  info "Installing: ${PKGS[*]}"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PKGS[@]}"
fi

# --- Service user and permissions --------------------------------------------
if ! id "$APP_USER" >/dev/null 2>&1; then
  info "Creating system user $APP_USER"
  useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

chown root:"$APP_USER" "$CONFIG"
chmod 640 "$CONFIG"
chmod 755 "$SCRIPT"

if ! runuser -u "$APP_USER" -- test -r "$SCRIPT"; then
  die "$APP_USER cannot read $SCRIPT. Clone the repo somewhere world-readable, e.g. /opt/$APP (not /root)."
fi

install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$LOG_DIR"

# --- Cron job (overwritten every run) ----------------------------------------
info "Writing $CRON_FILE (daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE") $SYS_TZ)"
cat > "$CRON_FILE" <<EOF
# Managed by $REPO_DIR/setup.sh - edit config.env and re-run setup.sh instead.
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$MINUTE $HOUR * * * $APP_USER /usr/bin/python3 $SCRIPT >> $LOG_FILE 2>&1
EOF
chmod 644 "$CRON_FILE"

# --- Log rotation (overwritten every run) ------------------------------------
if [[ -d /etc/logrotate.d ]]; then
  cat > "$LOGROTATE_FILE" <<EOF
$LOG_FILE {
    monthly
    rotate 6
    compress
    missingok
    notifempty
    su $APP_USER $APP_USER
}
EOF
fi

# --- Make sure cron is enabled and running -----------------------------------
if command -v systemctl >/dev/null && systemctl list-unit-files cron.service >/dev/null 2>&1; then
  systemctl enable --now cron >/dev/null 2>&1 || true
  systemctl restart cron
else
  service cron restart
fi

# --- Verify NWS reachability (does not post to Discord) ----------------------
info "Dry run: fetching forecast from the NWS API..."
if runuser -u "$APP_USER" -- /usr/bin/python3 "$SCRIPT" --dry-run >/dev/null; then
  info "NWS API reachable and forecast parsed OK."
else
  warn "Dry run failed - check network/DNS from this container, then run: runuser -u $APP_USER -- python3 $SCRIPT --dry-run"
fi

echo
info "Setup complete."
echo "    Schedule : daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE") (system time: $SYS_TZ)"
echo "    Cron     : $CRON_FILE"
echo "    Log      : $LOG_FILE"
echo
echo "    Send a test message now:"
echo "      runuser -u $APP_USER -- python3 $SCRIPT --test"
