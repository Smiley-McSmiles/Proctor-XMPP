"""Portable tar archive writing/reading with hashing and safe member checks."""

from __future__ import print_function

import gzip
import hashlib
import io
import lzma
import os
import shutil
import stat
import tarfile
import tempfile
import time

from .errors import IntegrityError, ProctorError
from .util import (
    CHUNK_SIZE, ensure_dir, gid_name, safe_archive_name, run, sha256_bytes, uid_name, which,
)

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - older Python
    _zstd = None

COMPRESSION_EXTENSIONS = {
    "gz": ".tar.gz",
    "xz": ".tar.xz",
    "zstd": ".tar.zst",
    "none": ".tar",
}

MANIFEST_NAME = "manifest.json"


def compression_extension(compression):
    try:
        return COMPRESSION_EXTENSIONS[compression]
    except KeyError:
        raise ProctorError("unsupported compression: %s" % compression)


def detect_compression(filename):
    name = str(filename)
    for suffix in (".age", ".gpg"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "gz"
    if name.endswith(".tar.xz") or name.endswith(".txz"):
        return "xz"
    if name.endswith(".tar.zst") or name.endswith(".tzst"):
        return "zstd"
    if name.endswith(".tar"):
        return "none"
    raise ProctorError("cannot determine compression from filename: %s" % filename)


class TarOutput(object):
    """Streaming tar output over gzip/xz/zstd/plain."""

    def __init__(self, path, compression):
        self.path = path
        self.compression = compression
        self._compress_after = None
        self._raw = None
        self._handle = None
        self._tar = None
        ensure_dir(os.path.dirname(os.path.abspath(path)), mode=0o700)
        if compression == "gz":
            self._handle = gzip.open(path, "wb", compresslevel=6)
        elif compression == "xz":
            self._handle = lzma.open(path, "wb", preset=6)
        elif compression == "zstd":
            if _zstd is not None:
                self._handle = _zstd.open(path, "wb", level=6)
            else:
                if not which("zstd"):
                    raise ProctorError(
                        "zstd compression requested but Python lacks zstd support and the "
                        "'zstd' binary is not installed"
                    )
                self._compress_after = path
                self._raw = tempfile.NamedTemporaryFile(
                    prefix="proctor-uncompressed-", suffix=".tar", delete=False
                )
                self._handle = self._raw
                path = self._raw.name
        elif compression == "none":
            self._handle = open(path, "wb")
        else:
            raise ProctorError("unsupported compression: %s" % compression)
        self._tar = tarfile.open(fileobj=self._handle, mode="w|", format=tarfile.PAX_FORMAT)

    @property
    def tar(self):
        return self._tar

    def close(self):
        try:
            if self._tar is not None:
                try:
                    self._tar.close()
                finally:
                    self._tar = None
        finally:
            if self._handle is not None:
                try:
                    self._handle.close()
                finally:
                    self._handle = None
        if self._compress_after is not None:
            source = self._raw.name
            self._raw.close()
            try:
                run(["zstd", "-q", "-f", "-6", "-o", self._compress_after, source])
            finally:
                try:
                    os.unlink(source)
                except OSError:
                    pass
            self._compress_after = None
            self._raw = None

    def abort(self):
        try:
            self.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass


class _HashingReader(object):
    def __init__(self, handle, size):
        self.handle = handle
        self.remaining = size
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        if size <= 0:
            return b""
        data = self.handle.read(size)
        self.remaining -= len(data)
        self.digest.update(data)
        return data


class BackupWriter(object):
    def __init__(self, path, compression="gz", skip_unreadable=False):
        self.path = path
        self.compression = compression
        self.skip_unreadable = skip_unreadable
        self.output = TarOutput(path, compression)
        self.entries = []
        self.extras = []
        self.warnings = []
        self.skipped = []
        self.components = {}
        self._seen = set()
        self._uid_cache = {}
        self._gid_cache = {}
        self._closed = False

    def _names(self, st):
        if st.st_uid not in self._uid_cache:
            self._uid_cache[st.st_uid] = uid_name(st.st_uid)
        if st.st_gid not in self._gid_cache:
            self._gid_cache[st.st_gid] = gid_name(st.st_gid)
        return self._uid_cache[st.st_uid], self._gid_cache[st.st_gid]

    def _base_info(self, arcname, st, entry_type):
        info = tarfile.TarInfo(arcname)
        info.type = entry_type
        info.mode = stat.S_IMODE(st.st_mode)
        info.uid = st.st_uid
        info.gid = st.st_gid
        info.uname, info.gname = self._names(st)
        info.mtime = st.st_mtime
        info.size = 0
        return info

    def _record(self, component, source, arcname, st, entry_type, sha256=None, linkname=None):
        if st.st_uid not in self._uid_cache:
            self._uid_cache[st.st_uid] = uid_name(st.st_uid)
        if st.st_gid not in self._gid_cache:
            self._gid_cache[st.st_gid] = gid_name(st.st_gid)
        self.entries.append({
            "path": source,
            "arcname": arcname,
            "type": entry_type,
            "size": st.st_size if entry_type == "file" else 0,
            "mode": stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "uname": self._uid_cache[st.st_uid],
            "gname": self._gid_cache[st.st_gid],
            "mtime": int(st.st_mtime),
            "sha256": sha256,
            "linkname": linkname,
            "component": component,
        })
        stats = self.components.setdefault(component, {"files": 0, "bytes": 0})
        if entry_type == "file":
            stats["files"] += 1
            stats["bytes"] += st.st_size

    def add_tree(self, root, component, skip_prefixes=None):
        root = os.path.abspath(root)
        skip_prefixes = [os.path.abspath(item) for item in (skip_prefixes or [])]
        if not os.path.lexists(root):
            self.warnings.append("skipping missing %s path: %s" % (component, root))
            return
        try:
            st = os.lstat(root)
        except OSError as exc:
            self.warnings.append("cannot stat %s: %s" % (root, exc))
            return
        self._add(root, component, st, skip_prefixes)
        if stat.S_ISDIR(st.st_mode):
            self._walk(root, component, skip_prefixes)

    def _walk(self, directory, component, skip_prefixes):
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            self.warnings.append("cannot list %s: %s" % (directory, exc))
            return
        for name in names:
            path = os.path.join(directory, name)
            if any(path == prefix or path.startswith(prefix + os.sep) for prefix in skip_prefixes):
                continue
            try:
                st = os.lstat(path)
            except OSError as exc:
                self.warnings.append("cannot stat %s: %s" % (path, exc))
                continue
            if self._add(path, component, st, skip_prefixes):
                if stat.S_ISDIR(st.st_mode):
                    self._walk(path, component, skip_prefixes)

    def _add(self, path, component, st, skip_prefixes):
        if path in self._seen:
            return False
        if any(path == prefix or path.startswith(prefix + os.sep) for prefix in skip_prefixes):
            return False
        self._seen.add(path)
        arcname = "files/" + safe_archive_name(path)
        try:
            if stat.S_ISDIR(st.st_mode):
                info = self._base_info(arcname, st, tarfile.DIRTYPE)
                self.output.tar.addfile(info)
                self._record(component, path, arcname, st, "dir")
            elif stat.S_ISREG(st.st_mode):
                self._add_regular(path, arcname, component, st)
            elif stat.S_ISLNK(st.st_mode):
                target = os.readlink(path)
                info = self._base_info(arcname, st, tarfile.SYMTYPE)
                info.linkname = target
                self.output.tar.addfile(info)
                self._record(component, path, arcname, st, "symlink", linkname=target)
            else:
                self.skipped.append({
                    "path": path,
                    "reason": "unsupported file type (socket, fifo or device)",
                })
            return True
        except OSError as exc:
            if self.skip_unreadable:
                self.warnings.append("skipping unreadable file %s: %s" % (path, exc))
                return False
            raise ProctorError("failed to read %s: %s" % (path, exc))

    def _add_regular(self, path, arcname, component, st):
        info = self._base_info(arcname, st, tarfile.REGTYPE)
        info.size = st.st_size
        try:
            handle = open(path, "rb")
        except OSError as exc:
            if self.skip_unreadable:
                self.warnings.append("skipping unreadable file %s: %s" % (path, exc))
                return
            raise ProctorError("cannot open %s for backup: %s" % (path, exc))
        with handle:
            reader = _HashingReader(handle, st.st_size)
            self.output.tar.addfile(info, reader)
            if reader.remaining != 0:
                raise IntegrityError(
                    "file changed while being backed up (short read): %s" % path,
                    hint="run again without --live, or use --live with a quiet server",
                )
        self._record(component, path, arcname, st, "file", sha256=reader.digest.hexdigest())

    def add_file(self, source, arcname, component="database", mode=None):
        source = os.path.abspath(source)
        try:
            st = os.lstat(source)
        except OSError as exc:
            raise ProctorError("cannot stat %s: %s" % (source, exc))
        info = self._base_info(arcname, st, tarfile.REGTYPE)
        if mode is not None:
            info.mode = mode
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.size = st.st_size
        digest = hashlib.sha256()
        with open(source, "rb") as handle:
            def generator():
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        return
                    digest.update(chunk)
                    yield chunk
            self.output.tar.addfile(info, _GeneratorReader(generator()))
        self.extras.append({
            "arcname": arcname,
            "size": st.st_size,
            "sha256": digest.hexdigest(),
            "component": component,
        })
        return digest.hexdigest()

    def add_bytes(self, arcname, data, mode=0o600, component="meta", record=True):
        info = tarfile.TarInfo(arcname)
        info.type = tarfile.REGTYPE
        info.mode = mode
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mtime = int(time.time())
        info.size = len(data)
        self.output.tar.addfile(info, io.BytesIO(data))
        result = sha256_bytes(data)
        if record:
            self.extras.append({
                "arcname": arcname,
                "size": len(data),
                "sha256": result,
                "component": component,
            })
        return result

    def close(self):
        if not self._closed:
            self.output.close()
            self._closed = True

    def abort(self):
        if not self._closed:
            self.output.abort()
            self._closed = True


class _GeneratorReader(object):
    def __init__(self, generator):
        self.generator = generator
        self.buffer = b""

    def read(self, size=-1):
        if size is None or size < 0:
            chunks = [self.buffer]
            self.buffer = b""
            for chunk in self.generator:
                chunks.append(chunk)
            return b"".join(chunks)
        while len(self.buffer) < size:
            try:
                self.buffer += next(self.generator)
            except StopIteration:
                break
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result


class ArchiveReader(object):
    """Context manager that opens an archive (already decrypted if needed)."""

    def __init__(self, path, workdir, compression=None):
        self.path = path
        self.workdir = workdir
        self.compression = compression or detect_compression(path)
        self._handle = None
        self._tar = None
        self._temporary = None

    def __enter__(self):
        try:
            return self._open()
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _open(self):
        compression = self.compression
        path = self.path
        if compression == "gz":
            self._handle = gzip.open(path, "rb")
        elif compression == "xz":
            self._handle = lzma.open(path, "rb")
        elif compression == "zstd":
            if _zstd is not None:
                self._handle = _zstd.open(path, "rb")
            else:
                if not which("zstd"):
                    raise ProctorError("zstd-compressed backup but 'zstd' is not installed")
                ensure_dir(self.workdir, mode=0o700)
                fd, temporary = tempfile.mkstemp(prefix="proctor-decompress-", suffix=".tar", dir=self.workdir)
                os.close(fd)
                self._temporary = temporary
                self._decompress_zstd(path, temporary)
                self._handle = open(temporary, "rb")
        elif compression == "none":
            self._handle = open(path, "rb")
        else:
            raise ProctorError("unsupported compression: %s" % compression)
        self._tar = tarfile.open(fileobj=self._handle, mode="r|")
        return self._tar

    def _decompress_zstd(self, source, destination):
        import subprocess
        with open(destination, "wb") as output:
            proc = subprocess.run(["zstd", "-d", "-c", source], stdout=output)
        if proc.returncode != 0:
            raise ProctorError("could not decompress %s with zstd" % source)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._tar is not None:
                try:
                    self._tar.close()
                finally:
                    self._tar = None
        finally:
            if self._handle is not None:
                try:
                    self._handle.close()
                finally:
                    self._handle = None
            if self._temporary:
                try:
                    os.unlink(self._temporary)
                except OSError:
                    pass
                self._temporary = None
        return False


def read_manifest(path, workdir, compression=None):
    try:
        with ArchiveReader(path, workdir, compression=compression) as tar:
            for member in tar:
                if member.name == MANIFEST_NAME and member.isfile():
                    return tar.extractfile(member).read()
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError("cannot read archive %s: %s" % (path, exc))
    raise IntegrityError("archive has no manifest.json: %s" % path)


def validate_member(member):
    # Tar member names always use '/', even on Windows. A literal backslash is
    # a valid filename character on POSIX and must not be treated as a
    # separator.
    name = member.name
    if name.startswith("/") or name.startswith("../"):
        raise IntegrityError("unsafe archive member: %s" % member.name)
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise IntegrityError("unsafe archive member: %s" % member.name)
    if member.isdev():
        raise IntegrityError("archive contains a device node: %s" % member.name)
    if member.isfifo():
        raise IntegrityError("archive contains a fifo: %s" % member.name)
    if member.islnk():
        link = member.linkname
        if link.startswith("/") or ".." in link.split("/"):
            raise IntegrityError("unsafe hardlink in archive: %s -> %s" % (member.name, member.linkname))
    return name


def member_sha256(tar, member):
    digest = hashlib.sha256()
    size = 0
    handle = tar.extractfile(member)
    if handle is None:
        raise IntegrityError("cannot read archive member: %s" % member.name)
    while True:
        chunk = handle.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def verify_members(tar, manifest, reporter=None, limit_errors=25):
    expected = {}
    for entry in manifest.get("entries", []):
        if entry.get("type") == "file":
            expected[entry["arcname"]] = ("file", entry.get("sha256"), entry.get("size"), None)
        elif entry.get("type") == "symlink":
            expected[entry["arcname"]] = ("symlink", None, None, entry.get("linkname"))
        else:
            expected[entry["arcname"]] = (entry.get("type"), None, None, None)
    for extra in manifest.get("extras", []):
        expected[extra["arcname"]] = ("file", extra.get("sha256"), extra.get("size"), None)

    errors = []
    seen = set()
    for member in tar:
        name = validate_member(member)
        if name == MANIFEST_NAME:
            continue
        wanted = expected.get(name)
        if wanted is None:
            errors.append("unexpected member: %s" % name)
            continue
        if name in seen:
            errors.append("duplicate member: %s" % name)
            continue
        seen.add(name)
        entry_type, expected_hash, expected_size, expected_link = wanted
        if entry_type == "file":
            if not member.isfile():
                errors.append("member type mismatch: %s" % name)
                continue
            if expected_size is not None and member.size != expected_size:
                errors.append("size mismatch: %s (expected %s, got %s)"
                              % (name, expected_size, member.size))
            digest, actual_size = member_sha256(tar, member)
            if expected_hash and digest != expected_hash:
                errors.append("checksum mismatch: %s" % name)
        elif entry_type == "symlink":
            if not member.issym():
                errors.append("member type mismatch: %s" % name)
            elif expected_link is not None and member.linkname != expected_link:
                errors.append("symlink target mismatch: %s" % name)
        elif entry_type == "dir":
            if not member.isdir():
                errors.append("member type mismatch: %s" % name)
        if len(errors) >= limit_errors:
            errors.append("too many errors; stopping verification")
            break
    missing = sorted(set(expected) - seen)
    for name in missing[:limit_errors]:
        errors.append("missing member: %s" % name)
    if len(missing) > limit_errors:
        errors.append("%d more missing members" % (len(missing) - limit_errors))
    return errors
