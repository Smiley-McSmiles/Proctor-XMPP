"""Restore and migration engine with path rewriting and ownership mapping."""

from __future__ import print_function

import copy
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import time

from . import __version__
from .archive import validate_member
from .crypto import is_encrypted, strip_encryption
from .database import describe_database, restore_database
from .errors import ConfigError, DetectionError, IntegrityError, PermissionDenied, ProctorError
from .prosody import detect_prosody
from .provision import (
    CERTBOT_ROOT, Prompter, ProvisionOptions, finalize_restore,
    grant_certificate_access, prepare_restore,
)
from .service import ServiceController, validate_prosody_config
from .util import (
    ensure_dir, gid_for, is_root, is_within, isoformat, sha256_bytes, uid_for,
)

TEXT_SUFFIXES = (".lua", ".cfg", ".conf", ".ini", ".json", ".yaml", ".yml", ".txt")
FORBIDDEN_DESTINATIONS = ("/", "/proc", "/sys", "/dev")


class RestoreOptions(object):
    def __init__(self, **kwargs):
        self.dry_run = kwargs.get("dry_run", False)
        self.verify = kwargs.get("verify", True)
        self.only = set(kwargs.get("only", []) or [])
        self.exclude = set(kwargs.get("exclude", []) or [])
        self.keep_current_config = kwargs.get("keep_current_config", False)
        self.skip_database = kwargs.get("skip_database", False)
        self.start = kwargs.get("start", None)
        self.safety_backup = kwargs.get("safety_backup", None)
        self.maps = dict(kwargs.get("maps", {}) or {})
        self.rewrite_config = kwargs.get("rewrite_config", True)
        self.no_overwrite = kwargs.get("no_overwrite", False)
        self.force = kwargs.get("force", False)
        self.mode = kwargs.get("mode", "restore")
        self.acquire_lock = kwargs.get("acquire_lock", True)
        self.install_missing = kwargs.get("install_missing", None)
        self.webserver = kwargs.get("webserver", "auto")
        self.certbot = kwargs.get("certbot", "auto")
        self.firewall = kwargs.get("firewall", "auto")
        self.email = kwargs.get("email")
        self.domains = list(kwargs.get("domains", []) or [])
        self.admin = kwargs.get("admin")
        self.proxy_http = kwargs.get("proxy_http", None)
        self.assume_yes = kwargs.get("assume_yes", False)


class TargetLayout(object):
    def __init__(self):
        self.config_file = None
        self.config_dir = None
        self.data_dir = None
        self.log_dir = None
        self.cert_dirs = []
        self.plugin_dirs = []
        self.letsencrypt_roots = []
        self.user = "prosody"
        self.group = "prosody"
        self.service_name = "prosody"
        self.storage = None
        self.sql = {}
        self.version = None
        self.virtualhosts = []
        self.components = []
        self.modules_enabled = []
        self.installed = False
        self.available_modules_list = []
        self.hostname = None

    def available_modules(self):
        return self.available_modules_list

    def as_dict(self):
        return {
            "config_file": self.config_file,
            "config_dir": self.config_dir,
            "data_dir": self.data_dir,
            "log_dir": self.log_dir,
            "cert_dirs": self.cert_dirs,
            "plugin_dirs": self.plugin_dirs,
            "user": self.user,
            "group": self.group,
            "service": self.service_name,
            "storage": self.storage,
            "sql": {key: value for key, value in (self.sql or {}).items() if key != "password"},
            "version": self.version,
            "installed": self.installed,
        }


class PathMapper(object):
    def __init__(self, mappings):
        # Longest source root first so nested mappings win.
        restored = {}
        for source, destination in mappings:
            if source and destination:
                restored[os.path.abspath(source)] = os.path.abspath(destination)
        self.mappings = sorted(restored.items(), key=lambda item: len(item[0]), reverse=True)

    def map(self, path):
        path = os.path.abspath(path)
        for source, destination in self.mappings:
            if path == source or is_within(path, source):
                relative = os.path.relpath(path, source)
                return os.path.normpath(os.path.join(destination, relative))
        return path

    def changed(self, path):
        return self.map(path) != os.path.abspath(path)

    def rewrite_bytes(self, data):
        # Use placeholders so a target path that contains an old prefix
        # (for example /var/lib/prosody -> /var/lib/prosody-new) is not
        # rewritten twice.
        placeholders = []
        for index, (source, destination) in enumerate(self.mappings):
            if source == destination:
                continue
            token = ("\x00PROCTOR_MAP_%d\x00" % index).encode("ascii")
            if source.encode("utf-8") in data:
                data = data.replace(source.encode("utf-8"), token)
                placeholders.append((token, destination.encode("utf-8")))
        for token, destination in placeholders:
            data = data.replace(token, destination)
        return data

    def map_symlink(self, target):
        if os.path.isabs(target):
            return self.map(target)
        return target

    def as_list(self):
        return [{"from": source, "to": destination} for source, destination in self.mappings]


def resolve_target(config, manifest, reporter):
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
    try:
        install = detect_prosody(host, overrides=overrides, require=False)
    except DetectionError:
        # An explicit --prosody-config may point at the file we are about to
        # restore onto a fresh host; treat it as a target path, not an error.
        install = None
    if install is not None:
        return install, True
    target = TargetLayout()
    defaults = host.defaults
    target.config_file = os.path.abspath(os.path.expanduser(
        overrides.get("config") or defaults.get("config_file")
    ))
    target.config_dir = os.path.dirname(target.config_file)
    target.data_dir = os.path.abspath(os.path.expanduser(
        overrides.get("data_dir") or defaults.get("data_dir")
    ))
    target.log_dir = os.path.abspath(os.path.expanduser(
        overrides.get("log_dir") or defaults.get("log_dir")
    ))
    target.cert_dirs = list(overrides.get("cert_dirs") or [os.path.join(target.config_dir, "certs")])
    target.plugin_dirs = list(overrides.get("plugin_dirs") or [])
    source = manifest.get("source", {}).get("prosody", {})
    target.letsencrypt_roots = list(source.get("letsencrypt_roots") or [])
    target.hostname = host.hostname
    target.user = overrides.get("user") or defaults.get("user") or "prosody"
    target.group = overrides.get("group") or defaults.get("group") or "prosody"
    target.service_name = overrides.get("service") or defaults.get("service") or "prosody"
    target.virtualhosts = list(source.get("virtualhosts") or [])
    target.components = list(source.get("components") or [])
    target.modules_enabled = list(source.get("modules_enabled") or [])
    target.storage = None
    target.sql = {}
    target.installed = False
    reporter.warn(
        "Prosody is not installed/configured on this host; files will be restored to "
        "OS defaults (%s, %s)" % (target.config_dir, target.data_dir)
    )
    return target, False


def build_mapper(manifest, target, custom_maps=None):
    targets = manifest.get("targets", {})
    mappings = []
    for component, roots in targets.items():
        for index, source in enumerate(roots):
            if component == "config":
                destination = target.config_dir
            elif component == "data":
                destination = target.data_dir
            elif component == "certs":
                if target.cert_dirs:
                    if index < len(target.cert_dirs):
                        destination = target.cert_dirs[index]
                    else:
                        destination = os.path.join(target.cert_dirs[0],
                                                   os.path.basename(source.rstrip("/")))
                else:
                    destination = os.path.join(target.config_dir, "certs")
            elif component == "plugins":
                if target.plugin_dirs:
                    destination = target.plugin_dirs[min(index, len(target.plugin_dirs) - 1)]
                else:
                    destination = source
            elif component == "logs":
                destination = target.log_dir
            elif component == "letsencrypt":
                destination = source
            elif component in ("system", "extra"):
                destination = source
            else:
                destination = source
            mappings.append((source, destination))
    source_prosody = manifest.get("source", {}).get("prosody", {}) or {}
    source_config = source_prosody.get("config_file")
    if source_config and target.config_file:
        mappings.append((source_config, target.config_file))
    # Even components excluded from the archive (logs, external certs) are
    # referenced by the restored config and must move to the target layout.
    source_log = source_prosody.get("log_dir")
    if source_log:
        mappings.append((source_log, target.log_dir))
    for index, source_cert in enumerate(source_prosody.get("cert_dirs") or []):
        if is_within(source_cert, "/etc/letsencrypt"):
            mappings.append((source_cert, source_cert))
        elif target.cert_dirs:
            if index < len(target.cert_dirs):
                destination = target.cert_dirs[index]
            else:
                destination = os.path.join(target.cert_dirs[0],
                                           os.path.basename(source_cert.rstrip("/")))
            mappings.append((source_cert, destination))
    if target.plugin_dirs:
        for index, source_plugin in enumerate(source_prosody.get("plugin_dirs") or []):
            destination = target.plugin_dirs[min(index, len(target.plugin_dirs) - 1)]
            mappings.append((source_plugin, destination))
    for source, destination in (custom_maps or {}).items():
        mappings.append((source, destination))
    return PathMapper(mappings)


def restore_backup(config, repository, reference, options, reporter):
    host = config.host
    entry = repository.get(reference)
    manifest = repository.manifest(entry)
    if manifest.get("format") != "proctor-backup":
        raise IntegrityError("not a Proctor backup: %s" % entry["id"])
    if options.mode not in ("restore", "migrate"):
        options.mode = "restore"

    if entry.get("encrypted") and not options.dry_run:
        _ensure_decryptable(config.encryption, entry, reporter)

    verify_result = None
    if options.verify:
        reporter.step("Verifying backup %s" % entry["id"])
        verify_result = repository.verify(entry, encryption=config.encryption, reporter=reporter)
        if verify_result["errors"]:
            raise IntegrityError(
                "backup verification failed: %s" % "; ".join(verify_result["errors"]),
                hint="the archive is corrupt; restore an older backup or copy it again",
            )

    prepared = None
    should_provision = (not options.only or "config" in options.only)
    if should_provision:
        provision_options = ProvisionOptions(
            install_missing=options.install_missing,
            webserver=options.webserver,
            certbot=options.certbot,
            firewall=options.firewall,
            email=options.email,
            domains=options.domains,
            admin=options.admin,
            proxy_http=options.proxy_http,
            dry_run=options.dry_run,
            assume_yes=bool(options.assume_yes and options.install_missing),
        )
        prompter = Prompter(assume_yes=bool(options.assume_yes and options.install_missing))
        prepared = prepare_restore(config, manifest, provision_options, reporter, prompter)
    target, installed = resolve_target(config, manifest, reporter)
    target.hostname = host.hostname
    mapper = build_mapper(manifest, target, options.maps)
    source_prosody = manifest.get("source", {}).get("prosody", {}) or {}
    source_cert_dirs = list(source_prosody.get("cert_dirs") or [])
    source_le_roots = list(source_prosody.get("letsencrypt_roots") or [])
    source_user = source_prosody.get("user") or "prosody"
    source_group = source_prosody.get("group") or source_user

    entries = manifest.get("entries", [])
    selected = []
    for item in entries:
        if not _selected(item, options, source_cert_dirs, source_le_roots):
            continue
        selected.append(item)

    db_meta = manifest.get("database")
    restore_db = bool(db_meta) and not options.skip_database and not _excluded("database", options)
    if options.only and "data" not in options.only and "database" not in options.only:
        restore_db = False
    db_compatibility = _database_compatibility(db_meta, target, installed)
    if restore_db and db_compatibility.get("error"):
        raise ProctorError(db_compatibility["error"], hint=db_compatibility.get("hint"))

    report = {
        "id": entry["id"],
        "mode": options.mode,
        "dry_run": options.dry_run,
        "source_hostname": manifest.get("source", {}).get("hostname"),
        "target_hostname": host.hostname,
        "source_os": manifest.get("source", {}).get("os", {}).get("pretty_name"),
        "target_os": host.pretty_name,
        "target": target.as_dict(),
        "path_map": mapper.as_list(),
        "selected_entries": len(selected),
        "database": {
            "backup": db_meta,
            "restore": restore_db,
            "compatibility": db_compatibility,
        },
        "verified": bool(verify_result and not verify_result["errors"]),
        "warnings": list(manifest.get("warnings") or []),
        "actions": [],
        "extracted": {"files": 0, "dirs": 0, "symlinks": 0, "bytes": 0, "skipped": 0},
        "rewritten_configs": [],
        "service": {
            "name": target.service_name,
            "manager": host.init,
            "was_running": None,
            "stopped": False,
            "started": False,
            "validated": None,
        },
        "safety_backup": None,
        "checklist": [],
        "webserver": None,
        "provisioning": _prepared_summary(prepared),
        "completed_at": None,
    }

    if options.dry_run:
        report["actions"] = _planned_actions(selected, mapper, options, source_cert_dirs)
        report["checklist"] = _migration_checklist(manifest, target, installed, db_meta)
        return report

    if options.acquire_lock:
        with repository.lock():
            _restore_locked(config, repository, entry, manifest, target, installed, mapper,
                            selected, db_meta, restore_db, options, reporter, report,
                            source_user, source_group, source_cert_dirs, prepared)
    else:
        _restore_locked(config, repository, entry, manifest, target, installed, mapper,
                        selected, db_meta, restore_db, options, reporter, report,
                        source_user, source_group, source_cert_dirs, prepared)
    return report


def _restore_locked(config, repository, entry, manifest, target, installed, mapper,
                    selected, db_meta, restore_db, options, reporter, report,
                    source_user, source_group, source_cert_dirs, prepared=None):
    host = config.host
    repository.ensure()
    controller = ServiceController(
        host,
        name=target.service_name,
        config={
            "manager": config.prosody.get("service_manager", "auto"),
            "commands": config.prosody.get("commands", {}),
        },
    )
    was_running = None
    stopped_by_us = False
    if controller.available:
        was_running = controller.is_active()
        report["service"]["was_running"] = was_running
        if was_running is not False:
            if was_running is None:
                report["warnings"].append(
                    "could not determine service state; stopping it before restore"
                )
            reporter.step("Stopping %s" % target.service_name)
            if not controller.stop():
                raise ProctorError(
                    "%s did not stop; refusing to modify a live server" % target.service_name,
                    hint="stop it manually or check the service configuration",
                )
            stopped_by_us = True
            report["service"]["stopped"] = True
    else:
        report["warnings"].append(
            "no supported service manager detected; make sure Prosody is stopped before restoring"
        )

    safety = options.safety_backup
    if safety is None:
        safety = bool(config.backup.get("safety_backup", True))
    if safety:
        try:
            safety_entry = _safety_backup(config, repository, entry, target, installed,
                                          reporter, report)
        except Exception:
            if stopped_by_us and controller.available:
                try:
                    controller.start()
                except Exception:
                    pass
            raise
        if safety_entry:
            report["safety_backup"] = safety_entry

    if is_root() and uid_for(target.user) is None:
        report["warnings"].append(
            "target user %r does not exist; ownership may not be mapped correctly"
            % target.user
        )
    if is_root() and gid_for(target.group) is None:
        report["warnings"].append(
            "target group %r does not exist; ownership may not be mapped correctly"
            % target.group
        )
    config_restored = (
        not options.keep_current_config
        and "config" not in options.exclude
        and (not options.only or "config" in options.only)
    )
    source_storage = (manifest.get("source", {}).get("prosody", {}) or {}).get("storage")
    if (installed and not config_restored and source_storage
            and target.storage and source_storage != target.storage):
        report["warnings"].append(
            "backup storage is %s but the target config uses %s; restored data may be unused"
            % (source_storage, target.storage)
        )

    workdir = tempfile.mkdtemp(prefix="restore-", dir=repository.tmp_dir)
    os.chmod(workdir, 0o700)
    try:
        counts, rewritten, db_dump = _extract(config, repository, entry, manifest, mapper,
                                              selected, options, target, source_user,
                                              source_group, source_cert_dirs, workdir,
                                              reporter, report)
        report["extracted"] = counts
        report["rewritten_configs"] = rewritten

        if restore_db:
            if not db_dump:
                raise IntegrityError("database metadata exists but the dump is missing from the archive")
            if installed and target.storage and target.storage != "sql":
                report["warnings"].append(
                    "the target does not use SQL storage; the restored database may be unused"
                )
            db_target = target
            if db_meta.get("engine") == "sqlite" and (config_restored or not installed):
                # The restored config is the source config with paths rewritten;
                # make the database land at the same mapped path the restored
                # config will use, even if the target had a different layout.
                source_db = db_meta.get("path")
                if source_db:
                    mapped_db = mapper.map(source_db)
                else:
                    mapped_db = os.path.join(target.data_dir, os.path.basename(
                        db_meta.get("dump") or "prosody.sqlite"
                    ))
                db_target = copy.copy(target)
                db_target.sql = dict(target.sql or {})
                db_target.sql.update({
                    "engine": "sqlite",
                    "driver": "SQLite3",
                    "configured": True,
                    "path": mapped_db,
                    "database": mapped_db,
                })
            if db_meta.get("engine") == "sqlite":
                db_path = (db_target.sql or {}).get("path")
                if db_path:
                    parent = os.path.dirname(os.path.abspath(db_path))
                    ensure_dir(parent, mode=0o755)
                    if is_root() and is_within(parent, target.data_dir):
                        uid = uid_for(target.user)
                        gid = gid_for(target.group)
                        if uid is not None and gid is not None:
                            try:
                                os.chown(parent, uid, gid)
                            except OSError:
                                pass
                        try:
                            os.chmod(parent, 0o750)
                        except OSError:
                            pass
            reporter.step("Restoring database: %s" % describe_database(db_meta))
            db_result = restore_database(
                db_meta, db_target, db_dump, workdir,
                reporter=reporter, dry_run=False,
                chown_callback=lambda path: _chown_database(path, target, manifest),
                target_data_dir=target.data_dir,
            )
            report["database"]["result"] = db_result
        elif db_meta:
            reporter.info("Database restore skipped")

        # Restored Certbot files keep their source modes. In particular,
        # /etc/letsencrypt is usually 0700 root:root while Prosody relies on
        # an ACL that is not portable through a tar archive. Repair access
        # before validating/starting Prosody, otherwise STARTTLS fails and the
        # service may not come up.
        if os.path.isdir(CERTBOT_ROOT):
            grant_certificate_access(target, reporter)

        # Provision the web server and any missing certificate before starting
        # Prosody. The restored config may reference a certificate that was
        # excluded from the archive and is only issued during finalisation.
        if prepared and not prepared.get("skipped"):
            web_result = finalize_restore(config, manifest, target, prepared, options,
                                          reporter, Prompter(
                                              assume_yes=bool(options.assume_yes
                                                              and options.install_missing)
                                          ))
            report["webserver"] = web_result

        if prepared and prepared.get("turn"):
            report["turn"] = _restore_turn(
                config, manifest, prepared, options, reporter,
                Prompter(assume_yes=bool(options.assume_yes
                                          and options.install_missing)),
                prepared.get("web"),
            )

        should_start = options.start
        if should_start is None:
            should_start = bool(was_running) or bool(
                prepared and prepared.get("prosody_new")
            )
        if should_start and not installed:
            report["warnings"].append(
                "Prosody is not installed/configured on this host; not starting the service"
            )
            should_start = False
        if should_start:
            config_ok = None
            if target.config_file and os.path.isfile(target.config_file):
                reporter.step("Validating restored Prosody configuration")
                config_ok, output = validate_prosody_config(target.config_file)
                report["service"]["validated"] = config_ok
                if config_ok is False:
                    report["warnings"].append(
                        "prosodyctl config check failed; service left stopped: %s" % output
                    )
                    reporter.error("Prosody config check failed; not starting the service")
                    should_start = False
            if should_start and controller.available:
                reporter.step("Starting %s" % target.service_name)
                try:
                    controller.start()
                    report["service"]["started"] = True
                except Exception as exc:
                    report["warnings"].append("failed to start %s: %s" % (target.service_name, exc))
                    reporter.error("failed to start %s: %s" % (target.service_name, exc))
            elif should_start:
                report["warnings"].append(
                    "service manager unavailable; start %s manually" % target.service_name
                )
        elif stopped_by_us:
            report["warnings"].append(
                "%s remains stopped; start it with your service manager when ready"
                % target.service_name
            )

        _post_restore_system_actions(host, target, reporter, report)
        report["checklist"] = _migration_checklist(manifest, target, installed, db_meta)
        report["completed_at"] = isoformat()
        repository.append_history(options.mode, {
            "id": entry["id"],
            "target_hostname": host.hostname,
            "files": counts["files"],
            "dirs": counts["dirs"],
            "database": restore_db,
            "service_started": report["service"]["started"],
        }, limit=config.data.get("history_limit", 5000))
        if options.mode == "migrate":
            reporter.success("Migration files restored from %s" % entry["id"])
        else:
            reporter.success("Restore completed from %s" % entry["id"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _restore_turn(config, manifest, prepared, options, reporter, prompter, web):
    """Reproduce or repair the coturn side of a migrated TURN setup."""
    from .turn import detect_coturn, setup_turn
    source_turn = (manifest.get("source") or {}).get("turn") or {}
    turn_options = copy.copy(options)
    turn_options.turn = "yes"
    turn_options.turn_secret = prepared.get("turn_secret") or turn_options.turn_secret
    turn = detect_coturn()
    if turn.exists and not getattr(turn_options, "force", False) \
            and not (options.assume_yes and options.install_missing):
        reporter.info("coturn is already configured; repairing access/firewall only")
    domains = prepared.get("domains") or []
    return setup_turn(
        config, turn_options, reporter, prompter, domains, web,
        prepared.get("certbot"),
        email=options.email,
        cert_name=prepared.get("cert_name"),
        realm=prepared.get("turn_realm"),
    )


def _prepared_summary(prepared):
    if not prepared:
        return None
    return {
        "prosody_installed": prepared.get("prosody_installed"),
        "prosody_new": prepared.get("prosody_new"),
        "web": prepared.get("web_dict"),
        "certbot": prepared.get("certbot"),
        "certbot_installed": prepared.get("certbot_installed"),
        "issue_certbot": prepared.get("issue_certbot"),
        "turn": prepared.get("turn"),
        "turn_realm": prepared.get("turn_realm"),
        "domains": prepared.get("domains"),
        "cert_name": prepared.get("cert_name"),
        "proxy": prepared.get("proxy"),
        "skipped": prepared.get("skipped"),
        "plan": prepared.get("plan"),
    }


def _selected(item, options, source_cert_dirs, source_le_roots):
    component = item.get("component")
    path = item.get("path", "")
    if "certs" in options.exclude and _is_cert_path(path, source_cert_dirs, source_le_roots):
        return False
    if component == "certs" or component == "letsencrypt":
        if "certs" in options.exclude or component in options.exclude:
            return False
    if component in options.exclude:
        return False
    if options.only and component not in options.only:
        if component == "letsencrypt" and "certs" in options.only:
            pass
        elif not (component == "config" and "certs" in options.only
                  and _is_cert_path(path, source_cert_dirs, source_le_roots)):
            return False
    if options.keep_current_config and component == "config" and not _is_cert_path(
            path, source_cert_dirs, source_le_roots):
        return False
    return True


def _excluded(component, options):
    return component in options.exclude


def _is_cert_path(path, source_cert_dirs, source_le_roots):
    return any(is_within(path, root) for root in source_cert_dirs + source_le_roots)


def _planned_actions(entries, mapper, options, source_cert_dirs):
    actions = []
    for item in entries[:200]:
        if options.keep_current_config and item.get("component") == "config":
            continue
        actions.append({
            "source": item.get("path"),
            "destination": mapper.map(item.get("path")),
            "type": item.get("type"),
            "component": item.get("component"),
        })
    return actions


def _extract(config, repository, entry, manifest, mapper, selected, options, target,
             source_user, source_group, source_cert_dirs, workdir, reporter, report):
    entries_by_arc = {item["arcname"]: item for item in manifest.get("entries", [])}
    extras_by_arc = {item["arcname"]: item for item in manifest.get("extras", [])}
    counts = {"files": 0, "dirs": 0, "symlinks": 0, "bytes": 0, "skipped": 0}
    rewritten = []
    dirs_to_finalize = []
    db_paths = {}
    root = is_root()
    warned_owner = [False]
    selected_arcs = {item["arcname"] for item in selected}
    source_cert_dirs = list(source_cert_dirs or [])
    le_roots = list(manifest.get("source", {}).get("prosody", {}).get("letsencrypt_roots") or [])

    seen_members = set()
    with repository.open_archive(entry, workdir=workdir, encryption=config.encryption) as tar:
        for member in tar:
            name = validate_member(member)
            if name in seen_members:
                raise IntegrityError("archive contains a duplicate member: %s" % name)
            seen_members.add(name)
            if name == "manifest.json" or name.startswith("meta/"):
                continue
            if name.startswith("db/"):
                if report["database"]["restore"]:
                    expected = extras_by_arc.get(name)
                    destination = os.path.join(workdir, os.path.basename(name))
                    _extract_member(tar, member, destination, expected, 0o600)
                    db_paths[name] = destination
                continue
            item = entries_by_arc.get(name)
            if item is None:
                raise IntegrityError("archive member has no manifest entry: %s" % name)
            if not os.path.isabs(str(item.get("path") or "")):
                raise IntegrityError(
                    "manifest contains a non-absolute source path: %r" % item.get("path")
                )
            if name not in selected_arcs:
                counts["skipped"] += 1
                continue
            component = item.get("component")
            if component == "config" and options.keep_current_config and not _is_cert_path(
                    item["path"], source_cert_dirs, le_roots):
                counts["skipped"] += 1
                continue
            destination = mapper.map(item["path"])
            _validate_destination(destination)
            _check_parent_chain(destination, options.force)
            if options.no_overwrite and os.path.lexists(destination):
                counts["skipped"] += 1
                continue
            if member.isdir():
                if os.path.islink(destination):
                    if not options.force:
                        raise ProctorError(
                            "refusing to write through symlink: %s" % destination,
                            hint="pass --force only if this symlink is intentional",
                        )
                    os.unlink(destination)
                if os.path.lexists(destination) and not os.path.isdir(destination):
                    if os.path.isfile(destination):
                        os.unlink(destination)
                    else:
                        raise ProctorError("cannot replace non-directory with directory: %s"
                                           % destination)
                ensure_dir(destination, mode=0o755)
                counts["dirs"] += 1
                dirs_to_finalize.append((destination, item))
                continue
            ensure_dir(os.path.dirname(destination), mode=0o755)
            if member.isfile():
                expected = entry_hash(item)
                transform = None
                if options.rewrite_config and _should_rewrite(item, source_cert_dirs, le_roots):
                    transform = mapper.rewrite_bytes
                actual = _extract_member(tar, member, destination, expected, item.get("mode"),
                                         transform=transform)
                if transform is not None:
                    rewritten.append(destination)
                counts["files"] += 1
                counts["bytes"] += actual
                _apply_metadata(destination, item, target, manifest, source_user,
                                source_group, root, warned_owner)
            elif member.issym():
                target_path = mapper.map_symlink(item.get("linkname") or member.linkname)
                if os.path.lexists(destination):
                    if os.path.isdir(destination):
                        raise ProctorError(
                            "cannot replace directory with symlink: %s" % destination,
                            hint="move it away manually, then retry",
                        )
                    os.unlink(destination)
                os.symlink(target_path, destination)
                counts["symlinks"] += 1
                _apply_metadata(destination, item, target, manifest, source_user,
                                source_group, root, warned_owner, symlink=True)
            else:
                counts["skipped"] += 1

    for destination, item in sorted(dirs_to_finalize, key=lambda pair: len(pair[0]), reverse=True):
        _apply_metadata(destination, item, target, manifest, source_user, source_group,
                        root, warned_owner, directory=True)

    dump_path = None
    if db_paths:
        dump_path = list(db_paths.values())[0]
    return counts, rewritten, dump_path


def entry_hash(item):
    return (item.get("sha256"), item.get("size"))


def _extract_member(tar, member, destination, expected, mode, transform=None):
    if isinstance(expected, dict):
        expected_hash = expected.get("sha256")
        expected_size = expected.get("size")
    elif expected:
        expected_hash, expected_size = expected
    else:
        expected_hash, expected_size = None, None
    parent = os.path.dirname(destination)
    ensure_dir(parent, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=".proctor-", dir=parent)
    digest = hashlib.sha256()
    size = 0
    try:
        source = tar.extractfile(member)
        if source is None:
            raise IntegrityError("cannot read member: %s" % member.name)
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if expected_hash and digest.hexdigest() != expected_hash:
            raise IntegrityError("checksum mismatch while extracting: %s" % member.name)
        if expected_size is not None and size != expected_size:
            raise IntegrityError("size mismatch while extracting: %s" % member.name)
        if transform is not None:
            with open(temporary, "rb") as handle:
                data = handle.read()
            if b"\x00" not in data:
                rewritten = transform(data)
                if rewritten != data:
                    with open(temporary, "wb") as handle:
                        handle.write(rewritten)
                        handle.flush()
                        os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(mode if mode is not None else 0o600))
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return size


def _should_rewrite(item, source_cert_dirs, le_roots):
    component = item.get("component")
    if component not in ("config", "plugins"):
        return False
    path = item.get("path", "")
    if _is_cert_path(path, source_cert_dirs, le_roots):
        return False
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in TEXT_SUFFIXES:
        return False
    return item.get("size", 0) < 10 * 1024 * 1024


def _apply_metadata(path, item, target, manifest, source_user, source_group, root,
                    warned_owner, symlink=False, directory=False):
    mode = item.get("mode")
    if mode is not None and not symlink:
        try:
            os.chmod(path, stat.S_IMODE(mode))
        except OSError:
            pass
    if root:
        uid = _mapped_uid(item, target, manifest, source_user)
        gid = _mapped_gid(item, target, manifest, source_group)
        try:
            if symlink:
                os.lchown(path, uid, gid)
            else:
                os.chown(path, uid, gid)
        except OSError as exc:
            if not warned_owner[0]:
                warned_owner[0] = True
                # Metadata problems should not destroy an otherwise good restore.
                sys.stderr.write("warning: could not set ownership on %s: %s\n" % (path, exc))
    elif not warned_owner[0]:
        warned_owner[0] = True
        sys.stderr.write("warning: not running as root; ownership will not be restored\n")
    mtime = item.get("mtime")
    if mtime:
        try:
            os.utime(path, (mtime, mtime), follow_symlinks=not symlink)
        except (OSError, NotImplementedError):
            pass


def _mapped_uid(item, target, manifest, source_user):
    name = item.get("uname")
    if name and source_user and name == source_user:
        value = uid_for(target.user)
        if value is not None:
            return value
    if name:
        value = uid_for(name)
        if value is not None:
            return value
    return item.get("uid", 0)


def _mapped_gid(item, target, manifest, source_group):
    name = item.get("gname")
    if name and source_group and name == source_group:
        value = gid_for(target.group)
        if value is not None:
            return value
    if name:
        value = gid_for(name)
        if value is not None:
            return value
    return item.get("gid", 0)


def _validate_destination(path):
    path = os.path.abspath(path)
    for forbidden in FORBIDDEN_DESTINATIONS:
        if path == forbidden or (forbidden != "/" and is_within(path, forbidden)):
            raise ProctorError("refusing to restore into %s" % path)
    if not os.path.isabs(path):
        raise ProctorError("restore destination must be absolute: %s" % path)


def _check_parent_chain(path, force=False):
    """Refuse to write through an existing symlinked parent.

    A backup must not be able to redirect writes outside the intended restore
    tree by introducing a symlink before a file member.
    """
    parent = os.path.dirname(os.path.abspath(path))
    while parent and parent != os.path.dirname(parent):
        if os.path.islink(parent):
            if not force:
                raise ProctorError(
                    "refusing to write through symlink: %s" % parent,
                    hint="move the symlink or pass --force if it is intentional",
                )
            return
        parent = os.path.dirname(parent)


def _chown_database(path, target, manifest):
    if not is_root():
        return
    uid = uid_for(target.user)
    gid = gid_for(target.group)
    if uid is not None and gid is not None:
        try:
            os.chown(path, uid, gid)
        except OSError:
            pass


def _safety_backup(config, repository, entry, target, installed, reporter, report):
    if not installed or not target.config_file or not os.path.isfile(target.config_file):
        report["warnings"].append(
            "no existing Prosody configuration on the target; skipping automatic safety backup"
        )
        return None
    reporter.step("Creating a safety backup of the current installation")
    from .backup import BackupOptions, create_backup
    try:
        result = create_backup(
            config,
            repository,
            BackupOptions(
                live=True,
                include_logs=False,
                tags=["pre-restore", "protected"],
                note="Automatic safety backup before restoring %s" % entry["id"],
                acquire_lock=False,
                handle_service=False,
                service_was_running=False,
                verify_after=False,
            ),
            reporter,
        )
    except Exception as exc:
        raise ProctorError(
            "safety backup failed; refusing to overwrite the current installation: %s" % exc,
            hint="fix the problem, or pass --no-safety-backup to proceed without one",
        )
    report["warnings"].extend(result.get("warnings") or [])
    return result["entry"]["id"]


def _database_compatibility(db_meta, target, installed):
    if not db_meta:
        return {"ok": True, "message": "backup has no database"}
    if not installed:
        if db_meta.get("engine") != "sqlite":
            return {
                "ok": False,
                "error": "this backup contains a %s database, but Prosody is not configured "
                         "on the target" % db_meta.get("engine"),
                "hint": "install and configure Prosody with the same database backend first",
            }
        return {"ok": True, "message": "SQLite will be restored under the target data directory"}
    target_engine = (target.sql or {}).get("engine")
    if target_engine is None:
        if db_meta.get("engine") == "sqlite":
            return {"ok": True, "message": "SQLite will be restored to the default path"}
        return {
            "ok": False,
            "error": "target Prosody has no SQL configuration but the backup uses %s"
                     % db_meta.get("engine"),
            "hint": "configure storage = \"sql\" and the matching sql block first",
        }
    if target_engine != db_meta.get("engine"):
        return {
            "ok": False,
            "error": "database backend mismatch: backup is %s, target is %s"
                     % (db_meta.get("engine"), target_engine),
            "hint": "restore onto the same backend, or convert with Prosody's prosody-migrator",
        }
    return {"ok": True, "message": "database backends match"}


def _post_restore_system_actions(host, target, reporter, report):
    """Relabel SELinux contexts and reload systemd units when appropriate."""
    if host.init == "systemd":
        from .util import which
        if which("systemctl"):
            from .util import run
            run(["systemctl", "daemon-reload"], check=False)
    if not is_root() or not os.path.isdir("/sys/fs/selinux"):
        return
    from .util import run, which
    if not which("restorecon"):
        report["warnings"].append(
            "SELinux is enabled but restorecon is not installed; file contexts may be wrong"
        )
        return
    paths = []
    for candidate in (["/etc/letsencrypt", "/etc/nginx", "/etc/httpd", "/etc/apache2",
                       target.config_dir, target.data_dir, target.log_dir]
                      + list(target.cert_dirs or []) + list(target.plugin_dirs or [])):
        if candidate and os.path.exists(candidate) and candidate not in paths:
            paths.append(candidate)
    if not paths:
        return
    reporter.step("Restoring SELinux file contexts")
    proc = run(["restorecon", "-R"] + paths, check=False)
    if proc.returncode != 0:
        report["warnings"].append(
            "restorecon failed; run 'restorecon -R /etc/prosody /var/lib/prosody' manually"
        )


def _migration_checklist(manifest, target, installed, db_meta):
    source = manifest.get("source", {})
    source_prosody = source.get("prosody", {}) or {}
    checklist = []
    if source.get("hostname") and source.get("hostname") != target.hostname:
        checklist.append("Update DNS/SRV records for %s if the server hostname changed"
                         % ", ".join(source_prosody.get("virtualhosts") or ["the XMPP domain"]))
    if not installed:
        checklist.append("Install Prosody on this host before starting the service")
    source_version = source.get("prosody_version")
    target_version = getattr(target, "version", None)
    if installed and source_version and target_version:
        source_major = str(source_version).split(".")[0]
        target_major = str(target_version).split(".")[0]
        if source_major != target_major:
            checklist.append(
                "Review Prosody version change: source %s, target %s"
                % (source_version, target_version)
            )
    missing = []
    if installed:
        available = set(target.available_modules())
        if available:
            missing = [name for name in (source_prosody.get("modules_enabled") or [])
                       if name not in available]
    if missing:
        checklist.append("Install missing Prosody modules: %s" % ", ".join(missing))
    checklist.extend([
        "Open the XMPP ports you use (5222, 5269, and 5280/5281 if HTTP/BOSH/WebSocket)",
        "Verify certificates and Let's Encrypt renewal for every VirtualHost",
        "Run 'prosodyctl check config' and review warnings",
        "Test client login, roster and MUC history before decommissioning the old server",
        "Update monitoring, firewall rules, backups and DNS TTLs",
    ])
    return checklist


def migrate_backup(config, repository, reference, options, reporter):
    options.mode = "migrate"
    return restore_backup(config, repository, reference, options, reporter)


def _ensure_decryptable(settings, entry, reporter):
    from .crypto import encryption_method
    settings = settings or {}
    archive = entry.get("archive", "")
    if archive.endswith(".age"):
        method = "age"
    elif archive.endswith(".gpg"):
        method = "gpg"
    else:
        method = encryption_method(settings)
    if method == "age":
        identities = settings.get("identities") or []
        if isinstance(identities, str):
            identities = [identities]
        if settings.get("identity"):
            identities = [settings["identity"]] + list(identities)
        if not identities:
            raise ConfigError(
                "this backup is age-encrypted; set encryption.identity in the proctor config",
                hint="run proctor with access to the age private key, or use a decrypted backup",
            )
    elif method not in ("gpg", "gnupg"):
        raise ConfigError("unsupported encryption method for this backup: %s" % method)
