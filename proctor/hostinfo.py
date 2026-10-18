"""Operating system, init system and default-path detection."""

from __future__ import print_function

import os
import platform as py_platform
import re
import socket

from .util import read_json


class HostInfo(object):
    def __init__(self):
        self.system = py_platform.system() or os.uname()[0]
        self.system_id = self.system.lower()
        self.release = py_platform.release()
        self.machine = py_platform.machine()
        self.hostname = socket.gethostname()
        try:
            self.fqdn = socket.getfqdn()
        except Exception:
            self.fqdn = self.hostname
        self.distro_id = ""
        self.distro_name = ""
        self.distro_version = ""
        self.distro_like = []
        self.pretty_name = ""
        self.package_manager = ""
        self._detect_distro()
        self.init = detect_init(self.system_id)
        self.defaults = default_paths(self)

    def _detect_distro(self):
        if self.system_id == "linux":
            data = _read_os_release()
            self.distro_id = data.get("ID", "").lower()
            self.distro_name = data.get("NAME", "")
            self.distro_version = data.get("VERSION_ID", "")
            self.pretty_name = data.get("PRETTY_NAME", self.distro_name)
            like = data.get("ID_LIKE", "")
            self.distro_like = [part.lower() for part in like.split() if part]
            self.package_manager = _detect_package_manager(self.distro_id, self.distro_like)
        elif self.system_id in ("freebsd", "dragonfly"):
            self.distro_id = self.system_id
            self.distro_name = self.system
            self.distro_version = self.release
            self.pretty_name = "%s %s" % (self.system, self.release)
            self.package_manager = "pkg"
        elif self.system_id == "openbsd":
            self.distro_id = "openbsd"
            self.distro_name = "OpenBSD"
            self.distro_version = self.release
            self.pretty_name = "OpenBSD %s" % self.release
            self.package_manager = "pkg_add"
        elif self.system_id == "netbsd":
            self.distro_id = "netbsd"
            self.distro_name = "NetBSD"
            self.distro_version = self.release
            self.pretty_name = "NetBSD %s" % self.release
            self.package_manager = "pkgin"
        elif self.system_id == "darwin":
            self.distro_id = "darwin"
            self.distro_name = "macOS"
            self.distro_version = py_platform.mac_ver()[0]
            self.pretty_name = "macOS %s" % self.distro_version
            self.package_manager = "brew"

    def family(self):
        """Return a normalized family used for defaults."""
        if self.system_id != "linux":
            return self.system_id
        ids = [self.distro_id] + self.distro_like
        groups = {
            "debian": ("debian", "ubuntu", "mint", "pop", "kali", "raspbian", "devuan", "elementary", "zorin", "mx"),
            "fedora": ("fedora", "rhel", "centos", "rocky", "alma", "amzn", "oracle", "scientific"),
            "suse": ("opensuse", "suse", "sles", "sled"),
            "arch": ("arch", "manjaro", "endeavouros", "artix", "garuda"),
            "alpine": ("alpine",),
            "void": ("void",),
            "gentoo": ("gentoo", "funtoo"),
        }
        for family, names in groups.items():
            if any(item in names for item in ids):
                return family
        return self.distro_id or "linux"

    def is_bsd(self):
        return self.system_id in ("freebsd", "openbsd", "netbsd", "dragonfly")

    def as_dict(self):
        return {
            "system": self.system,
            "system_id": self.system_id,
            "family": self.family(),
            "distro_id": self.distro_id,
            "distro_name": self.distro_name,
            "distro_version": self.distro_version,
            "pretty_name": self.pretty_name,
            "package_manager": self.package_manager,
            "kernel": self.release,
            "machine": self.machine,
            "hostname": self.hostname,
            "fqdn": self.fqdn,
            "init": self.init,
            "defaults": self.defaults,
            "python": py_platform.python_version(),
        }


def _read_os_release():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        if not os.path.isfile(path):
            continue
        data = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                        value = value[1:-1]
                    data[key.strip()] = value
        except OSError:
            continue
        if data:
            return data
    return {}


def _detect_package_manager(distro_id, distro_like):
    ids = [distro_id] + list(distro_like)
    if any(item in ids for item in ("debian", "ubuntu", "mint", "pop", "kali", "raspbian", "devuan")):
        return "apt"
    if any(item in ids for item in ("fedora", "rhel", "centos", "rocky", "alma", "amzn", "oracle")):
        return "dnf"
    if any(item in ids for item in ("opensuse", "suse", "sles")):
        return "zypper"
    if any(item in ids for item in ("arch", "manjaro", "endeavouros", "artix")):
        return "pacman"
    if "alpine" in ids:
        return "apk"
    if "void" in ids:
        return "xbps"
    if any(item in ids for item in ("gentoo", "funtoo")):
        return "emerge"
    for binary, manager in (("apt", "apt"), ("dnf", "dnf"), ("yum", "yum"), ("zypper", "zypper"),
                            ("pacman", "pacman"), ("apk", "apk"), ("xbps-install", "xbps"),
                            ("emerge", "emerge")):
        if _has(binary):
            return manager
    return ""


def _has(binary):
    from shutil import which
    return which(binary) is not None


def _pid1_comm():
    try:
        with open("/proc/1/comm", "r", errors="replace") as handle:
            return handle.read().strip().lower()
    except OSError:
        return ""


def detect_init(system_id=None):
    """Return a normalized init/service manager identifier."""
    system_id = system_id or (py_platform.system() or "").lower()
    override = os.environ.get("PROCTOR_SERVICE_MANAGER", "").strip().lower()
    if override:
        return override
    if system_id in ("freebsd", "dragonfly"):
        return "rc"
    if system_id == "openbsd":
        return "rcctl"
    if system_id == "netbsd":
        return "rcd"
    if system_id == "darwin":
        return "launchd"
    if system_id != "linux":
        return "unknown"

    pid1 = _pid1_comm()
    if pid1 == "systemd" or (os.path.isdir("/run/systemd/system") and _has("systemctl")):
        return "systemd"
    if pid1 == "runit" or (os.path.isdir("/run/runit") and _has("sv")):
        return "runit"
    if _has("rc-service") and (os.path.isdir("/run/openrc") or os.path.isdir("/etc/runlevels")):
        return "openrc"
    if _has("s6-svscan") or os.path.isdir("/run/service"):
        return "s6"
    if _has("sv") and (os.path.isdir("/etc/sv") or os.path.isdir("/var/service") or os.path.isdir("/run/service")):
        return "runit"
    if _has("rc-service"):
        return "openrc"
    if _has("service"):
        return "sysv"
    return "unknown"


def default_paths(host):
    """Distro/BSD-aware Prosody defaults."""
    system = host.system_id
    family = host.family()
    if system in ("freebsd", "dragonfly"):
        config_dir = "/usr/local/etc/prosody"
        data_dir = "/var/db/prosody"
        log_dir = "/var/log/prosody"
        user, group = "prosody", "prosody"
    elif system == "openbsd":
        config_dir = "/etc/prosody"
        data_dir = "/var/prosody"
        log_dir = "/var/log/prosody"
        user, group = "_prosody", "_prosody"
    elif system == "netbsd":
        config_dir = "/usr/pkg/etc/prosody"
        data_dir = "/var/db/prosody"
        log_dir = "/var/log/prosody"
        user, group = "prosody", "prosody"
    elif system == "darwin":
        config_dir = "/usr/local/etc/prosody"
        data_dir = "/usr/local/var/lib/prosody"
        log_dir = "/usr/local/var/log/prosody"
        user, group = "prosody", "prosody"
    else:
        config_dir = "/etc/prosody"
        data_dir = "/var/lib/prosody"
        log_dir = "/var/log/prosody"
        user, group = "prosody", "prosody"
    return {
        "config_dir": config_dir,
        "config_file": os.path.join(config_dir, "prosody.cfg.lua"),
        "data_dir": data_dir,
        "log_dir": log_dir,
        "cert_dir": os.path.join(config_dir, "certs"),
        "user": user,
        "group": group,
        "service": "prosody",
        "family": family,
    }


def config_candidates(host):
    """Ordered config file candidates for this platform."""
    candidates = []
    env = os.environ.get("PROSODY_CONFIG")
    if env:
        candidates.append(env)
    candidates.extend([
        host.defaults["config_file"],
        "/etc/prosody/prosody.cfg.lua",
        "/usr/local/etc/prosody/prosody.cfg.lua",
        "/usr/pkg/etc/prosody/prosody.cfg.lua",
        "/opt/prosody/etc/prosody/prosody.cfg.lua",
        "/etc/prosody.cfg.lua",
    ])
    seen = set()
    result = []
    for item in candidates:
        item = os.path.abspath(os.path.expanduser(item))
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def find_config(host):
    for candidate in config_candidates(host):
        if os.path.isfile(candidate):
            return candidate
    return None


def derive_data_dir(host, config_dir):
    """Best-effort default data dir for a config located outside /etc."""
    config_dir = os.path.abspath(config_dir)
    if config_dir in ("/etc/prosody", "/etc/prosody/conf.d"):
        return host.defaults["data_dir"]
    if config_dir.startswith("/usr/local/etc/prosody"):
        if host.system_id in ("freebsd", "dragonfly", "netbsd"):
            return "/var/db/prosody"
        return "/usr/local/var/lib/prosody"
    if config_dir.startswith("/usr/pkg/etc/prosody"):
        return "/var/db/prosody"
    if config_dir.startswith("/opt/prosody"):
        return "/opt/prosody/var/lib/prosody"
    return host.defaults["data_dir"]


def derive_log_dir(host, config_dir):
    if host.is_bsd() or os.path.abspath(config_dir) == "/etc/prosody":
        return host.defaults["log_dir"]
    if config_dir.startswith("/usr/local/etc/prosody"):
        return "/usr/local/var/log/prosody"
    if config_dir.startswith("/opt/prosody"):
        return "/opt/prosody/var/log/prosody"
    return host.defaults["log_dir"]
