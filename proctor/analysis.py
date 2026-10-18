"""Read-only inspection: user lists and comparisons against the live host."""

from __future__ import print_function

import os
import shutil
import tempfile

from .database import sqlite_users_from_file
from .errors import IntegrityError
from .prosody import detect_prosody
from .util import is_within


def list_users(repository, reference, encryption=None, reporter=None, domain=None):
    entry = repository.get(reference)
    manifest = repository.manifest(entry)
    users = set()
    filesystem = manifest.get("users", {}).get("filesystem") or {}
    for host, names in filesystem.items():
        for name in names:
            users.add("%s@%s" % (name, host))
    db_meta = manifest.get("database") or {}
    preview = manifest.get("users", {}).get("preview") or []
    for item in preview:
        if "@" in item:
            users.add(item)
    if db_meta.get("engine") == "sqlite" and db_meta.get("dump"):
        users.update(_users_from_sqlite_dump(repository, entry, manifest,
                                             encryption=encryption))
    result_users = sorted(users)
    if domain:
        result_users = [item for item in result_users if item.rsplit("@", 1)[-1] == domain]
    counts = {}
    for item in result_users:
        host = item.rsplit("@", 1)[-1]
        counts[host] = counts.get(host, 0) + 1
    return {
        "id": entry["id"],
        "count": len(result_users),
        "domains": counts,
        "users": result_users,
        "truncated": False,
    }


def _users_from_sqlite_dump(repository, entry, manifest, encryption):
    dump_name = (manifest.get("database") or {}).get("dump")
    if not dump_name:
        return []
    workdir = tempfile.mkdtemp(prefix="users-", dir=repository.tmp_dir)
    os.chmod(workdir, 0o700)
    try:
        destination = os.path.join(workdir, os.path.basename(dump_name))
        found = False
        with repository.open_archive(entry, workdir=workdir, encryption=encryption) as tar:
            for member in tar:
                if member.name == dump_name and member.isfile():
                    with tar.extractfile(member) as source, open(destination, "wb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    found = True
                    break
        if not found:
            return []
        return sqlite_users_from_file(destination)
    except Exception as exc:
        if reporter:
            reporter.warn("could not read users from the SQLite dump: %s" % exc)
        return []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def diff_backup(config, repository, reference, encryption=None, reporter=None):
    entry = repository.get(reference)
    manifest = repository.manifest(entry)
    host = config.host
    install = detect_prosody(host, overrides={
        "config": config.prosody.get("config"),
        "data_dir": config.prosody.get("data_dir"),
        "log_dir": config.prosody.get("log_dir"),
        "cert_dirs": config.prosody.get("cert_dirs"),
        "plugin_dirs": config.prosody.get("plugin_dirs"),
        "user": config.prosody.get("user"),
        "group": config.prosody.get("group"),
    }, require=False)
    source = manifest.get("source", {})
    source_prosody = source.get("prosody", {}) or {}
    differences = []
    if source.get("hostname") != host.hostname:
        differences.append({
            "field": "hostname",
            "backup": source.get("hostname"),
            "current": host.hostname,
        })
    if install is None:
        differences.append({
            "field": "prosody",
            "backup": source_prosody.get("config_file"),
            "current": "not installed/configured",
        })
        return {
            "id": entry["id"],
            "compatible": False,
            "differences": differences,
            "backup_users": manifest.get("stats", {}).get("user_count"),
            "current_users": None,
        }
    pairs = [
        ("config_file", source_prosody.get("config_file"), install.config_file),
        ("data_dir", source_prosody.get("data_dir"), install.data_dir),
        ("log_dir", source_prosody.get("log_dir"), install.log_dir),
        ("storage", source_prosody.get("storage"), install.storage),
        ("prosody_version", source.get("prosody_version"), install.version),
        ("user", source_prosody.get("user"), install.user),
    ]
    for field, backup_value, current_value in pairs:
        if backup_value != current_value:
            differences.append({"field": field, "backup": backup_value, "current": current_value})
    backup_db = (manifest.get("database") or {}).get("engine")
    current_db = (install.sql or {}).get("engine") if install.sql else None
    if backup_db != current_db:
        differences.append({"field": "database", "backup": backup_db, "current": current_db})
    backup_users = manifest.get("stats", {}).get("user_count")
    current_users = None
    if current_db == "sqlite" and install.sql.get("path") and os.path.isfile(install.sql["path"]):
        current_users = len(sqlite_users_from_file(install.sql["path"]))
    elif install.storage != "sql":
        current_users = sum(len(users) for users in install.user_accounts().values())
    if backup_users != current_users:
        differences.append({
            "field": "user_count",
            "backup": backup_users,
            "current": current_users,
        })
    return {
        "id": entry["id"],
        "compatible": not differences,
        "differences": differences,
        "backup_users": backup_users,
        "current_users": current_users,
    }
