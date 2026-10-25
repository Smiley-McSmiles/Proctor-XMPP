"""Preflight and health checks."""

from __future__ import print_function

import glob
import os
import shutil
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import __version__
from .crypto import validate_encryption, encryption_method
from .database import db_is_configured, describe_database
from .errors import ProctorError
from .prosody import detect_prosody
from .service import ServiceController, validate_prosody_config
from .util import disk_free, human_bytes, parse_iso, which

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"
CHECK_SKIP = "skip"


def run_doctor(config, repository, reporter, check_config=True):
    host = config.host
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    add("proctor", CHECK_OK, "version %s, Python %s" % (__version__, sys.version.split()[0]))
    if sys.version_info < (3, 8):
        add("python", CHECK_FAIL, "Python 3.8 or newer is required")
    add("platform", CHECK_OK, "%s (%s), init: %s" % (
        host.pretty_name or host.system, host.machine, host.init))

    install = None
    try:
        install = detect_prosody(host, overrides={
            "config": config.prosody.get("config"),
            "data_dir": config.prosody.get("data_dir"),
            "log_dir": config.prosody.get("log_dir"),
            "cert_dirs": config.prosody.get("cert_dirs"),
            "plugin_dirs": config.prosody.get("plugin_dirs"),
            "user": config.prosody.get("user"),
            "group": config.prosody.get("group"),
        }, require=False)
    except ProctorError as exc:
        add("prosody", CHECK_FAIL, str(exc))

    if install is None:
        add("prosody", CHECK_FAIL,
            "Prosody config not found; set prosody.config or pass --prosody-config")
    if install is not None:
        add("prosody", CHECK_OK, "%s (config %s)" % (
            install.version or "version unknown", install.config_file))
        if os.access(install.config_file, os.R_OK):
            add("config-readable", CHECK_OK, install.config_file)
            try:
                mode = os.stat(install.config_file).st_mode & 0o777
                if mode & 0o077:
                    add("config-perms", CHECK_WARN,
                        "config is readable by group/other (mode %04o)" % mode)
                else:
                    add("config-perms", CHECK_OK, "mode %04o" % mode)
            except OSError:
                pass
        else:
            add("config-readable", CHECK_FAIL, "cannot read %s" % install.config_file)
        if os.path.isdir(install.data_dir):
            add("data-dir", CHECK_OK, install.data_dir)
        else:
            add("data-dir", CHECK_FAIL, "missing data directory: %s" % install.data_dir)
        if os.path.isdir(install.log_dir):
            add("log-dir", CHECK_OK, install.log_dir)
        else:
            add("log-dir", CHECK_WARN, "missing log directory: %s (logs are optional)" % install.log_dir)
        missing_certs = [item for item in install.cert_dirs if not os.path.isdir(item)]
        if missing_certs:
            add("cert-dirs", CHECK_WARN, "missing: %s" % ", ".join(missing_certs))
        else:
            add("cert-dirs", CHECK_OK, ", ".join(install.cert_dirs) or "none")
        add("storage", CHECK_OK, "%s; database: %s" % (
            install.storage, describe_database(install.sql if db_is_configured(
                install.sql, install.storage) else None)))
        missing_modules = install.missing_modules()
        if missing_modules:
            add("modules", CHECK_WARN, "enabled but not found: %s" % ", ".join(missing_modules))
        else:
            add("modules", CHECK_OK, "all configured modules found")
        if check_config and which("prosodyctl"):
            status, output = validate_prosody_config(install.config_file)
            if status is True:
                add("config-check", CHECK_OK, "prosodyctl check passed")
            elif status is False:
                add("config-check", CHECK_FAIL, output or "prosodyctl check failed")
            else:
                add("config-check", CHECK_SKIP, output)
        for warning in install.warnings:
            if warning.startswith("log directory") and not config.backup.get("include_logs"):
                continue
            add("prosody-warning", CHECK_WARN, warning)
        _certificate_checks(install, add)
        if db_is_configured(install.sql, install.storage):
            _database_checks(install, add)
        _turn_checks(config, install, add)

    controller = ServiceController(
        host, name=config.prosody.get("service") or "prosody",
        config={"manager": config.prosody.get("service_manager", "auto"),
                "commands": config.prosody.get("commands", {})},
    )
    if controller.available:
        try:
            active = controller.is_active()
            state = "unknown" if active is None else ("running" if active else "stopped")
            add("service", CHECK_OK if active else CHECK_WARN,
                "%s is %s (manager: %s)" % (controller.name, state, controller.manager))
        except ProctorError as exc:
            add("service", CHECK_WARN, str(exc))
    else:
        add("service", CHECK_WARN,
            "no supported service manager; use --live or configure prosody.commands")

    if config.backup.get("encrypt"):
        try:
            validate_encryption(config.encryption, enabled=True)
            add("encryption", CHECK_OK, "%s configured" % encryption_method(config.encryption))
        except ProctorError as exc:
            add("encryption", CHECK_FAIL, str(exc))
    else:
        tool = which("age") or which("gpg")
        add("encryption", CHECK_OK if tool else CHECK_WARN,
            ("available: %s" % tool) if tool else "no encryption tool found (optional)")

    _repository_checks(config, repository, add)
    if install is not None:
        _disk_checks(config, install, add)
        if not os.geteuid() == 0:
            add("privileges", CHECK_WARN,
                "not running as root; ownership and some paths cannot be restored")
        else:
            add("privileges", CHECK_OK, "running as root")
    else:
        add("privileges", CHECK_OK if os.geteuid() == 0 else CHECK_WARN,
            "root" if os.geteuid() == 0 else "not root")

    failures = [item for item in checks if item["status"] == CHECK_FAIL]
    warnings = [item for item in checks if item["status"] == CHECK_WARN]
    result = {
        "ok": not failures,
        "checks": checks,
        "failures": len(failures),
        "warnings": len(warnings),
        "host": host.as_dict(),
        "prosody": install.as_dict() if install is not None else None,
    }
    if reporter:
        if failures:
            reporter.error("%d check(s) failed" % len(failures))
        elif warnings:
            reporter.warn("%d warning(s)" % len(warnings))
        else:
            reporter.success("all checks passed")
    return result


def _repository_checks(config, repository, add):
    path = repository.path
    if not os.path.isdir(path):
        add("repository", CHECK_WARN, "%s does not exist yet (created on first backup)" % path)
        return
    if not os.access(path, os.W_OK):
        add("repository", CHECK_FAIL, "%s is not writable" % path)
        return
    try:
        mode = os.stat(path).st_mode & 0o777
        add("repository", CHECK_OK, "%s (mode %04o)" % (path, mode))
        if mode & 0o077:
            add("repository-perms", CHECK_WARN,
                "repository is accessible by other users; chmod 700 %s" % path)
    except OSError as exc:
        add("repository", CHECK_WARN, str(exc))
    free = disk_free(path)
    if free is not None:
        status = CHECK_OK if free > 1024 ** 3 else CHECK_WARN
        add("repository-space", status, "%s free" % human_bytes(free))
    try:
        entries = repository.list_entries()
    except ProctorError as exc:
        add("index", CHECK_WARN, str(exc))
        return
    if not entries:
        add("backups", CHECK_WARN, "no backups yet")
        return
    latest = entries[0]
    age = None
    created = parse_iso(latest.get("created_at"))
    if created:
        age = (datetime.now(timezone.utc) - created).total_seconds()
    detail = "%d backup(s); latest %s" % (len(entries), latest.get("id"))
    if age is not None:
        detail += " (%s ago)" % _duration(age)
    status = CHECK_OK
    if age is None or age > 7 * 86400:
        status = CHECK_WARN
    add("backups", status, detail)


def _disk_checks(config, install, add):
    free = disk_free(config.repository)
    if free is None:
        return
    if free < 256 * 1024 ** 2:
        add("disk-space", CHECK_FAIL, "%s free in repository filesystem" % human_bytes(free))
    elif free < 1024 ** 3:
        add("disk-space", CHECK_WARN, "%s free in repository filesystem" % human_bytes(free))
    else:
        add("disk-space", CHECK_OK, "%s free in repository filesystem" % human_bytes(free))


def _database_checks(install, add):
    engine = install.sql.get("engine")
    if engine == "sqlite":
        path = install.sql.get("path")
        if os.path.isfile(path):
            add("database", CHECK_OK, "SQLite: %s" % path)
        else:
            add("database", CHECK_FAIL, "SQLite database missing: %s" % path)
    elif engine == "postgres":
        add("database", CHECK_OK if which("pg_dump") else CHECK_FAIL,
            "PostgreSQL; pg_dump %s" % ("available" if which("pg_dump") else "missing"))
        add("database-client", CHECK_OK if which("psql") else CHECK_WARN,
            "psql %s" % ("available" if which("psql") else "missing (needed to restore)"))
    elif engine == "mysql":
        add("database", CHECK_OK if which("mysqldump") else CHECK_FAIL,
            "MySQL; mysqldump %s" % ("available" if which("mysqldump") else "missing"))
        add("database-client", CHECK_OK if which("mysql") else CHECK_WARN,
            "mysql %s" % ("available" if which("mysql") else "missing (needed to restore)"))
    else:
        add("database", CHECK_FAIL, "unsupported SQL driver: %s" % install.sql.get("driver"))


def _certificate_checks(install, add):
    if not which("openssl"):
        add("certificates", CHECK_SKIP, "openssl not installed; expiry not checked")
        return
    candidates = list(install.parsed.get("certificate_files") or [])
    for directory in install.cert_dirs:
        for pattern in ("*.crt", "*.pem", "*.cer"):
            candidates.extend(glob.glob(os.path.join(directory, pattern)))
    candidates = sorted(set(
        item for item in candidates
        if os.path.isfile(item)
        and not os.path.basename(item).lower().startswith("privkey")
        and not item.lower().endswith(".key")))[:50]
    if not candidates:
        add("certificates", CHECK_WARN, "no certificate files found")
        return
    expiring = []
    invalid = []
    for path in candidates:
        expiry = _certificate_expiry(path)
        if expiry is None:
            invalid.append(path)
            continue
        days = (expiry - datetime.now(timezone.utc)).days
        if days < 30:
            expiring.append("%s (%d days)" % (os.path.basename(path), days))
    if invalid:
        add("certificates", CHECK_WARN, "could not parse: %s" % ", ".join(
            os.path.basename(item) for item in invalid[:5]))
    valid = len(candidates) - len(invalid)
    if not valid:
        add("certificate-expiry", CHECK_SKIP, "no parseable certificates")
    elif expiring:
        add("certificate-expiry", CHECK_WARN, "expiring soon: %s" % ", ".join(expiring))
    else:
        add("certificate-expiry", CHECK_OK, "%d certificate(s) valid for 30+ days" % valid)


def _turn_checks(config, install, add):
    """coturn / mod_turn_external health, only when TURN is in use."""
    try:
        from .turn import detect_coturn, turn_checks
    except ImportError:
        return
    turn = detect_coturn()
    enabled = getattr(install, "modules_enabled", []) or []
    if not turn.exists and "turn_external" not in enabled and not which("turnserver"):
        return
    for name, status, detail in turn_checks(config, install):
        add(name, status, detail)


def _certificate_expiry(path):
    from .util import run
    proc = run(["openssl", "x509", "-in", path, "-noout", "-enddate"], check=False)
    if proc.returncode != 0:
        return None
    output = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if "=" not in output:
        return None
    value = output.split("=", 1)[1]
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 3600:
        return "%dm" % max(1, seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)
