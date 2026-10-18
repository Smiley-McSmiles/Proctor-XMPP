"""Backup repository: index, locking, verification, retention and history."""

from __future__ import print_function

import copy
import gzip
import json
import os
import shutil
import tarfile
import tempfile
import time
from datetime import datetime

from . import FORMAT_VERSION
from .archive import (
    ArchiveReader, MANIFEST_NAME, detect_compression, member_sha256, read_manifest,
    validate_member, verify_members,
)
from .crypto import decrypt_file, is_encrypted, strip_encryption
from .errors import ConfigError, IntegrityError, ProctorError, UsageError
from .util import (
    FileLock, atomic_write, atomic_write_json, ensure_dir, isoformat, parse_iso, read_json,
    sha256_file, sha256_bytes, timestamp_id,
)

INDEX_NAME = "index.json"
MARKER_NAME = ".proctor-repo.json"
SIDECAR_SUFFIX = ".manifest.json"
CHECKSUM_SUFFIX = ".sha256"


class Repository(object):
    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.backups_dir = os.path.join(self.path, "backups")
        self.tmp_dir = os.path.join(self.path, "tmp")
        self.index_path = os.path.join(self.path, INDEX_NAME)
        self.marker_path = os.path.join(self.path, MARKER_NAME)
        self.lock_path = os.path.join(self.path, ".lock")
        self.history_path = os.path.join(self.path, "history.jsonl")

    # -- lifecycle -------------------------------------------------------
    def init(self, force=False):
        ensure_dir(self.path, mode=0o700)
        ensure_dir(self.backups_dir, mode=0o700)
        ensure_dir(self.tmp_dir, mode=0o700)
        if not os.path.exists(self.marker_path):
            atomic_write_json(self.marker_path, {
                "format": "proctor-repository",
                "version": FORMAT_VERSION,
                "created_at": isoformat(),
            })
        if not os.path.exists(self.index_path):
            sidecars = [name for name in os.listdir(self.backups_dir)
                        if name.endswith(SIDECAR_SUFFIX)]
            if sidecars:
                self.recover_index()
            else:
                self._save_index({"version": FORMAT_VERSION, "backups": []})
        os.chmod(self.path, 0o700)
        return self

    def exists(self):
        return os.path.isdir(self.path) and os.path.isfile(self.marker_path)

    def ensure(self):
        if not self.exists():
            self.init()
        return self

    def lock(self, timeout=30.0):
        ensure_dir(self.path, mode=0o700)
        return FileLock(self.lock_path, timeout=timeout)

    def cleanup_tmp(self, max_age=86400):
        ensure_dir(self.tmp_dir, mode=0o700)
        now = time.time()
        for name in os.listdir(self.tmp_dir):
            path = os.path.join(self.tmp_dir, name)
            try:
                if now - os.stat(path).st_mtime > max_age:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.unlink(path)
            except OSError:
                pass

    # -- index -----------------------------------------------------------
    def load_index(self):
        data = read_json(self.index_path, default=None)
        if not data or not isinstance(data.get("backups"), list):
            if not os.path.isdir(self.backups_dir):
                return {"version": FORMAT_VERSION, "backups": []}
            data = self.recover_index()
        return data

    def _save_index(self, data):
        data["version"] = FORMAT_VERSION
        data["updated_at"] = isoformat()
        atomic_write_json(self.index_path, data, mode=0o600)

    def recover_index(self):
        ensure_dir(self.backups_dir, mode=0o700)
        entries = []
        for name in sorted(os.listdir(self.backups_dir)):
            if not name.endswith(SIDECAR_SUFFIX):
                continue
            path = os.path.join(self.backups_dir, name)
            sidecar = read_json(path, default=None)
            if not sidecar:
                continue
            entry = self._entry_from_sidecar(sidecar)
            if entry:
                entries.append(entry)
        data = {"version": FORMAT_VERSION, "backups": entries, "recovered_at": isoformat()}
        self._save_index(data)
        return data

    def list_entries(self):
        data = self.load_index()
        entries = data.get("backups", [])
        return sorted(entries, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, reference):
        entries = self.list_entries()
        if not entries:
            raise UsageError("no backups in repository %s" % self.path)
        reference = str(reference).strip()
        if reference in ("latest", "last"):
            return entries[0]
        for entry in entries:
            if entry.get("id") == reference or entry.get("archive") == reference:
                return entry
        matches = [entry for entry in entries if entry.get("id", "").startswith(reference)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(entry["id"] for entry in matches[:8])
            raise UsageError("ambiguous backup reference %r: %s" % (reference, ids))
        raise UsageError("backup not found: %s" % reference)

    def archive_path(self, entry):
        name = os.path.basename(str(entry.get("archive") or ""))
        if name != entry.get("archive") or not name:
            raise IntegrityError("unsafe archive name in repository index: %r"
                                 % entry.get("archive"))
        path = os.path.join(self.backups_dir, name)
        if not os.path.isfile(path):
            raise ProctorError(
                "backup archive is missing: %s" % path,
                hint="run 'proctor list' and remove stale entries, or restore from another copy",
            )
        return path

    def sidecar_path(self, entry):
        name = os.path.basename(str(entry.get("manifest") or (entry["id"] + SIDECAR_SUFFIX)))
        if name != (entry.get("manifest") or (entry["id"] + SIDECAR_SUFFIX)):
            raise IntegrityError("unsafe manifest name in repository index")
        return os.path.join(self.backups_dir, name)

    def checksum_path(self, entry):
        return self.archive_path(entry) + CHECKSUM_SUFFIX

    def read_sidecar(self, entry):
        data = read_json(self.sidecar_path(entry), default=None)
        if not data:
            return None
        if "manifest" in data:
            return data
        return {"manifest": data, "artifact": {}}

    def open_archive(self, entry, workdir=None, encryption=None):
        """Return a context manager yielding a streaming tar for this entry."""
        from contextlib import contextmanager

        @contextmanager
        def opener():
            workdir_path = workdir or self.tmp_dir
            ensure_dir(workdir_path, mode=0o700)
            path = self.archive_path(entry)
            temporary = None
            plain = path
            if entry.get("encrypted"):
                settings = _encryption_for_archive(entry, encryption or {})
                fd, temporary = tempfile.mkstemp(
                    prefix="proctor-decrypt-", suffix=".tar", dir=workdir_path
                )
                os.close(fd)
                try:
                    decrypt_file(path, temporary, settings)
                except Exception:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
                    raise
                plain = temporary
            reader = None
            try:
                compression = entry.get("compression")
                if not compression:
                    try:
                        compression = detect_compression(strip_encryption(path)[0])
                    except ProctorError:
                        compression = None
                reader = ArchiveReader(plain, workdir_path, compression=compression)
                try:
                    tar = reader.__enter__()
                except Exception as exc:
                    raise IntegrityError("cannot open backup archive: %s" % exc)
                yield tar
            finally:
                if reader is not None:
                    reader.__exit__(None, None, None)
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

        return opener()

    def manifest(self, entry):
        sidecar = self.read_sidecar(entry)
        if sidecar and sidecar.get("manifest"):
            return sidecar["manifest"]
        if not entry.get("encrypted"):
            compression = entry.get("compression")
            if not compression:
                try:
                    compression = detect_compression(strip_encryption(
                        self.archive_path(entry)
                    )[0])
                except ProctorError:
                    compression = None
            ensure_dir(self.tmp_dir, mode=0o700)
            return json.loads(read_manifest(self.archive_path(entry), self.tmp_dir,
                                            compression=compression))
        raise IntegrityError(
            "sidecar manifest is missing or unreadable: %s" % self.sidecar_path(entry),
            hint="run 'proctor verify' or re-import the backup",
        )

    # -- adding ----------------------------------------------------------
    def add(self, manifest, archive_path, artifact_sha256, size=None, note=None, tags=None):
        """Register an already-final archive and write its sidecar."""
        ensure_dir(self.backups_dir, mode=0o700)
        entry = self._entry_from_manifest(manifest)
        entry["sha256"] = artifact_sha256
        entry["size"] = size if size is not None else os.path.getsize(archive_path)
        entry["archive"] = os.path.basename(archive_path)
        entry["manifest"] = entry["id"] + SIDECAR_SUFFIX
        entry["encrypted"] = (is_encrypted(entry["archive"])
                              or bool((manifest.get("options") or {}).get("encrypted")))
        try:
            entry["compression"] = detect_compression(entry["archive"])
        except ProctorError:
            entry["compression"] = manifest.get("options", {}).get("compression")
        entry["note"] = note if note is not None else manifest.get("note", "")
        entry["tags"] = list(tags if tags is not None else manifest.get("tags", []))
        sidecar = {
            "format": "proctor-sidecar",
            "version": FORMAT_VERSION,
            "manifest": manifest,
            "artifact": {
                "archive": entry["archive"],
                "sha256": entry["sha256"],
                "size": entry["size"],
                "encrypted": entry["encrypted"],
                "compression": entry["compression"],
            },
        }
        atomic_write_json(self.sidecar_path(entry), sidecar, mode=0o600)
        self._write_checksum(archive_path, entry["sha256"])
        data = self.load_index()
        data["backups"] = [item for item in data.get("backups", []) if item.get("id") != entry["id"]]
        data["backups"].append(entry)
        self._save_index(data)
        return entry

    def add_existing(self, manifest, archive_path, artifact_sha256, sidecar=None):
        entry = self.add(manifest, archive_path, artifact_sha256)
        return entry

    def import_archive(self, source_archive, manifest, artifact_sha256, sidecar=None):
        ensure_dir(self.backups_dir, mode=0o700)
        archive_name = os.path.basename(source_archive)
        if not archive_name or archive_name.startswith("."):
            raise UsageError("invalid archive filename: %s" % archive_name)
        for existing in self.list_entries():
            if existing.get("id") != manifest.get("id"):
                continue
            if existing.get("sha256") and existing["sha256"] != artifact_sha256:
                raise ConfigError(
                    "backup id %s already exists with different contents"
                    % manifest.get("id")
                )
            if not os.path.isfile(self.sidecar_path(existing)):
                self.add(manifest, self.archive_path(existing), artifact_sha256)
            return existing, False
        destination = os.path.join(self.backups_dir, archive_name)
        if os.path.exists(destination):
            existing = sha256_file(destination)[0]
            if existing == artifact_sha256:
                for entry in self.list_entries():
                    if entry.get("archive") == archive_name:
                        return entry, False
                return self.add(manifest, destination, artifact_sha256), False
            raise ConfigError("an archive with the same name but different checksum already exists")
        if os.path.abspath(source_archive) != os.path.abspath(destination):
            shutil.copyfile(source_archive, destination)
        os.chmod(destination, 0o600)
        entry = self.add(manifest, destination, artifact_sha256)
        return entry, True

    def _entry_from_manifest(self, manifest):
        source = manifest.get("source", {})
        stats = manifest.get("stats", {})
        return {
            "id": manifest.get("id"),
            "created_at": manifest.get("created_at"),
            "hostname": source.get("hostname"),
            "fqdn": source.get("fqdn"),
            "prosody_version": source.get("prosody_version"),
            "components": manifest.get("components", {}),
            "database": (manifest.get("database") or {}).get("engine"),
            "users": stats.get("user_count"),
            "archive": None,
            "manifest": None,
            "sha256": None,
            "size": stats.get("bytes"),
            "encrypted": (manifest.get("options") or {}).get("encrypted", False),
            "compression": (manifest.get("options") or {}).get("compression"),
            "note": manifest.get("note", ""),
            "tags": manifest.get("tags", []),
            "format_version": manifest.get("format_version"),
        }

    def _entry_from_sidecar(self, sidecar):
        if "manifest" in sidecar:
            manifest = sidecar["manifest"]
            artifact = sidecar.get("artifact") or {}
        else:
            manifest = sidecar
            artifact = {}
        entry = self._entry_from_manifest(manifest)
        if not entry.get("id"):
            return None
        archive = artifact.get("archive")
        if not archive:
            manifest_file = entry["id"] + SIDECAR_SUFFIX
            for suffix in (".tar.gz.age", ".tar.gz.gpg", ".tar.xz.age", ".tar.xz.gpg",
                           ".tar.zst.age", ".tar.zst.gpg", ".tar.gz", ".tar.xz", ".tar.zst",
                           ".tar.age", ".tar.gpg", ".tar"):
                candidate = os.path.join(self.backups_dir, entry["id"] + suffix)
                if os.path.isfile(candidate):
                    archive = entry["id"] + suffix
                    break
        if not archive:
            return None
        entry["archive"] = archive
        entry["manifest"] = entry["id"] + SIDECAR_SUFFIX
        entry["sha256"] = artifact.get("sha256")
        entry["size"] = artifact.get("size")
        entry["encrypted"] = artifact.get("encrypted", is_encrypted(archive))
        entry["compression"] = artifact.get("compression")
        entry["note"] = manifest.get("note", "")
        entry["tags"] = manifest.get("tags", [])
        return entry

    def _write_checksum(self, archive_path, digest):
        name = os.path.basename(archive_path)
        atomic_write(archive_path + CHECKSUM_SUFFIX,
                     ("%s  %s\n" % (digest, name)).encode("ascii"), mode=0o600)

    # -- verification ----------------------------------------------------
    def verify(self, entry, workdir=None, encryption=None, reporter=None, quick=False):
        errors = []
        warnings = []
        archive = self.archive_path(entry)
        result = {
            "id": entry["id"],
            "archive": entry["archive"],
            "encrypted": entry.get("encrypted", False),
            "checksum_ok": False,
            "members_ok": None,
            "manifest_ok": False,
            "errors": errors,
            "warnings": warnings,
        }
        expected = entry.get("sha256")
        checksum_path = archive + CHECKSUM_SUFFIX
        if os.path.isfile(checksum_path):
            try:
                with open(checksum_path, "r", encoding="ascii", errors="replace") as handle:
                    parts = handle.read().split()
                if not parts:
                    warnings.append("checksum sidecar is empty")
                elif expected and parts[0] != expected:
                    errors.append("checksum sidecar disagrees with index")
            except OSError as exc:
                warnings.append("cannot read checksum sidecar: %s" % exc)
        actual, size = sha256_file(archive)
        result["sha256"] = actual
        result["size"] = size
        if expected and actual != expected:
            errors.append("archive checksum mismatch")
        else:
            result["checksum_ok"] = True
        if quick or (entry.get("encrypted") and encryption is None):
            return result
        if entry.get("encrypted") and encryption is not None:
            encryption = _encryption_for_archive(entry, encryption)
        sidecar = self.read_sidecar(entry)
        if not sidecar or not sidecar.get("manifest"):
            errors.append("sidecar manifest missing")
            return result
        workdir = workdir or self.tmp_dir
        ensure_dir(workdir, mode=0o700)
        temp = None
        plain = archive
        try:
            if entry.get("encrypted"):
                fd, temp = tempfile.mkstemp(prefix="proctor-verify-", suffix=".tar", dir=workdir)
                os.close(fd)
                decrypt_file(archive, temp, encryption)
                plain = temp
            compression = entry.get("compression")
            if not compression:
                try:
                    compression = detect_compression(strip_encryption(archive)[0])
                except ProctorError:
                    compression = None
            embedded = json.loads(read_manifest(
                plain, workdir, compression=compression
            ))
            if embedded != sidecar["manifest"]:
                errors.append("embedded manifest differs from sidecar manifest")
            else:
                result["manifest_ok"] = True
            with ArchiveReader(plain, workdir, compression=compression) as tar:
                errors.extend(verify_members(tar, embedded))
            result["members_ok"] = not any("mismatch" in item or "missing" in item
                                           or "unexpected" in item for item in errors)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if temp:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
        return result

    # -- removal / retention ---------------------------------------------
    def delete(self, entry, dry_run=False):
        archive = self.archive_path(entry)
        sidecar = self.sidecar_path(entry)
        paths = [archive, archive + CHECKSUM_SUFFIX, sidecar]
        freed = 0
        for path in paths:
            if os.path.isfile(path):
                try:
                    freed += os.path.getsize(path)
                except OSError:
                    pass
                if not dry_run:
                    os.unlink(path)
        if not dry_run:
            data = self.load_index()
            data["backups"] = [item for item in data.get("backups", [])
                               if item.get("id") != entry.get("id")]
            self._save_index(data)
        return {
            "id": entry["id"],
            "archive": entry["archive"],
            "freed": freed,
            "dry_run": dry_run,
            "paths": paths,
        }

    def prune(self, policy, dry_run=True):
        entries = self.list_entries()
        keep, remove = select_retention(entries, policy)
        removed = []
        freed = 0
        for entry in remove:
            result = self.delete(entry, dry_run=dry_run)
            removed.append(result)
            freed += result["freed"]
        return {
            "kept": [entry["id"] for entry in keep],
            "removed": removed,
            "freed": freed,
            "dry_run": dry_run,
        }

    # -- history ---------------------------------------------------------
    def append_history(self, action, details=None, limit=5000):
        ensure_dir(self.path, mode=0o700)
        record = {
            "time": isoformat(),
            "action": action,
            "user": _current_user(),
        }
        if details:
            record["details"] = details
        try:
            with open(self.history_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.history_path, 0o600)
        except OSError:
            return
        self._trim_history(limit)

    def _trim_history(self, limit):
        try:
            with open(self.history_path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            if len(lines) <= limit:
                return
            atomic_write(self.history_path, "".join(lines[-limit:]).encode("utf-8"), mode=0o600)
        except OSError:
            pass

    def history(self, limit=50):
        records = []
        try:
            with open(self.history_path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return records[-limit:][::-1]

    # -- stats -----------------------------------------------------------
    def stats(self):
        entries = self.list_entries()
        total = 0
        for entry in entries:
            try:
                total += os.path.getsize(os.path.join(self.backups_dir, entry["archive"]))
            except OSError:
                pass
        latest = entries[0] if entries else None
        return {
            "path": self.path,
            "count": len(entries),
            "bytes": total,
            "latest": latest,
        }


def _encryption_for_archive(entry, settings):
    settings = copy.deepcopy(settings or {})
    archive = entry.get("archive", "")
    if archive.endswith(".gpg"):
        settings["method"] = "gpg"
    elif archive.endswith(".age"):
        settings["method"] = "age"
    return settings


def _current_user():
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return None


def select_retention(entries, policy):
    """Grandfather-father-son retention. Returns (keep, remove)."""
    policy = policy or {}
    keep_last = max(0, int(policy.get("keep_last", 0) or 0))
    keep_daily = max(0, int(policy.get("keep_daily", 0) or 0))
    keep_weekly = max(0, int(policy.get("keep_weekly", 0) or 0))
    keep_monthly = max(0, int(policy.get("keep_monthly", 0) or 0))
    if not any((keep_last, keep_daily, keep_weekly, keep_monthly)):
        raise UsageError("retention policy would keep zero backups; set at least one keep-* value")

    ordered = sorted(entries, key=lambda item: item.get("created_at", ""), reverse=True)
    keep_ids = set()
    if ordered:
        # The newest backup is never deleted by retention.
        keep_ids.add(ordered[0]["id"])
    for entry in ordered[:keep_last]:
        keep_ids.add(entry["id"])

    def keep_buckets(count, key_function):
        if not count:
            return
        buckets = {}
        for entry in ordered:
            dt = parse_iso(entry.get("created_at"))
            if dt is None:
                continue
            key = key_function(dt)
            if key not in buckets:
                buckets[key] = entry
        for index, key in enumerate(sorted(buckets, reverse=True)):
            if index >= count:
                break
            keep_ids.add(buckets[key]["id"])

    keep_buckets(keep_daily, lambda dt: dt.strftime("%Y-%m-%d"))
    keep_buckets(keep_weekly, lambda dt: dt.strftime("%G-W%V"))
    keep_buckets(keep_monthly, lambda dt: dt.strftime("%Y-%m"))

    for entry in ordered:
        tags = [str(tag).lower() for tag in (entry.get("tags") or [])]
        if "protected" in tags or "keep" in tags:
            keep_ids.add(entry["id"])

    keep = [entry for entry in ordered if entry.get("id") in keep_ids]
    remove = [entry for entry in ordered if entry.get("id") not in keep_ids]
    return keep, remove
