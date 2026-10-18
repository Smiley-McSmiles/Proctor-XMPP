"""Install or remove a systemd timer / cron schedule for backups."""

from __future__ import print_function

import os
import shlex
import sys

from .config import default_config_path
from .errors import ConfigError, PermissionDenied
from .util import atomic_write, is_root, run, which

SERVICE_NAME = "proctor-backup"
SYSTEMD_DIR = "/etc/systemd/system"
DEFAULT_CRON = "/etc/cron.d/proctor"


def _proctor_command(config, live=False, explicit=None):
    if explicit:
        return explicit
    binary = which("proctor")
    if binary:
        command = [binary]
    else:
        command = [sys.executable, "-m", "proctor"]
    command.append("backup")
    command.append("--quiet")
    if live:
        command.append("--live")
    if config.path and os.path.abspath(config.path) != os.path.abspath(default_config_path(config.host)):
        command.extend(["--config", config.path])
    return shlex.join(command)


def _pythonpath(config):
    if which("proctor"):
        return None
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return package_root


def install_schedule(config, at="03:30", kind="auto", cron_file=None, live=False,
                     command=None, reporter=None):
    if not is_root():
        raise PermissionDenied("installing a system-wide schedule requires root")
    at = _validate_time(at)
    command = _proctor_command(config, live=live, explicit=command)
    systemd = kind in ("auto", "systemd") and config.host.init == "systemd" and which("systemctl")
    if kind == "systemd" and not systemd:
        raise ConfigError("systemd is not available on this host")
    if systemd:
        return _install_systemd(config, at, command, reporter)
    if kind == "systemd":
        raise ConfigError("systemd is not available on this host")
    return _install_cron(config, at, command, cron_file, reporter)


def _install_systemd(config, at, command, reporter):
    if not os.path.isdir(SYSTEMD_DIR):
        raise ConfigError("systemd unit directory not found: %s" % SYSTEMD_DIR)
    service_path = os.path.join(SYSTEMD_DIR, SERVICE_NAME + ".service")
    timer_path = os.path.join(SYSTEMD_DIR, SERVICE_NAME + ".timer")
    pythonpath = _pythonpath(config)
    service_lines = [
        "[Unit]",
        "Description=Proctor backup of the Prosody XMPP server",
        "Documentation=man:proctor(1)",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "Nice=10",
        "IOSchedulingClass=best-effort",
        "IOSchedulingPriority=7",
    ]
    if pythonpath:
        service_lines.append("Environment=PYTHONPATH=%s" % pythonpath)
    service_lines.extend([
        "ExecStart=%s" % command,
        "PrivateTmp=true",
        "",
    ])
    hour, minute = at.split(":")
    timer_lines = [
        "[Unit]",
        "Description=Daily Proctor backup timer",
        "",
        "[Timer]",
        "OnCalendar=*-*-* %s:%s:00" % (hour, minute),
        "Persistent=true",
        "RandomizedDelaySec=20m",
        "Unit=%s.service" % SERVICE_NAME,
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    atomic_write(service_path, ("\n".join(service_lines)).encode("utf-8"), mode=0o644)
    atomic_write(timer_path, ("\n".join(timer_lines)).encode("utf-8"), mode=0o644)
    run(["systemctl", "daemon-reload"], check=False)
    proc = run(["systemctl", "enable", "--now", SERVICE_NAME + ".timer"], check=False)
    if proc.returncode != 0:
        raise ConfigError("could not enable the timer: %s"
                          % (proc.stderr or b"").decode("utf-8", "replace"))
    if reporter:
        reporter.success("Installed systemd timer %s.timer (daily at %s)" % (SERVICE_NAME, at))
        reporter.info("Inspect with: systemctl list-timers %s.timer" % SERVICE_NAME)
    return {
        "kind": "systemd",
        "at": at,
        "service": service_path,
        "timer": timer_path,
        "command": command,
    }


def _install_cron(config, at, command, cron_file, reporter):
    cron_file = cron_file or DEFAULT_CRON
    if os.path.basename(cron_file) == "crontab" or cron_file in (
            "/etc/crontab", "/etc/crontab.local"):
        raise ConfigError(
            "refusing to overwrite a system crontab file: %s" % cron_file,
            hint="add the documented line to /etc/crontab manually, or use a "
                 "dedicated file under /etc/cron.d",
        )
    hour, minute = at.split(":")
    if not os.path.isdir("/etc/cron.d") and cron_file == DEFAULT_CRON:
        raise ConfigError(
            "/etc/cron.d is not available on this host",
            hint="pass --cron-file with a file your cron daemon reads "
                 "(for BSDs, add the line to /etc/crontab manually)",
        )
    directory = os.path.dirname(os.path.abspath(cron_file))
    if not os.path.isdir(directory):
        raise ConfigError("cron directory does not exist: %s" % directory)
    pythonpath = _pythonpath(config)
    if pythonpath:
        command = "PYTHONPATH=%s %s" % (shlex.quote(pythonpath), command)
    log_path = "/var/log/proctor-backup.log"
    line = ("%s %s * * * root %s >> %s 2>&1\n"
            % (minute, hour, command, shlex.quote(log_path)))
    content = (
        "# Managed by proctor: daily Prosody backup\n"
        "SHELL=/bin/sh\n"
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        "%s" % line
    )
    atomic_write(cron_file, content.encode("utf-8"), mode=0o644)
    if reporter:
        reporter.success("Installed cron schedule in %s (daily at %s)" % (cron_file, at))
    return {"kind": "cron", "at": at, "cron_file": cron_file, "command": command}


def remove_schedule(config, cron_file=None, reporter=None):
    cron_file = cron_file or DEFAULT_CRON
    if os.path.basename(cron_file) == "crontab" or cron_file in (
            "/etc/crontab", "/etc/crontab.local"):
        raise ConfigError(
            "refusing to edit a system crontab file: %s" % cron_file,
            hint="remove the Proctor line manually",
        )
    removed = []
    if is_root() and os.path.isdir(SYSTEMD_DIR):
        timer_path = os.path.join(SYSTEMD_DIR, SERVICE_NAME + ".timer")
        service_path = os.path.join(SYSTEMD_DIR, SERVICE_NAME + ".service")
        if os.path.exists(timer_path):
            run(["systemctl", "disable", "--now", SERVICE_NAME + ".timer"], check=False)
        for path in (timer_path, service_path):
            if os.path.exists(path):
                os.unlink(path)
                removed.append(path)
        if removed:
            run(["systemctl", "daemon-reload"], check=False)
    if os.path.exists(cron_file):
        os.unlink(cron_file)
        removed.append(cron_file)
    if reporter:
        if removed:
            reporter.success("Removed schedule: %s" % ", ".join(removed))
        else:
            reporter.info("No Proctor schedule found")
    return {"removed": removed}


def show_schedule(config, cron_file=None):
    timer_path = os.path.join(SYSTEMD_DIR, SERVICE_NAME + ".timer")
    service_path = os.path.join(SYSTEMD_DIR, SERVICE_NAME + ".service")
    result = {"systemd": {}, "cron": None}
    for label, path in (("timer", timer_path), ("service", service_path)):
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                result["systemd"][label] = {"path": path, "content": handle.read()}
    cron_file = cron_file or DEFAULT_CRON
    if os.path.isfile(cron_file):
        with open(cron_file, "r", encoding="utf-8", errors="replace") as handle:
            result["cron"] = {"path": cron_file, "content": handle.read()}
    return result


def _validate_time(value):
    parts = str(value).split(":")
    if len(parts) != 2:
        raise ConfigError("time must be HH:MM (24-hour), got %r" % value)
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ConfigError("time must be HH:MM (24-hour), got %r" % value)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError("time out of range: %s" % value)
    return "%02d:%02d" % (hour, minute)
