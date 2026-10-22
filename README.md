# weather-reporter

Daily weather report for Discord. Pulls the National Weather Service forecast, then posts a short, color-coded summary to a Discord webhook every morning, with warnings for rain tomorrow, a freeze tonight, and active NWS alerts.

Runs from cron in a small Proxmox LXC. One script to run (`weather_report.py`, with its code in `src/`), standard library only; no pip packages, no API keys.

## Requirements

- Debian or Ubuntu (setup uses `apt-get` to install `cron` if it's missing)
- Python 3.9 or newer (Debian 12 ships 3.11)
- Root access. It's meant for a dedicated container; see [Why it runs as root](#why-it-runs-as-root)
- Outbound HTTPS to `api.weather.gov` and `discord.com`, plus `geocoding-api.open-meteo.com` during setup
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

## Install

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

The report runs on the container's system clock. New LXCs often default to UTC. To use local time, set the time zone before setup, for example `timedatectl set-timezone America/New_York`, and check it with `date`.

### 2. Clone (inside the LXC, as root)

```bash
apt-get update && apt-get install -y git python3
git clone https://github.com/Chaos-man/weather-reporter.git /opt/weather-reporter
cd /opt/weather-reporter
```

### 3. Run setup

```bash
./weather_report.py
```

Setup asks four questions. Press Enter to keep the value shown in brackets:

| Question | Notes |
|---|---|
| Discord webhook URL | Channel Settings > Integrations > Webhooks |
| Location (City, ST) | For example `New York, NY`. Setup looks up the coordinates and asks you to confirm, or to pick from a list if there are several matches |
| Daily run time | 24-hour `HH:MM`, in the container's time zone |
| Contact email | Optional. Sent to NWS in the User-Agent header, which they ask for |

It then shows a summary and asks before saving. After saving, it checks the forecast from NWS and offers to send a test message.

Re-run `./weather_report.py` at any time to change settings. The current values are the defaults, so you only change what you need.

### What setup changes

| What | Where | Details |
|---|---|---|
| Config | `config.env` in the install folder | Owned by root, mode `600`. Git-ignored, so the webhook never reaches the repo |
| Cron job | `/etc/cron.d/<instance>` | Runs `weather_report.py --run` as root daily at the run time |
| Log | `/var/log/weather-reporter/<instance>.log` | Output of each cron run |
| Log rotation | `/etc/logrotate.d/<instance>` | Monthly, 6 months kept |
| Packages | system | Installs `cron` and `ca-certificates` only if missing |

`<instance>` is the install's folder name, so `/opt/weather-reporter` uses `/etc/cron.d/weather-reporter`. See [Multiple locations](#multiple-locations).

## Non-interactive setup

Every question has a flag, and anything given as a flag isn't asked. Add `--yes` to skip all questions. Missing values then come from the existing `config.env`, then from the defaults. This is useful for scripts and Ansible.

```bash
./weather_report.py --yes --webhook "https://discord.com/api/webhooks/..." \
  --location "New York, NY" --time 07:30 --contact you@example.com
```

| Flag | Sets |
|---|---|
| `--webhook URL` | Discord webhook |
| `--location "CITY, ST"` | Location, looked up automatically. With `--yes`, the best match is used |
| `--lat N --lon N` | Coordinates; skips the lookup. `--location` then only sets the display name |
| `--time HH:MM` | Daily run time |
| `--contact EMAIL` | NWS contact |
| `--rain-threshold PCT` | Rain-tomorrow warning (default 30) |
| `--rain-show-threshold PCT` | Minimum chance to show the 🌧️ line (default 10) |
| `--freeze-threshold F` | Freeze warning (default 32) |

The thresholds aren't asked during interactive setup. Set them with these flags, or by editing `config.env`.

> **Note:** a webhook passed with `--webhook` is saved in your shell history and is briefly visible in the process list. Interactive setup avoids both.

## Commands

| Command | What it does |
|---|---|
| `./weather_report.py` | Set up or reconfigure (interactive) |
| `./weather_report.py --run` | Fetch the forecast and post to Discord (what cron runs) |
| `./weather_report.py --dry-run` | Fetch the forecast and print the Discord payload; nothing is posted |
| `./weather_report.py --test` | Post to Discord with `[TEST]` in the title |
| `./weather_report.py --version` | Show the installed version |
| `./weather_report.py --help` | All options |

## Configuration

Setup writes `config.env`, and you can also edit it by hand. Threshold and location changes take effect on the next run. After changing `RUN_TIME`, re-run setup so the cron job is updated.

| Key | Default | Notes |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | none | Required |
| `LOCATION_NAME` | `New York, NY` | Shown in the title |
| `LATITUDE` / `LONGITUDE` | `40.7128` / `-74.0060` | Forecast point |
| `RUN_TIME` | `08:00` | 24-hour `HH:MM`, system time zone |
| `RAIN_THRESHOLD` | `30` | Percent; rain-tomorrow warning |
| `RAIN_SHOW_THRESHOLD` | `10` | Percent; minimum chance today or tonight to show the 🌧️ line |
| `FREEZE_THRESHOLD_F` | `32` | °F |
| `CONTACT` | none | Email or URL for the NWS User-Agent header |

Keep comments on their own lines. A comment after a value (`RAIN_THRESHOLD="30"  # note`) becomes part of the value.

"Today," "tonight," and "tomorrow" follow the container's local date. If the container runs in UTC and you're in a US time zone, an early run time can fall on the previous local evening.

## Why it runs as root

The cron job runs as root, and setup needs root to write `/etc/cron.d`. The script only makes outbound HTTPS requests and writes its log. Nothing listens on a port. In a dedicated, unprivileged LXC, container root is mapped to an unprivileged user on the Proxmox host, so a separate service account adds little.

If you install it on a shared host instead, keep in mind that the code in the install folder runs as root daily. Only root should be able to write to that folder, which is the default under `/opt`.

## Multiple locations

Each clone is a separate instance named after its folder, with its own `config.env`, cron job, log, and schedule. To add a second location or webhook:

```bash
git clone https://github.com/Chaos-man/weather-reporter.git /opt/weather-reporter2
cd /opt/weather-reporter2
./weather_report.py
```

| Folder | Instance name | Cron job |
|---|---|---|
| `/opt/weather-reporter` | `weather-reporter` | `/etc/cron.d/weather-reporter` |
| `/opt/weather-reporter2` | `weather-reporter2` | `/etc/cron.d/weather-reporter2` |
| `/opt/nyc` | `weather-reporter-nyc` | `/etc/cron.d/weather-reporter-nyc` |

- Folder names that don't start with `weather-reporter` get that prefix, so they can't overwrite unrelated cron jobs.
- Names are lowercased, and characters other than letters, numbers, `_`, and `-` become `-`.
- All instances log to `/var/log/weather-reporter/`, one file each.
- If two folders produce the same instance name (for example `/opt/weather-reporter` and `/srv/weather-reporter`), setup refuses to overwrite the other install's cron job. Rename the folder and re-run.
- Each clone is updated separately with `git pull`.

To remove an instance:

```bash
rm /etc/cron.d/<instance> /etc/logrotate.d/<instance>
rm -r /opt/<folder>
```

## Updating

```bash
cd /opt/weather-reporter
git pull
./weather_report.py
```

Re-running setup after an update makes sure the cron job matches the new version. Press Enter at each question to keep your settings.

### Upgrading from a version with `setup.sh`

Older versions used `setup.sh` and a `weather-reporter` service user. After `git pull`, run `./weather_report.py` once in each install folder. Setup:
- keeps your existing `config.env` values as the defaults
- rewrites the cron job to run as root with `--run`
- makes `config.env` root-only

Until you do, the old cron job fails with a "use --run" error instead of posting. Once no install uses the old user anymore, setup prints the command to remove it (`userdel weather-reporter`).

## Releases

Releases are listed on the repo's [Releases page](https://github.com/Chaos-man/weather-reporter/releases). To install a specific release instead of the latest `main`:

```bash
git clone --branch v1.0 https://github.com/Chaos-man/weather-reporter.git /opt/weather-reporter
```

To publish a new release:

1. Update `__version__` in `src/__init__.py` (for example `"1.1"`) and push that commit to `main`.
2. Tag the commit and push the tag:
   ```bash
   git tag v1.1 && git push origin v1.1
   ```

The Release workflow then runs the tests, checks that the tag matches `__version__`, and creates the GitHub release with notes generated from the commits since the previous release. If the tests fail or the version doesn't match, no release is created.

## Troubleshooting

- **Nothing posted:** check `tail /var/log/weather-reporter/<instance>.log`.
- **Ran at the wrong time:** run `date` in the container. Cron uses the system time zone.
- **`Discord returned HTTP 401/404`:** the webhook was deleted or regenerated. Re-run setup and enter the new one.
- **"NWS has no forecast for ..."** during setup: the location is outside NWS coverage (US and territories only).
- **NWS errors:** the API sometimes returns temporary 5xx errors. The script retries 3 times. Confirm the container has DNS and outbound HTTPS with `--dry-run`.
- **City lookup fails:** check that `geocoding-api.open-meteo.com` is reachable, or pass `--lat` and `--lon`.

## Credits

- Weather forecasts and alerts: [National Weather Service API](https://www.weather.gov/documentation/services-web-api) (NOAA). Free, no key required.
- Location search during setup: [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api), using location data from [GeoNames](https://www.geonames.org/).

## License

[MIT](LICENSE) © 2026 Sean McEnany
