"""Backup orchestration."""

from __future__ import print_function

import json
import os
import shutil
import tempfile
import time

from . import __version__, FORMAT_NAME, FORMAT_VERSION
from .archive import BackupWriter, compression_extension
from .crypto import encryption_method, encryption_summary, encrypt_file, validate_encryption
from .database import describe_database, dump_database, db_is_configured
from .errors import DetectionError, EnvironmentError, IntegrityError, ProctorError
from .prosody import detect_prosody
from .provision import detect_webserver, letsencrypt_metadata
from .service import ServiceController
from .util import (
    ensure_dir, human_bytes, is_within, isoformat, random_suffix, run, sha256_file,
    timestamp_id, tree_size, which,
)


class BackupOptions(object):
    def __init__(self, **kwargs):
        self.live = kwargs.get("live", False)
        self.include_logs = kwargs.get("include_logs", False)
        self.include_system = kwargs.get("include_system", True)
        self.include_letsencrypt = kwargs.get("include_letsencrypt", "auto")
        self.compression = kwargs.get("compression", "gz")
        self.encrypt = kwargs.get("encrypt", None)
        self.verify_after = kwargs.get("verify_after", False)
        self.skip_database = kwargs.get("skip_database", False)
        self.skip_unreadable = kwargs.get("skip_unreadable", False)
        self.note = kwargs.get("note", "") or ""
        self.tags = list(kwargs.get("tags", []) or [])
        self.only = set(kwargs.get("only", []) or [])
        self.exclude = set(kwargs.get("exclude", []) or [])
        self.exclude_paths = [os.path.abspath(os.path.expanduser(item))
                              for item in (kwargs.get("exclude_paths", []) or [])]
        self.include_paths = [os.path.abspath(os.path.expanduser(item))
                              for item in (kwargs.get("include_paths", []) or [])]
        self.acquire_lock = kwargs.get("acquire_lock", True)
        self.handle_service = kwargs.get("handle_service", True)
        self.service_was_running = kwargs.get("service_was_running", None)

    def wants(self, component):
        if component in self.exclude:
            return False
        if self.only and component not in self.only:
            return False
        return True


def create_backup(config, repository, options, reporter):
    host = config.host
    overrides = {
        "config": config.prosody.get("config"),
        "data_dir": config.prosody.get("data_dir"),
        "log_dir": config.prosody.get("log_dir"),
        "cert_dirs": config.prosody.get("cert_dirs"),
        "plugin_dirs": config.prosody.get("plugin_dirs"),
        "user": config.prosody.get("user"),
        "group": config.prosody.get("group"),
        "service": config.prosody.get("service"),
    }
    install = detect_prosody(host, overrides=overrides, require=True)
    controller = ServiceController(
        host,
        name=config.prosody.get("service") or install.service_name,
        config={
            "manager": config.prosody.get("service_manager", "auto"),
            "commands": config.prosody.get("commands", {}),
        },
    )
    encrypt_requested = options.encrypt
    if encrypt_requested is None:
        encrypt_requested = bool(config.backup.get("encrypt", False))
    if encrypt_requested:
        validate_encryption(config.encryption, enabled=True)

    if options.acquire_lock:
        with repository.lock():
            return _create_backup_locked(config, repository, options, reporter, install,
                                         controller, encrypt_requested)
    return _create_backup_locked(config, repository, options, reporter, install,
                                 controller, encrypt_requested)


def _create_backup_locked(config, repository, options, reporter, install, controller,
                          encrypt_requested):
    host = config.host
    repository.ensure()
    repository.cleanup_tmp()
    started = time.time()
    id_value = "%s-%s" % (timestamp_id(), random_suffix(6))
    workdir = tempfile.mkdtemp(prefix="backup-", dir=repository.tmp_dir)
    os.chmod(workdir, 0o700)
    final_archive = None
    temporary_final = None
    published = False
    stopped_by_us = False
    was_running = options.service_was_running
    warnings = []
    for warning in install.warnings:
        if warning.startswith("log directory") and not options.include_logs:
            continue
        if warning.startswith("certificate directory") and not options.wants("certs"):
            continue
        warnings.append(warning)
    for path in options.include_paths:
        if not os.path.lexists(path):
            warnings.append("extra path does not exist: %s" % path)
    skipped = []
    writer = None

    try:
        roots, skip_prefixes = build_roots(install, options)
        total_source = 0
        for component, path in roots:
            try:
                total_source += tree_size(path, skip_prefixes=skip_prefixes)
            except OSError:
                pass
        free = _free_space(repository.path)
        min_free = int(config.backup.get("min_free_bytes", 0) or 0)
        if free is not None and min_free and free < min_free:
            raise EnvironmentError(
                "repository free space (%s) is below the configured minimum (%s)"
                % (human_bytes(free), human_bytes(min_free))
            )
        if free is not None and total_source and free < total_source:
            warnings.append(
                "repository free space (%s) is smaller than the source size (%s); "
                "the backup may fail" % (human_bytes(free), human_bytes(total_source))
            )

        if options.handle_service and not options.live:
            if controller.available:
                if was_running is None:
                    was_running = controller.is_active()
                if was_running is not False:
                    if was_running is None:
                        reporter.warn(
                            "cannot determine whether %s is running; stopping it anyway"
                            % install.service_name
                        )
                    reporter.step("Stopping %s for a consistent backup" % install.service_name)
                    if not controller.stop():
                        raise EnvironmentError(
                            "%s did not stop in time" % install.service_name,
                            hint="check the service, or use --live for an online backup",
                        )
                    stopped_by_us = True
                else:
                    reporter.info("%s is not running; continuing" % install.service_name)
            else:
                raise EnvironmentError(
                    "no supported service manager detected (%s)" % host.init,
                    hint="use --live for an online backup, or configure prosody.commands "
                         "stop/start in the proctor config",
                )
        elif options.live:
            if controller.available and controller.is_active():
                warnings.append(
                    "online backup requested while Prosody is running; files may change during "
                    "the copy (the SQLite dump itself is consistent)"
                )

        db_meta = None
        if not options.skip_database and db_is_configured(install.sql, install.storage):
            db_meta = dump_database(install, workdir, reporter)
            if db_meta:
                reporter.info("Database: %s" % describe_database(db_meta))
        elif options.skip_database:
            reporter.warn("database backup skipped by request")

        archive_name = id_value + compression_extension(options.compression)
        plain_path = os.path.join(workdir, archive_name)
        reporter.step("Writing backup archive")
        writer = BackupWriter(plain_path, compression=options.compression,
                              skip_unreadable=options.skip_unreadable or options.live)
        for component, path in roots:
            reporter.debug("adding %s: %s" % (component, path))
            writer.add_tree(path, component, skip_prefixes=skip_prefixes)
        warnings.extend(writer.warnings)
        skipped.extend(writer.skipped)

        if db_meta:
            digest = writer.add_file(
                os.path.join(workdir, os.path.basename(db_meta["dump"])),
                db_meta["dump"], component="database", mode=0o600,
            )
            db_meta["sha256"] = digest
            dump_path = os.path.join(workdir, os.path.basename(db_meta["dump"]))
            db_meta["size"] = os.path.getsize(dump_path)

        filesystem_users = install.user_accounts()
        filesystem_flat = _flatten_users(filesystem_users)
        db_users = set((db_meta or {}).get("users") or [])
        account_union = set(filesystem_flat) | db_users
        db_count = (db_meta or {}).get("user_count")
        if db_count is not None and db_count > len(db_users):
            # The database preview was truncated; keep the true count while
            # still including filesystem-only accounts.
            user_count = len(account_union) + (db_count - len(db_users))
        else:
            user_count = len(account_union)
        user_preview = sorted(account_union)[:200]

        environment = host.as_dict()
        environment["proctor_version"] = __version__
        environment["prosody"] = install.as_dict()
        environment["database"] = db_meta
        environment["service"] = {
            "name": install.service_name,
            "manager": controller.manager,
            "was_running": bool(was_running) if was_running is not None else None,
        }
        about = _prosodyctl_about(install)
        config_summary = install.config_summary()
        restore_text = _restore_readme(install, db_meta)
        try:
            web_info = detect_webserver(host)
            web_meta = web_info.as_dict() if web_info is not None else {"kind": "none"}
        except ProctorError as exc:
            web_meta = {"kind": "none", "detection_error": str(exc)}
            warnings.append("web server detection failed: %s" % exc)
        try:
            le_meta = letsencrypt_metadata()
        except ProctorError as exc:
            le_meta = {"present": False, "detection_error": str(exc)}
        if (web_meta.get("kind") in ("nginx", "apache")
                and install.http_interfaces
                and any(item in ("127.0.0.1", "::1", "localhost")
                        for item in install.http_interfaces)):
            web_meta["proxied"] = True
            if not web_meta.get("proxy_paths"):
                web_meta["proxy_paths"] = [
                    "/http-bind", "/xmpp-websocket", "/upload"
                ]
        writer.add_bytes("meta/environment.json",
                         (json.dumps(environment, indent=2, ensure_ascii=True) + "\n").encode("utf-8"),
                         component="meta")
        writer.add_bytes("meta/config-summary.json",
                         (json.dumps(config_summary, indent=2, ensure_ascii=True) + "\n").encode("utf-8"),
                         component="meta")
        if about:
            writer.add_bytes("meta/prosodyctl-about.txt", about.encode("utf-8", "replace"),
                             component="meta")
        writer.add_bytes("meta/RESTORE.txt", restore_text.encode("utf-8"), component="meta")

        encryption_info = encryption_summary(config.encryption, enabled=bool(encrypt_requested))
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "proctor_version": __version__,
            "id": id_value,
            "created_at": isoformat(),
            "source": {
                "hostname": host.hostname,
                "fqdn": host.fqdn,
                "os": host.as_dict(),
                "prosody_version": install.version,
                "prosody": install.config_summary(),
                "service": environment["service"],
                "webserver": web_meta,
                "letsencrypt": le_meta,
            },
            "targets": _targets_dict(roots),
            "components": writer.components,
            "entries": writer.entries,
            "extras": writer.extras,
            "database": db_meta,
            "users": {
                "count": user_count,
                "filesystem": filesystem_users,
                "preview": user_preview,
            },
            "options": {
                "compression": options.compression,
                "live": options.live,
                "include_logs": options.include_logs,
                "include_system": options.include_system,
                "include_letsencrypt": options.include_letsencrypt,
                "encrypted": bool(encrypt_requested),
                "encryption": encryption_info if encrypt_requested else {"enabled": False},
            },
            "note": options.note,
            "tags": options.tags,
            "warnings": warnings,
            "skipped": skipped,
            "stats": {
                "entries": len(writer.entries),
                "bytes": sum(item.get("size", 0) for item in writer.entries)
                         + sum(item.get("size", 0) for item in writer.extras),
                "duration_seconds": round(time.time() - started, 2),
                "user_count": user_count,
            },
        }
        manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
        writer.add_bytes("manifest.json", manifest_bytes, component="manifest", record=False)
        writer.close()
        writer = None

        suffix = ""
        if encrypt_requested:
            method = encryption_method(config.encryption)
            suffix = ".age" if method == "age" else ".gpg"
        final_name = archive_name + suffix
        final_archive = os.path.join(repository.backups_dir, final_name)
        ensure_dir(repository.backups_dir, mode=0o700)
        temporary_final = final_archive + ".part"
        if os.path.exists(temporary_final):
            os.unlink(temporary_final)
        if encrypt_requested:
            reporter.step("Encrypting backup with %s" % encryption_method(config.encryption))
            encrypt_file(plain_path, temporary_final, config.encryption)
            os.unlink(plain_path)
        else:
            shutil.move(plain_path, temporary_final)
        os.chmod(temporary_final, 0o600)
        os.replace(temporary_final, final_archive)
        published = True
        digest, size = sha256_file(final_archive)
        try:
            entry = repository.add(manifest, final_archive, digest, size=size,
                                   note=options.note, tags=options.tags)
        except Exception:
            for cleanup in (final_archive,
                            final_archive + ".sha256",
                            os.path.join(repository.backups_dir,
                                         id_value + SIDECAR_SUFFIX)):
                try:
                    if os.path.exists(cleanup):
                        os.unlink(cleanup)
                except OSError:
                    pass
            published = False
            raise
        if options.verify_after:
            result = repository.verify(entry, workdir=workdir,
                                       encryption=config.encryption, reporter=reporter)
            if result["errors"]:
                raise IntegrityError("backup verification failed: %s"
                                     % "; ".join(result["errors"]))
        repository.append_history("backup", {
            "id": entry["id"],
            "archive": entry["archive"],
            "sha256": entry["sha256"],
            "size": entry["size"],
            "live": options.live,
            "encrypted": entry.get("encrypted", False),
        }, limit=config.data.get("history_limit", 5000))
        reporter.success("Backup %s created (%s)" % (entry["id"], human_bytes(entry["size"])))
        if warnings:
            for warning in warnings:
                reporter.warn(warning)
        return {
            "entry": entry,
            "manifest": manifest,
            "warnings": warnings,
            "skipped": skipped,
            "repository": repository.path,
            "archive_path": final_archive,
        }
    except Exception:
        if not published and final_archive and os.path.exists(final_archive):
            try:
                os.unlink(final_archive)
            except OSError:
                pass
        raise
    finally:
        if writer is not None:
            writer.abort()
        if not published and temporary_final and os.path.exists(temporary_final):
            try:
                os.unlink(temporary_final)
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)
        if stopped_by_us:
            reporter.step("Restarting %s" % install.service_name)
            try:
                if not controller.start():
                    raise EnvironmentError(
                        "%s did not become active again" % install.service_name
                    )
            except Exception as exc:
                reporter.error("failed to restart %s: %s" % (install.service_name, exc))
                raise ProctorError(
                    "backup finished but %s could not be restarted: %s"
                    % (install.service_name, exc),
                    hint="start it with your service manager and check its logs",
                )


def build_roots(install, options):
    """Return ordered (component, path) roots and exclusion prefixes."""
    candidates = []
    if options.wants("config"):
        candidates.append(("config", install.config_dir))
    if options.wants("data"):
        candidates.append(("data", install.data_dir))
    letsencrypt = options.include_letsencrypt
    for directory in install.cert_dirs:
        real = os.path.realpath(directory)
        if is_within(directory, "/etc/letsencrypt") or is_within(real, "/etc/letsencrypt"):
            # Let's Encrypt's live/archive/renewal layout must be preserved so
            # renewal symlinks stay valid on the target.
            if letsencrypt == "never" or not options.wants("letsencrypt"):
                continue
            # The canonical Let's Encrypt roots are added below; adding the
            # symlinked directory itself would only archive the link.
        elif options.wants("certs"):
            candidates.append(("certs", directory))
    if options.wants("plugins"):
        for directory in install.plugin_dirs:
            candidates.append(("plugins", directory))
    auto_le = False
    if letsencrypt == "auto" and not install.letsencrypt_roots:
        try:
            auto_le = bool(letsencrypt_metadata().get("present"))
        except ProctorError:
            auto_le = False
    if letsencrypt == "always" or (letsencrypt == "auto"
                                   and (install.letsencrypt_roots or auto_le)):
        if options.wants("letsencrypt"):
            le_roots = install.letsencrypt_roots or _default_letsencrypt_roots()
            for directory in le_roots:
                candidates.append(("letsencrypt", directory))
    if options.include_logs and options.wants("logs"):
        candidates.append(("logs", install.log_dir))
    if options.include_system and options.wants("system"):
        for path in _system_paths(install):
            candidates.append(("system", path))
    if options.wants("extra"):
        for path in options.include_paths:
            candidates.append(("extra", path))

    skip_prefixes = []
    if not options.wants("certs"):
        skip_prefixes.extend(
            directory for directory in install.cert_dirs
            if not is_within(directory, "/etc/letsencrypt")
        )
    if not options.wants("letsencrypt"):
        skip_prefixes.extend(install.letsencrypt_roots)
    skip_prefixes.extend(options.exclude_paths)
    if (not options.skip_database and install.sql.get("engine") == "sqlite"
            and db_is_configured(install.sql, install.storage)
            and install.sql.get("path")):
        # The SQLite file inside data_path is dumped consistently via the
        # sqlite3 backup API; do not also copy a live, possibly stale copy.
        skip_prefixes.extend([
            install.sql["path"],
            install.sql["path"] + "-wal",
            install.sql["path"] + "-shm",
            install.sql["path"] + "-journal",
        ])

    forbidden_roots = {"/", "/etc", "/var", "/usr", "/home", "/root", "/boot",
                       "/dev", "/proc", "/sys"}
    accepted = []
    accepted_paths = []
    for component, path in candidates:
        if not path:
            continue
        path = os.path.abspath(path)
        if not os.path.lexists(path):
            continue
        if path in forbidden_roots:
            raise ProctorError(
                "refusing to back up an entire system directory: %s" % path,
                hint="set the real Prosody data/config path in the proctor config",
            )
        if os.path.islink(path) and component in ("config", "data", "plugins"):
            raise ProctorError(
                "%s path is a symlink: %s" % (component, path),
                hint="point proctor at the real directory (prosody.config, data_dir, "
                     "plugin_dirs) so its contents are backed up",
            )
        if (os.path.islink(path) and component == "certs"
                and not is_within(os.path.realpath(path), "/etc/letsencrypt")):
            raise ProctorError(
                "certificate directory is a symlink: %s" % path,
                hint="set cert_dirs to the real directory",
            )
        if any(is_within(path, existing) for existing in accepted_paths):
            continue
        accepted.append((component, path))
        accepted_paths.append(path)
    return accepted, skip_prefixes


def _default_letsencrypt_roots():
    return [
        "/etc/letsencrypt/live",
        "/etc/letsencrypt/archive",
        "/etc/letsencrypt/renewal",
        "/etc/letsencrypt/accounts",
        "/etc/letsencrypt/renewal-hooks",
        "/etc/letsencrypt/cli.ini",
        "/etc/letsencrypt/ssl-dhparams.pem",
    ]


def _system_paths(install):
    service = install.service_name or "prosody"
    paths = [
        "/etc/systemd/system/%s.service" % service,
        "/etc/systemd/system/%s.service.d" % service,
        "/etc/default/%s" % service,
        "/etc/sysconfig/%s" % service,
        "/etc/conf.d/%s" % service,
        "/etc/rc.conf.d/%s" % service,
        "/etc/cron.d/%s" % service,
        "/etc/logrotate.d/%s" % service,
    ]
    if install.config_dir.startswith("/usr/local"):
        paths.append("/usr/local/etc/rc.conf.d/%s" % service)
    return [path for path in paths if os.path.lexists(path)]


def _targets_dict(roots):
    result = {}
    for component, path in roots:
        result.setdefault(component, []).append(path)
    return result


def _flatten_users(users):
    result = []
    for host, names in sorted(users.items()):
        for name in names:
            result.append("%s@%s" % (name, host))
    return result


def _prosodyctl_about(install):
    if not which("prosodyctl"):
        return ""
    try:
        proc = run(["prosodyctl", "about"], check=False, timeout=30)
    except Exception:
        return ""
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace").strip()
    if len(output) > 100000:
        output = output[:100000] + "\n[truncated]\n"
    return output


def _free_space(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _restore_readme(install, db_meta):
    lines = [
        "Proctor backup for Prosody",
        "==========================",
        "",
        "Restore on this machine:",
        "  proctor restore latest --yes",
        "",
        "Restore/migrate onto a different machine:",
        "  proctor import /path/to/bundle.proctorbundle",
        "  proctor migrate <id> --yes --install-missing",
        "",
        "On a fresh host, --install-missing offers to install Prosody and the",
        "nginx/Apache + certbot setup recorded in this manifest.",
        "",
        "Source configuration:",
        "  config:    %s" % install.config_file,
        "  data:      %s" % install.data_dir,
        "  logs:      %s" % install.log_dir,
        "  storage:   %s" % install.storage,
        "  database:  %s" % describe_database(db_meta),
        "",
        "The manifest contains per-file SHA-256 checksums and ownership metadata.",
        "Always run 'proctor verify <id>' before restoring.",
        "This archive may contain credentials and private keys; keep it private.",
        "",
    ]
    return "\n".join(lines)
