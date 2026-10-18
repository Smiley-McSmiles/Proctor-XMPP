"""Small, dependency-free helpers used across Proctor."""

from __future__ import print_function

import errno
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

from .errors import ExternalCommandError, ProctorError, UsageError

CHUNK_SIZE = 1024 * 1024


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def isoformat(dt=None):
    if dt is None:
        dt = utcnow()
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def timestamp_id(dt=None):
    return (dt or utcnow()).strftime("%Y%m%dT%H%M%SZ")


def timestamp_slug(dt=None):
    return (dt or utcnow()).strftime("%Y%m%d-%H%M%S")


def random_suffix(length=4):
    return os.urandom(length).hex()[:length]


def human_bytes(value):
    value = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    index = 0
    while value >= 1024.0 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "%d %s" % (int(value), units[index])
    return "%.1f %s" % (value, units[index])


def parse_duration(text):
    """Parse 30s, 15m, 12h, 7d into seconds."""
    match = re.match(r"^\s*(\d+)\s*([smhdw]?)\s*$", str(text), re.I)
    if not match:
        raise UsageError("invalid duration: %s" % text)
    value = int(match.group(1))
    unit = (match.group(2) or "s").lower()
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return value * factors[unit]


def which(name):
    return shutil.which(name)


def run(command, check=True, capture=True, env=None, input_data=None, timeout=None, cwd=None):
    """Run a command and return CompletedProcess. Raises ExternalCommandError."""
    if isinstance(command, str):
        command = shlex.split(command)
    command = [str(part) for part in command]
    if not command:
        raise UsageError("empty command")
    stdout = subprocess.PIPE if capture else None
    stderr = subprocess.PIPE if capture else None
    try:
        kwargs = {
            "stdout": stdout,
            "stderr": stderr,
            "env": env,
            "timeout": timeout,
            "cwd": cwd,
        }
        if input_data is not None:
            kwargs["input"] = input_data
        proc = subprocess.run(command, **kwargs)
    except FileNotFoundError:
        raise ExternalCommandError(command, 127, "executable not found")
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(command, 124, "command timed out")
    if check and proc.returncode != 0:
        stderr_text = ""
        if proc.stderr:
            stderr_text = proc.stderr.decode("utf-8", "replace")
        raise ExternalCommandError(command, proc.returncode, stderr_text)
    return proc


def run_output(command, **kwargs):
    proc = run(command, **kwargs)
    return proc.stdout.decode("utf-8", "replace").strip()


def ensure_dir(path, mode=0o700):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        os.makedirs(path, mode=mode)
    return path


def atomic_write(path, data, mode=0o600):
    """Write bytes atomically in the same directory, fsync file and directory."""
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    ensure_dir(directory, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_dir(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def atomic_write_json(path, value, mode=0o600):
    # ensure_ascii keeps invalid-UTF-8 filesystem names (surrogate escapes)
    # representable and avoids encoding failures on unusual backups.
    payload = json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n"
    return atomic_write(path, payload.encode("utf-8"), mode=mode)


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        raise ProctorError("cannot read JSON file %s: %s" % (path, exc))


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def parse_size(text):
    """Parse 10M / 2GiB into bytes."""
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?i?b?)\s*$", str(text), re.I)
    if not match:
        raise UsageError("invalid size: %s" % text)
    value = float(match.group(1))
    unit = match.group(2).lower().rstrip("b")
    factors = {"": 1, "k": 1024, "ki": 1024, "m": 1024 ** 2, "mi": 1024 ** 2,
               "g": 1024 ** 3, "gi": 1024 ** 3, "t": 1024 ** 4, "ti": 1024 ** 4,
               "p": 1024 ** 5, "pi": 1024 ** 5}
    return int(value * factors.get(unit, 1))


def is_within(path, root):
    """True if path is root or inside root (both absolute, normalized)."""
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def relative_to_root(path, root):
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    if not is_within(path, root):
        raise ValueError("%s is not within %s" % (path, root))
    return os.path.relpath(path, root)


def sanitize_component(value):
    value = str(value).strip().replace(" ", "-")
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return value.strip("-.") or "backup"


def safe_archive_name(path):
    """Turn an absolute source path into a safe relative archive member name."""
    path = os.path.normpath(path)
    drive, tail = os.path.splitdrive(path)
    parts = []
    if drive:
        parts.append(drive.rstrip(":\\/").replace(":", "_"))
    for part in tail.split(os.sep):
        if part in ("", os.curdir):
            continue
        if part == os.pardir:
            parts.append("__parent__")
        else:
            parts.append(part)
    if path.startswith(os.sep):
        return "/".join(parts)
    return "/".join(parts)


def safe_join(root, member):
    """Join a relative archive member to root, rejecting traversal."""
    member = member.replace("\\", "/")
    if member.startswith("/") or re.match(r"^[A-Za-z]:", member):
        raise ProctorError("unsafe absolute archive member: %s" % member)
    parts = [part for part in member.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ProctorError("unsafe archive member with '..': %s" % member)
    destination = os.path.join(root, *parts)
    if not is_within(destination, root):
        raise ProctorError("archive member escapes target: %s" % member)
    return destination


def remove_symlink_or_file(path):
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except FileNotFoundError:
        pass


def copy_file_atomic(source, destination, mode=None):
    """Copy a file to destination atomically, preserving mode by default."""
    ensure_dir(os.path.dirname(destination), mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(destination))
    try:
        with os.fdopen(fd, "wb") as out_handle, open(source, "rb") as in_handle:
            shutil.copyfileobj(in_handle, out_handle, CHUNK_SIZE)
            out_handle.flush()
            os.fsync(out_handle.fileno())
        os.chmod(temporary, mode if mode is not None else stat.S_IMODE(os.stat(source).st_mode))
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def matches_any(path, patterns):
    if not patterns:
        return False
    normalized = path.replace(os.sep, "/")
    for pattern in patterns:
        pattern = pattern.replace(os.sep, "/")
        if fnmatch.fnmatch(normalized, pattern) or is_within(normalized, pattern):
            return True
    return False


class FileLock(object):
    """Advisory exclusive lock used to serialize repository mutations."""

    def __init__(self, path, timeout=30.0):
        self.path = path
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        ensure_dir(os.path.dirname(self.path), mode=0o700)
        self.handle = open(self.path, "a+")
        if fcntl is None:
            return self
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.time() >= deadline:
                    raise ProctorError(
                        "another proctor process is using the repository",
                        hint="wait for it to finish or remove %s if it is stale" % self.path,
                    )
                time.sleep(0.2)
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write("%d\n" % os.getpid())
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        return False


def disk_free(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def walk_files(root, skip_prefixes=None):
    """Yield (path, stat) for a tree without following symlinks."""
    skip_prefixes = [os.path.abspath(item) for item in (skip_prefixes or [])]
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if not matches_any(os.path.join(dirpath, name), skip_prefixes)
        )
        for name in sorted(dirnames):
            path = os.path.join(dirpath, name)
            try:
                yield path, os.lstat(path)
            except OSError:
                continue
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if matches_any(path, skip_prefixes):
                continue
            try:
                yield path, os.lstat(path)
            except OSError:
                continue


def tree_size(root, skip_prefixes=None):
    total = 0
    for path, st in walk_files(root, skip_prefixes=skip_prefixes):
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total


def gid_name(gid):
    try:
        import grp
        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        return None


def uid_name(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return None


def uid_for(name, fallback=None):
    try:
        import pwd
        return pwd.getpwnam(name).pw_uid
    except (ImportError, KeyError):
        return fallback


def gid_for(name, fallback=None):
    try:
        import grp
        return grp.getgrnam(name).gr_gid
    except (ImportError, KeyError):
        return fallback


def parse_owner_map(values):
    result = {}
    for value in values or []:
        if "=" not in value:
            raise UsageError("expected OLD=NEW, got %r" % value)
        old, new = value.split("=", 1)
        old = old.strip()
        new = new.strip()
        if not old or not new:
            raise UsageError("path mapping needs non-empty paths: %r" % value)
        old = os.path.abspath(os.path.expanduser(old))
        new = os.path.abspath(os.path.expanduser(new))
        if old == "/" or new == "/":
            raise UsageError("refusing to map the filesystem root: %r" % value)
        result[old] = new
    return result


def parse_map(values):
    return parse_owner_map(values)


def shorten(text, limit=60):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def json_dumps(value, pretty=True):
    return json.dumps(value, indent=2 if pretty else None, sort_keys=False, ensure_ascii=False)


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
