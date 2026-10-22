"""Install files for one copy of the app: config.env, the cron job, and log rotation."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from src.config import APP, CRON_DIR, LOG_DIR, LOGROTATE_DIR, SetupError, parse_run_time, render_config


def instance_name(repo_dir: Path) -> str:
    """Folder name -> cron-safe instance name (cron.d only runs [A-Za-z0-9_-] files)."""
    name = re.sub(r"[^a-z0-9_-]", "-", repo_dir.name.lower()).strip("-")
    if not name:
        raise SetupError(f"can't build an instance name from folder '{repo_dir}'")
    return name if name.startswith(APP) else f"{APP}-{name}"


def write_file(path: Path, content: str, mode: int) -> None:
    """Write via a temp file + rename so a failed write never leaves half a file."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    if os.geteuid() == 0:
        os.chown(tmp, 0, 0)
    os.replace(tmp, path)


MANAGED_RE = re.compile(r"^# Managed by (.+)/(?:setup\.sh|weather_report\.py) - ", re.MULTILINE)


def cron_owner(cron_file: Path):
    """Folder that wrote this cron file, or None if it isn't one of ours."""
    match = MANAGED_RE.search(cron_file.read_text()) if cron_file.exists() else None
    return Path(match.group(1)) if match else None


def render_cron(repo_dir: Path, script: Path, python: str, hour: int, minute: int, log_file: Path) -> str:
    return (
        f"# Managed by {repo_dir}/weather_report.py - re-run setup to change this file.\n"
        "SHELL=/bin/sh\n"
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f'{minute} {hour} * * * root "{python}" "{script}" --run >> "{log_file}" 2>&1\n'
    )


def render_logrotate(log_file: Path) -> str:
    return (
        f'"{log_file}" {{\n'
        "    monthly\n"
        "    rotate 6\n"
        "    compress\n"
        "    missingok\n"
        "    notifempty\n"
        "}\n"
    )


def install(values: dict, repo_dir: Path, script: Path, python: str,
            cron_dir: Path = CRON_DIR, logrotate_dir: Path = LOGROTATE_DIR,
            log_dir: Path = LOG_DIR) -> dict:
    """Write config.env, the cron job, and log rotation for this install.

    Only touches files; package and service handling is separate so this can be
    tested against temporary folders.
    """
    instance = instance_name(repo_dir)
    cron_file = cron_dir / instance
    log_file = log_dir / f"{instance}.log"
    for path in (repo_dir, script, log_file):
        if "%" in str(path) or "\n" in str(path) or '"' in str(path):
            raise SetupError(f"path '{path}' contains a character cron can't handle (%, quote, or newline)")

    owner = cron_owner(cron_file)
    if cron_file.exists() and owner != repo_dir:
        raise SetupError(
            f"{cron_file} already belongs to {owner or 'something else'}. "
            "Rename this folder and re-run setup."
        )

    hour, minute = parse_run_time(values["RUN_TIME"])
    write_file(repo_dir / "config.env", render_config(values), 0o600)

    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o755)
    if os.geteuid() == 0:
        os.chown(log_dir, 0, 0)  # older versions made this owned by a service user
    write_file(cron_file, render_cron(repo_dir, script, python, hour, minute, log_file), 0o644)

    logrotate_file = None
    if logrotate_dir.is_dir():
        logrotate_file = logrotate_dir / instance
        write_file(logrotate_file, render_logrotate(log_file), 0o644)

    return {"instance": instance, "cron_file": cron_file, "log_file": log_file,
            "logrotate_file": logrotate_file, "hour": hour, "minute": minute}


def run_cmd(cmd: list) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})


def ensure_packages() -> None:
    """Install cron and CA certificates if they're missing (Debian/Ubuntu)."""
    missing = []
    if not shutil.which("cron"):
        missing.append("cron")
    if not Path("/etc/ssl/certs/ca-certificates.crt").exists():
        missing.append("ca-certificates")
    if not missing:
        return
    if not shutil.which("apt-get"):
        raise SetupError(f"please install {' and '.join(missing)}, then re-run setup")
    print(f"==> Installing {' '.join(missing)}")
    run_cmd(["apt-get", "update", "-qq"])
    run_cmd(["apt-get", "install", "-y", "-qq", *missing])


def ensure_cron_running() -> None:
    if Path("/run/systemd/system").is_dir() and shutil.which("systemctl"):
        run_cmd(["systemctl", "enable", "--now", "cron"])
    elif shutil.which("service"):
        run_cmd(["service", "cron", "start"])


def old_user_note(cron_dir: Path = CRON_DIR) -> str:
    """Older versions ran as a 'weather-reporter' user. Suggest removing it once unused."""
    try:
        import pwd
        pwd.getpwnam(APP)
    except (ImportError, KeyError):
        return ""
    for cron_file in cron_dir.glob(f"{APP}*"):
        if re.search(rf"^\S+ \S+ \S+ \S+ \S+ {APP} ", cron_file.read_text(), re.MULTILINE):
            return ""  # another install still uses it
    return f"The '{APP}' user from older versions is no longer used. Remove it with: userdel {APP}"
