# weather-reporter

Daily weather report for Discord. Pulls the National Weather Service forecast, then posts a short, color-coded summary to a Discord webhook every morning, with warnings for rain tomorrow, a freeze tonight, and active NWS alerts.

Runs from cron in a small Proxmox LXC. Python standard library only; no pip packages, no API keys.

## Requirements

- Python 3.7 or newer, standard library only (Debian 12 ships 3.11). `setup.sh` installs `python3` if it's missing.
- Outbound HTTPS to `api.weather.gov` and `discord.com`
- A US location (the NWS API covers the US and its territories)

## What the report shows

| Line | Source |
|---|---|
| 🌡️ High / Low | Today's daytime high and tonight's low |
| 🌧️ Rain | Chance of precipitation today and tonight. Shown only when either is at or above `RAIN_SHOW_THRESHOLD` (default 10%) |
| ⛅ Conditions | NWS short forecast for today |
| 🚨 Alerts | Any active NWS alerts for the location |
| 🥶 Freeze tonight | Tonight's low is at or below `FREEZE_THRESHOLD_F` (default 32°F) |
| ☔ Rain tomorrow | Tomorrow's day or night period is at or above `RAIN_THRESHOLD` (default 30%) |

The embed's sidebar color reflects the most important condition:

| Color | Meaning |
|---|---|
| 🔴 Red | Active NWS alert |
| 🔵 Blue | Freeze tonight |
| 🟡 Yellow | Rain tomorrow |
| 🟢 Green | All clear |

## Setup

### 1. Create the LXC (on the Proxmox host)

Find a current Debian template and download it:

```bash
pveam update
pveam available --section system | grep debian
pveam download local debian-12-standard_<version>_amd64.tar.zst
```

Create and start a small unprivileged container. Adjust the ID, storage, and bridge for your environment:

```bash
pct create 120 local:vztmpl/debian-12-standard_<version>_amd64.tar.zst \
  --hostname weather-reporter \
  --unprivileged 1 \
  --cores 1 --memory 256 --swap 256 \
  --rootfs local-lvm:2 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --onboot 1 \
  --start 1
pct enter 120
```

### 2. Clone the repo (inside the LXC, as root)

```bash
apt-get update && apt-get install -y git
git clone https://github.com/Chaos-man/weather-reporter.git /opt/weather-reporter
cd /opt/weather-reporter
```

Clone to `/opt` (or another world-readable path), not `/root`. The job runs as an unprivileged user that needs to read the script.

### 3. Configure

```bash
cp config.env.example config.env
nano config.env
```

At minimum, set `DISCORD_WEBHOOK_URL`, your location (`LOCATION_NAME`, `LATITUDE`, `LONGITUDE`), and `CONTACT`.

`RUN_TIME` uses the container's system time zone. Check it with `date`. New LXCs often default to UTC, so `08:00` means 08:00 UTC unless you change the container's time zone (for example, `timedatectl set-timezone America/New_York`).

### 4. Run the setup script

```bash
./setup.sh
```

setup.sh makes these changes inside the container:

| What | Where | Details |
|---|---|---|
| Service user | `weather-reporter` | System user created with `useradd --system`: no home directory, `nologin` shell. The cron job runs as this user, not root. |
| Config permissions | `config.env` | Owned by `root:weather-reporter`, mode `640`, so only root and the service user can read the webhook URL. |
| Cron job | `/etc/cron.d/<instance>` | Runs daily at `RUN_TIME` |
| Log | `/var/log/weather-reporter/<instance>.log` | Owned by the service user |
| Log rotation | `/etc/logrotate.d/<instance>` | Monthly, 6 months kept |
| Packages | system | Installs `python3`, `cron`, and `ca-certificates` if missing |

It finishes with a dry run against the NWS API to confirm the container can reach it. Nothing is posted to Discord during setup.

`<instance>` is the install's folder name, so a clone in `/opt/weather-reporter` uses `/etc/cron.d/weather-reporter`. See [Multiple locations](#multiple-locations).

setup.sh is safe to re-run. Each run overwrites that install's cron job and log rotation with the current `config.env` values, and an existing service user is reused.

### 5. Test

```bash
runuser -u weather-reporter -- python3 /opt/weather-reporter/weather_report.py --test
```

## Configuration

All settings live in `config.env`. It is git-ignored, so the webhook URL never reaches the repo.

| Key | Default | Notes |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | none | Required |
| `LOCATION_NAME` | `New York, NY` | Shown in the title |
| `LATITUDE` / `LONGITUDE` | `40.7128` / `-74.0060` | Forecast point (US locations only; NWS coverage) |
| `RUN_TIME` | `08:00` | 24-hour `HH:MM`, system time zone |
| `RAIN_THRESHOLD` | `30` | Percent; rain-tomorrow warning |
| `RAIN_SHOW_THRESHOLD` | `10` | Percent; minimum chance today or tonight to show the 🌧️ line |
| `FREEZE_THRESHOLD_F` | `32` | °F |
| `CONTACT` | none | Email or URL for the NWS User-Agent header |

To change the schedule, edit `RUN_TIME` and re-run `./setup.sh`. Threshold and location changes take effect on the next run without re-running setup.

"Today," "tonight," and "tomorrow" are based on the container's local date. If the container runs in UTC and you're in a US time zone, an early `RUN_TIME` can fall on the previous local evening, so set `RUN_TIME` with that in mind or set the container's time zone.

## Multiple locations

Each clone is a separate instance named after its folder, with its own `config.env`, cron job, log, and schedule. To add a second location or webhook:

```bash
git clone https://github.com/Chaos-man/weather-reporter.git /opt/weather-reporter2
cd /opt/weather-reporter2
cp config.env.example config.env
nano config.env
./setup.sh
```

| Folder | Instance name | Cron job |
|---|---|---|
| `/opt/weather-reporter` | `weather-reporter` | `/etc/cron.d/weather-reporter` |
| `/opt/weather-reporter2` | `weather-reporter2` | `/etc/cron.d/weather-reporter2` |
| `/opt/nyc` | `weather-reporter-nyc` | `/etc/cron.d/weather-reporter-nyc` |

- Folder names that don't start with `weather-reporter` get that prefix, so they can't overwrite unrelated cron jobs.
- Names are lowercased, and characters other than letters, numbers, `_`, and `-` become `-`.
- All instances share the `weather-reporter` service user and the `/var/log/weather-reporter/` folder, with one log file each.
- If two folders produce the same instance name (for example `/opt/weather-reporter` and `/srv/weather-reporter`), setup.sh refuses to overwrite the other install's cron job. Rename the folder and re-run.
- Each clone is updated separately with `git pull`.

To remove an instance:

```bash
rm /etc/cron.d/<instance> /etc/logrotate.d/<instance>
rm -r /opt/<folder>
```

## Commands

| Command | What it does |
|---|---|
| `python3 weather_report.py` | Fetch the forecast and post to Discord (what cron runs) |
| `python3 weather_report.py --dry-run` | Fetch the forecast and print the Discord payload; nothing is posted |
| `python3 weather_report.py --test` | Post to Discord with `[TEST]` in the title |
| `./setup.sh` | Install or reconfigure. Safe to re-run |

## Updating

```bash
cd /opt/weather-reporter
git pull
./setup.sh
```

## Troubleshooting

- **Nothing posted:** check `tail /var/log/weather-reporter/<instance>.log`.
- **Ran at the wrong time:** run `date` in the container. Cron uses the system time zone.
- **`Discord returned HTTP 401/404`:** the webhook was deleted or regenerated. Update `DISCORD_WEBHOOK_URL`.
- **NWS errors:** the API sometimes returns temporary 5xx errors. The script retries 3 times. Confirm the container has DNS and outbound HTTPS with `--dry-run`.

## Data source

[National Weather Service API](https://www.weather.gov/documentation/services-web-api), free with no key. NWS asks clients to send a User-Agent with contact info, which is why `CONTACT` exists.

## License

[MIT](LICENSE) © 2026 Sean McEnany
