"""Portable single-file export/import bundles for moving backups between hosts."""

from __future__ import print_function

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time

from . import __version__, FORMAT_VERSION
from .archive import detect_compression, read_manifest, validate_member
from .errors import ConfigError, IntegrityError, ProctorError
from .util import ensure_dir, isoformat, sha256_file

BUNDLE_FORMAT = "proctor-bundle"


def export_bundle(repository, reference, output, force=False, reporter=None):
    entry = repository.get(reference)
    sidecar = repository.read_sidecar(entry)
    archive = repository.archive_path(entry)
    if sidecar is None:
        if entry.get("encrypted"):
            raise ProctorError(
                "cannot export an encrypted backup without its sidecar manifest",
                hint="copy the archive and %s.manifest.json together"
                     % os.path.join(repository.backups_dir, entry["id"]),
            )
        manifest = json.loads(read_manifest(archive, repository.tmp_dir,
                                            compression=entry.get("compression")))
        sidecar = {"manifest": manifest, "artifact": {
            "archive": entry["archive"], "sha256": entry.get("sha256"),
            "size": entry.get("size"), "encrypted": False,
            "compression": entry.get("compression"),
        }}
    manifest = sidecar.get("manifest") or sidecar
    artifact = sidecar.get("artifact") or {}
    digest = entry.get("sha256") or artifact.get("sha256")
    if not digest:
        digest, size = sha256_file(archive)
    else:
        size = os.path.getsize(archive)
    output = os.path.abspath(os.path.expanduser(output))
    if os.path.exists(output) and not force:
        raise ConfigError("output already exists: %s" % output,
                          hint="use --force to overwrite")
    ensure_dir(os.path.dirname(output), mode=0o700)
    bundle_info = {
        "format": BUNDLE_FORMAT,
        "version": FORMAT_VERSION,
        "proctor_version": __version__,
        "created_at": isoformat(),
        "artifact": {
            "archive": entry["archive"],
            "sha256": digest,
            "size": size,
            "encrypted": bool(entry.get("encrypted")),
            "compression": entry.get("compression"),
        },
    }
    bundle_bytes = (json.dumps(bundle_info, indent=2) + "\n").encode("utf-8")
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    checksum_bytes = ("%s  %s\n" % (digest, entry["archive"])).encode("ascii")
    fd, temporary = tempfile.mkstemp(prefix=".proctor-bundle-", dir=os.path.dirname(output))
    os.close(fd)
    try:
        with tarfile.open(temporary, mode="w", format=tarfile.PAX_FORMAT) as tar:
            _add_bytes(tar, "BUNDLE.json", bundle_bytes)
            _add_bytes(tar, "manifest.json", manifest_bytes)
            _add_bytes(tar, "checksum.sha256", checksum_bytes)
            info = tar.gettarinfo(archive, arcname="archive/" + os.path.basename(archive))
            with open(archive, "rb") as handle:
                tar.addfile(info, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    result = {
        "id": entry["id"],
        "output": output,
        "sha256": digest,
        "size": os.path.getsize(output),
        "artifact": entry["archive"],
    }
    if reporter:
        reporter.success("Exported %s to %s" % (entry["id"], output))
    return result


def _add_bytes(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def import_bundle(repository, source, manifest_path=None, reporter=None):
    source = os.path.abspath(os.path.expanduser(source))
    if not os.path.isfile(source):
        raise ConfigError("import file not found: %s" % source)
    repository.ensure()
    workdir = tempfile.mkdtemp(prefix="import-", dir=repository.tmp_dir)
    os.chmod(workdir, 0o700)
    try:
        if _looks_like_tar(source):
            archive_path, manifest, bundle_info = _unpack_bundle(source, workdir)
            expected = (bundle_info or {}).get("artifact", {}).get("sha256")
        else:
            archive_path = source
            manifest = None
            expected = None
            document = None
            if manifest_path:
                document = _load_manifest_file(manifest_path)
            else:
                candidate = source + ".manifest.json"
                if not os.path.isfile(candidate):
                    base = os.path.basename(source)
                    for suffix in (".age", ".gpg"):
                        if base.endswith(suffix):
                            base = base[: -len(suffix)]
                            break
                    for suffix in (".tar.gz", ".tar.xz", ".tar.zst", ".tgz", ".txz", ".tzst", ".tar"):
                        if base.endswith(suffix):
                            base = base[: -len(suffix)]
                            break
                    candidate = os.path.join(os.path.dirname(source),
                                             base + ".manifest.json")
                if os.path.isfile(candidate):
                    document = _load_manifest_file(candidate)
            if document is not None:
                manifest = document.get("manifest", document)
                expected = (document.get("artifact") or {}).get("sha256")
            if manifest is None:
                if source.endswith((".age", ".gpg")):
                    raise ConfigError(
                        "an encrypted archive needs its .manifest.json sidecar to import",
                        hint="pass --manifest PATH or use a .proctorbundle export",
                    )
                manifest = json.loads(read_manifest(source, workdir,
                                                    compression=detect_compression(source)))
        manifest = _normalize_manifest(manifest)
        digest, size = sha256_file(archive_path)
        if expected and digest != expected:
            raise IntegrityError(
                "imported archive checksum does not match its manifest/bundle",
                hint="the file was modified or corrupted in transit",
            )
        if manifest.get("id") is None:
            raise IntegrityError("manifest has no backup id")
        if not manifest.get("created_at"):
            manifest["created_at"] = isoformat()
        entry, imported = repository.import_archive(archive_path, manifest, digest)
        if reporter:
            reporter.success("%s %s" % ("Imported" if imported else "Already present:",
                                         entry["id"]))
        return {
            "entry": entry,
            "imported": imported,
            "sha256": digest,
            "size": size,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _looks_like_tar(path):
    try:
        with tarfile.open(path, mode="r:*") as tar:
            names = tar.getnames()
            return any(name == "BUNDLE.json" for name in names)
    except (tarfile.TarError, OSError):
        return False


def _unpack_bundle(path, workdir):
    bundle_info = None
    manifest = None
    archive_path = None
    archive_name = None
    with tarfile.open(path, mode="r:*") as tar:
        for member in tar:
            name = validate_member(member)
            if not member.isfile():
                raise IntegrityError("bundle contains a non-regular member: %s" % name)
            if name == "BUNDLE.json":
                bundle_info = json.loads(tar.extractfile(member).read())
            elif name == "manifest.json":
                manifest = json.loads(tar.extractfile(member).read())
            elif name == "checksum.sha256":
                # Checksum is advisory; BUNDLE.json is authoritative.
                tar.extractfile(member).read()
            elif name.startswith("archive/"):
                base = os.path.basename(name)
                if not base or base in (".", ".."):
                    raise IntegrityError("invalid archive name in bundle")
                if archive_path is not None:
                    raise IntegrityError("bundle contains more than one archive")
                archive_name = base
                destination = os.path.join(workdir, base)
                with tar.extractfile(member) as source, open(destination, "wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                os.chmod(destination, 0o600)
                archive_path = destination
            else:
                raise IntegrityError("unexpected file in bundle: %s" % name)
    if not bundle_info:
        raise IntegrityError("bundle has no BUNDLE.json")
    if bundle_info.get("format") != BUNDLE_FORMAT:
        raise IntegrityError("not a Proctor bundle: %s" % bundle_info.get("format"))
    if not manifest:
        raise IntegrityError("bundle has no manifest.json")
    if archive_path is None:
        raise IntegrityError("bundle has no archive payload")
    artifact_name = (bundle_info.get("artifact") or {}).get("archive")
    if artifact_name and os.path.basename(artifact_name) != archive_name:
        raise IntegrityError("bundle archive name does not match BUNDLE.json")
    return archive_path, manifest, bundle_info


def _load_manifest_file(path):
    path = os.path.abspath(os.path.expanduser(path))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _normalize_manifest(manifest):
    if not isinstance(manifest, dict):
        raise IntegrityError("backup manifest is not an object")
    if manifest.get("format") != "proctor-backup":
        raise IntegrityError("not a Proctor backup manifest")
    if manifest.get("format_version", 1) > FORMAT_VERSION:
        raise IntegrityError(
            "backup format version %s is newer than this proctor supports"
            % manifest.get("format_version")
        )
    backup_id = str(manifest.get("id") or "")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", backup_id):
        raise IntegrityError("backup manifest has an unsafe id: %r" % backup_id)
    return manifest
