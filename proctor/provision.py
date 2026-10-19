"""Interactive provisioning for Prosody, nginx/Apache and certbot.

This module is deliberately explicit: package-manager commands are real, so
every install path is guarded by a confirmation (or an explicit CLI flag), and
`--dry-run` only prints what would happen.
"""

from __future__ import print_function

import getpass
import glob
import os
import re
import shlex
import sys
import time

from .errors import Aborted, ConfigError, ExternalCommandError, PermissionDenied, ProctorError
from .hostinfo import find_config
from .prosody import detect_prosody
from .service import ServiceController
from .util import (
    atomic_write, ensure_dir, gid_for, human_bytes, is_root, run, uid_for, which,
)

CERTBOT_ROOT = "/etc/letsencrypt"
CERTBOT_LIVE = os.path.join(CERTBOT_ROOT, "live")
CERTBOT_ARCHIVE = os.path.join(CERTBOT_ROOT, "archive")
CERTBOT_RENEWAL = os.path.join(CERTBOT_ROOT, "renewal")
WEBROOT = "/var/www/html"
PROXY_PATHS = ("/http-bind", "/xmpp-websocket", "/upload", "/.well-known/host-meta")


class Prompter(object):
    """Prompt using stderr so stdout remains usable for JSON output."""

    def __init__(self, assume_yes=False, interactive=None, stream=None):
        self.assume_yes = bool(assume_yes)
        if interactive is None:
            interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        self.interactive = bool(interactive)
        self.stream = stream or sys.stderr

    def _readline(self, message):
        if not self.interactive:
            raise Aborted(
                "interactive answer required; pass the relevant option or --yes"
            )
        self.stream.write(message)
        self.stream.flush()
        line = sys.stdin.readline()
        if line == "":
            raise Aborted("input closed while waiting for an answer")
        return line.strip()

    def yes_no(self, question, default=True):
        if self.assume_yes:
            return default
        suffix = " [Y/n] " if default else " [y/N] "
        answer = self._readline(question + suffix).lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def choice(self, question, choices, default=None):
        """choices: list of (value, label)."""
        if self.assume_yes:
            if default is None:
                raise ConfigError("no default for: %s" % question)
            return default
        lines = [question]
        for index, (value, label) in enumerate(choices, 1):
            marker = " (default)" if value == default else ""
            lines.append("  %d) %s%s" % (index, label, marker))
        answer = self._readline("\n".join(lines) + "\n> ").strip()
        if not answer:
            if default is None:
                raise Aborted("a choice is required for: %s" % question)
            return default
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(choices):
                return choices[index - 1][0]
        for value, _label in choices:
            if answer.lower() == str(value).lower():
                return value
        raise ConfigError("invalid choice: %s" % answer)

    def text(self, question, default=None, required=True):
        if self.assume_yes and default is not None:
            return default
        suffix = " [%s] " % default if default else " "
        answer = self._readline(question + suffix)
        if not answer:
            if default is not None:
                return default
            if required:
                raise Aborted("a value is required for: %s" % question)
            return ""
        return answer

    def password(self, question):
        if self.assume_yes or not self.interactive:
            return None
        try:
            return getpass.getpass(question + " ", stream=self.stream)
        except (EOFError, KeyboardInterrupt):
            return None


class PackageInstaller(object):
    _refreshed = set()

    def __init__(self, host, reporter, dry_run=False):
        self.host = host
        self.reporter = reporter
        self.dry_run = dry_run

    def refresh_command(self):
        """Return the package-index refresh command, or None if not needed."""
        manager = self.host.package_manager
        commands = {
            "apt": ["apt-get", "update"],
            "dnf": ["dnf", "makecache"],
            "yum": ["yum", "makecache"],
            "zypper": ["zypper", "--non-interactive", "refresh"],
            "apk": ["apk", "update"],
            "xbps": ["xbps-install", "-S"],
        }
        return commands.get(manager)

    def _refresh(self, env):
        command = self.refresh_command()
        if not command:
            return
        manager = self.host.package_manager
        if manager in self._refreshed:
            return
        # Mark it before running: a broken mirror should not make Proctor try
        # the same slow refresh for Prosody, the web server and certbot.
        self._refreshed.add(manager)
        self.reporter.step("Refreshing package metadata: %s" % " ".join(command))
        try:
            proc = run(command, check=False, env=env, timeout=None)
        except Exception as exc:
            self.reporter.warn("package metadata refresh failed: %s" % exc)
            return
        if proc.returncode != 0:
            output = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
                "utf-8", "replace"
            ).strip()
            self.reporter.warn(
                "package metadata refresh failed (continuing with cached indexes): %s"
                % (output.splitlines()[-1] if output else "exit %d" % proc.returncode)
            )

    def command(self, packages):
        packages = list(dict.fromkeys(packages))
        manager = self.host.package_manager
        if manager == "apt":
            return ["apt-get", "install", "-y"] + packages
        if manager == "dnf":
            return ["dnf", "install", "-y"] + packages
        if manager == "yum":
            return ["yum", "install", "-y"] + packages
        if manager == "zypper":
            return ["zypper", "--non-interactive", "install"] + packages
        if manager == "pacman":
            return ["pacman", "-S", "--noconfirm", "--needed"] + packages
        if manager == "apk":
            return ["apk", "add", "--no-cache"] + packages
        if manager == "xbps":
            return ["xbps-install", "-Sy"] + packages
        if manager == "emerge":
            return ["emerge", "--ask=n", "--quiet"] + packages
        if manager == "pkg":
            return ["pkg", "install", "-y"] + packages
        if manager == "pkg_add":
            return ["pkg_add"] + packages
        if manager == "pkgin":
            return ["pkgin", "-y", "install"] + packages
        if manager == "brew":
            return ["brew", "install"] + packages
        raise ConfigError(
            "cannot determine a package manager for %s" % self.host.pretty_name,
            hint="install the packages manually, or add provision.package_manager "
                 "and provision.packages to the proctor config",
        )

    def install(self, packages, why="required packages"):
        if not packages:
            return {"packages": [], "command": None}
        command = self.command(packages)
        printable = shlex.join(command)
        if self.dry_run:
            self.reporter.info("Would install %s: %s" % (why, printable))
            return {"packages": packages, "command": command, "dry_run": True}
        if not is_root():
            raise PermissionDenied(
                "installing %s requires root" % why,
                hint="run proctor with sudo/doas, or install packages manually",
            )
        self.reporter.step("Installing %s: %s" % (why, " ".join(packages)))
        env = dict(os.environ)
        if self.host.package_manager == "apt":
            env["DEBIAN_FRONTEND"] = "noninteractive"
        self._refresh(env)
        try:
            run(command, env=env, timeout=None)
        except ExternalCommandError as exc:
            raise ConfigError(
                "package installation failed: %s" % exc,
                hint="run the package manager manually, update its metadata, "
                     "or install the packages and re-run proctor",
            )
        return {"packages": packages, "command": command, "dry_run": False}


class WebServerInfo(object):
    def __init__(self):
        self.kind = None
        self.installed = False
        self.binary = None
        self.config_root = None
        self.main_config = None
        self.conf_dir = None
        self.sites_available = None
        self.sites_enabled = None
        self.service = None
        self.test_command = None
        self.running = None
        self.proxied = False
        self.proxy_paths = []
        self.certbot = False
        self.cert_names = []
        self.domains = []

    def as_dict(self):
        return {
            "kind": self.kind,
            "installed": self.installed,
            "binary": self.binary,
            "config_root": self.config_root,
            "main_config": self.main_config,
            "conf_dir": self.conf_dir,
            "sites_available": self.sites_available,
            "sites_enabled": self.sites_enabled,
            "service": self.service,
            "running": self.running,
            "proxied": self.proxied,
            "proxy_paths": self.proxy_paths,
            "certbot": self.certbot,
            "cert_names": self.cert_names,
            "domains": self.domains,
        }


def prosody_installed(host):
    # A config file alone does not mean the package is installed. This matters
    # on a fresh target where an archive may already have been unpacked.
    return bool(which("prosodyctl") or which("prosody"))


def detect_webserver(host):
    nginx = _detect_nginx(host)
    apache = _detect_apache(host)
    candidates = [item for item in (nginx, apache) if item and (item.installed or item.config_root)]
    if not candidates:
        return None
    for item in candidates:
        controller = ServiceController(host, item.service or item.kind)
        if controller.available:
            try:
                item.running = controller.is_active()
            except ProctorError:
                item.running = None
        _enrich_webserver(item)
    for item in candidates:
        if item.proxied and item.running:
            return item
    for item in candidates:
        if item.proxied:
            return item
    for item in candidates:
        if item.running:
            return item
    return candidates[0]


def _detect_nginx(host):
    roots = [
        "/etc/nginx",
        "/usr/local/etc/nginx",
        "/usr/pkg/etc/nginx",
        "/opt/local/etc/nginx",
    ]
    root = next((item for item in roots if os.path.isdir(item)), None)
    binary = which("nginx")
    if not root and binary:
        root = "/etc/nginx"
    if not root:
        return None
    info = WebServerInfo()
    info.kind = "nginx"
    info.installed = bool(binary)
    info.binary = binary or "nginx"
    info.config_root = root
    info.main_config = os.path.join(root, "nginx.conf")
    info.conf_dir = os.path.join(root, "conf.d") if os.path.isdir(os.path.join(root, "conf.d")) else None
    info.sites_available = os.path.join(root, "sites-available") if os.path.isdir(os.path.join(root, "sites-available")) else None
    info.sites_enabled = os.path.join(root, "sites-enabled") if os.path.isdir(os.path.join(root, "sites-enabled")) else None
    info.service = "nginx"
    info.test_command = [info.binary, "-t"]
    return info


def _detect_apache(host):
    family = host.family()
    if host.system_id == "openbsd" and not which("apachectl"):
        return None
    if family == "debian":
        root = "/etc/apache2"
        service = "apache2"
    elif family in ("fedora", "suse", "arch"):
        root = "/etc/httpd"
        service = "httpd"
    elif family == "alpine":
        root = "/etc/apache2"
        service = "apache2"
    elif family == "void":
        root = "/etc/apache2"
        service = "apache2"
    elif host.system_id in ("freebsd", "dragonfly"):
        root = "/usr/local/etc/apache24"
        service = "apache24"
    elif host.system_id == "netbsd":
        root = "/usr/pkg/etc/httpd"
        service = "apache"
    else:
        root = "/etc/apache2"
        service = "apache2"
    binary = which("apache2ctl") or which("apachectl") or which("httpd")
    if not os.path.isdir(root) and not binary:
        return None
    info = WebServerInfo()
    info.kind = "apache"
    info.installed = bool(which("apache2ctl") or which("apachectl") or which("httpd"))
    info.binary = binary or "apachectl"
    info.config_root = root
    info.main_config = os.path.join(root, "apache2.conf" if family == "debian" else "httpd.conf")
    info.conf_dir = os.path.join(root, "conf.d") if os.path.isdir(os.path.join(root, "conf.d")) else None
    info.sites_available = os.path.join(root, "sites-available") if os.path.isdir(os.path.join(root, "sites-available")) else None
    info.sites_enabled = os.path.join(root, "sites-enabled") if os.path.isdir(os.path.join(root, "sites-enabled")) else None
    info.service = service
    if which("apache2ctl"):
        info.test_command = ["apache2ctl", "configtest"]
    elif which("apachectl"):
        info.test_command = ["apachectl", "configtest"]
    elif which("httpd"):
        info.test_command = ["httpd", "-t"]
    else:
        info.test_command = [info.binary, "-t"]
    return info


def _enrich_webserver(info):
    if not info:
        return
    paths = set()
    files = []
    if info.main_config and os.path.isfile(info.main_config):
        files.append(info.main_config)
    for directory in (info.conf_dir, info.sites_available, info.sites_enabled):
        if directory and os.path.isdir(directory):
            for name in sorted(os.listdir(directory))[:100]:
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    files.append(path)
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for needle, label in (
                ("http-bind", "/http-bind"),
                ("xmpp-websocket", "/xmpp-websocket"),
                ("/upload", "/upload"),
                ("5280", None)):
            if needle in text:
                if label:
                    paths.add(label)
                else:
                    info.proxied = True
        if "http-bind" in text or "xmpp-websocket" in text or "5280" in text:
            info.proxied = True
    info.proxy_paths = sorted(paths)
    _enrich_letsencrypt(info)


def _enrich_letsencrypt(info):
    meta = letsencrypt_metadata()
    info.certbot = bool(meta["cert_names"])
    info.cert_names = meta["cert_names"]
    info.domains = meta["domains"]


def letsencrypt_metadata():
    result = {"present": False, "cert_names": [], "domains": [],
              "authenticator": None, "webroot_path": None}
    for path in sorted(glob.glob(os.path.join(CERTBOT_RENEWAL, "*.conf"))):
        data = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key.strip()] = value.strip()
        except OSError:
            continue
        name = os.path.basename(path)[:-5]
        if data.get("cert_name"):
            name = data["cert_name"]
        result["cert_names"].append(name)
        if data.get("domains"):
            result["domains"].extend(
                item.strip() for item in data["domains"].split(",") if item.strip()
            )
        result["authenticator"] = data.get("authenticator") or result["authenticator"]
        result["webroot_path"] = data.get("webroot_path") or result["webroot_path"]
        result["present"] = True
    result["cert_names"] = sorted(set(result["cert_names"]))
    result["domains"] = sorted(set(result["domains"]))
    if os.path.isdir(CERTBOT_LIVE):
        for name in os.listdir(CERTBOT_LIVE):
            if os.path.isfile(os.path.join(CERTBOT_LIVE, name, "fullchain.pem")):
                result["cert_names"].append(name)
                result["present"] = True
    result["cert_names"] = sorted(set(result["cert_names"]))
    return result


def web_packages(host, kind):
    family = host.family()
    if kind == "nginx":
        packages = ["www-servers/nginx"] if family == "gentoo" else ["nginx"]
    elif kind == "apache":
        if host.system_id == "openbsd":
            return []
        if family == "debian" or family == "alpine":
            packages = ["apache2"]
        elif family in ("fedora", "suse"):
            packages = ["httpd"]
            packages.append("mod_ssl")
        elif family == "arch":
            packages = ["apache"]
        elif family == "void":
            packages = ["apache"]
        elif family == "gentoo":
            packages = ["www-servers/apache"]
        elif host.system_id in ("freebsd", "dragonfly"):
            packages = ["apache24"]
        elif host.system_id == "netbsd":
            packages = ["apache24"]
        else:
            packages = ["apache2"]
    else:
        return []
    return packages


def certbot_packages(host):
    if host.package_manager == "emerge":
        return ["app-crypt/certbot"]
    if host.hostname and host.system_id in ("freebsd", "netbsd"):
        # Ports package names are Python-versioned on some releases.
        return ["certbot"]
    packages = ["certbot"]
    # POSIX ACLs let us grant Prosody access without making Certbot's tree
    # world-readable. Debian/Ubuntu package setfacl in 'acl'.
    if host.family() == "debian":
        packages.append("acl")
    return packages


def prosody_packages(host):
    if host.package_manager == "emerge":
        return ["net-im/prosody"]
    return ["prosody"]


# ---------------------------------------------------------------------------
# Provisioning options and layout

class ProvisionOptions(object):
    def __init__(self, **kwargs):
        self.install_missing = kwargs.get("install_missing", None)
        self.webserver = kwargs.get("webserver", "auto")
        self.certbot = kwargs.get("certbot", "auto")
        self.email = kwargs.get("email")
        self.domains = list(kwargs.get("domains", []) or [])
        self.admin = kwargs.get("admin")
        self.admin_password = kwargs.get("admin_password")
        self.proxy_http = kwargs.get("proxy_http", None)
        self.storage = kwargs.get("storage")
        self.start = kwargs.get("start", True)
        self.dry_run = kwargs.get("dry_run", False)
        self.assume_yes = kwargs.get("assume_yes", False)
        self.force = kwargs.get("force", False)
        self.http_port = kwargs.get("http_port", 5280)
        self.http_interface = kwargs.get("http_interface", "*")
        self.firewall = kwargs.get("firewall", "auto")


class ServerLayout(object):
    def __init__(self):
        self.config_file = None
        self.config_dir = None
        self.data_dir = None
        self.log_dir = None
        self.cert_dirs = []
        self.user = "prosody"
        self.group = "prosody"
        self.service_name = "prosody"
        self.storage = "internal_hashed"
        self.sql = {}
        self.version = None

    def as_dict(self):
        return {
            "config_file": self.config_file,
            "config_dir": self.config_dir,
            "data_dir": self.data_dir,
            "log_dir": self.log_dir,
            "cert_dirs": self.cert_dirs,
            "user": self.user,
            "group": self.group,
            "service": self.service_name,
            "storage": self.storage,
            "version": self.version,
        }


def layout_for_host(config, install=None):
    layout = ServerLayout()
    defaults = config.host.defaults
    if install is None:
        try:
            install = detect_prosody(config.host, overrides={
                "config": config.prosody.get("config"),
                "data_dir": config.prosody.get("data_dir"),
                "log_dir": config.prosody.get("log_dir"),
                "cert_dirs": config.prosody.get("cert_dirs"),
                "plugin_dirs": config.prosody.get("plugin_dirs"),
                "user": config.prosody.get("user"),
                "group": config.prosody.get("group"),
                "service": config.prosody.get("service"),
            }, require=False)
        except ProctorError:
            install = None
    if install is not None:
        layout.config_file = install.config_file
        layout.config_dir = install.config_dir
        layout.data_dir = install.data_dir
        layout.log_dir = install.log_dir
        layout.cert_dirs = list(install.cert_dirs)
        layout.user = install.user
        layout.group = install.group
        layout.service_name = install.service_name
        layout.storage = install.storage
        layout.sql = install.sql
        layout.version = install.version
        return layout
    layout.config_file = os.path.abspath(os.path.expanduser(
        config.prosody.get("config") or defaults["config_file"]
    ))
    layout.config_dir = os.path.dirname(layout.config_file)
    layout.data_dir = os.path.abspath(os.path.expanduser(
        config.prosody.get("data_dir") or defaults["data_dir"]
    ))
    layout.log_dir = os.path.abspath(os.path.expanduser(
        config.prosody.get("log_dir") or defaults["log_dir"]
    ))
    layout.cert_dirs = list(config.prosody.get("cert_dirs") or [
        os.path.join(layout.config_dir, "certs")
    ])
    layout.user = config.prosody.get("user") or defaults.get("user") or "prosody"
    layout.group = config.prosody.get("group") or defaults.get("group") or "prosody"
    layout.service_name = config.prosody.get("service") or defaults.get("service") or "prosody"
    return layout


def _controller_for(config, name):
    return ServiceController(config.host, name=name, config={
        "manager": config.prosody.get("service_manager", "auto"),
        "commands": config.prosody.get("commands", {}),
    })


def _web_controller(config, name):
    return ServiceController(config.host, name=name, config={
        "manager": config.prosody.get("service_manager", "auto"),
    })


# ---------------------------------------------------------------------------
# Prosody / web server installation

def ensure_prosody(config, options, reporter, prompter):
    host = config.host
    if prosody_installed(host):
        return {"installed": True, "new": False, "planned": False}
    if options.dry_run:
        reporter.info("Would install the Prosody package (%s)" % host.package_manager)
        return {"installed": False, "new": False, "planned": True}
    if options.install_missing is False:
        return {"installed": False, "declined": True, "planned": False}
    if options.install_missing is None:
        if not prompter.interactive:
            return {"installed": False, "declined": True, "planned": False}
        if not prompter.yes_no(
                "Prosody is not installed on this host. Install the %s package now?"
                % host.package_manager, default=True):
            return {"installed": False, "declined": True, "planned": False}
    installer = PackageInstaller(host, reporter, dry_run=options.dry_run)
    installer.install(prosody_packages(host), why="Prosody")
    if not prosody_installed(host):
        raise ProctorError(
            "Prosody was installed but could not be detected",
            hint="check the package manager output and whether prosodyctl is in PATH",
        )
    return {"installed": True, "new": True, "planned": False}


def pick_web_kind(existing, prompter):
    if existing is not None and existing.installed and existing.kind in ("nginx", "apache"):
        return existing.kind
    if prompter.interactive:
        value = prompter.choice(
            "Which web server should front Prosody?",
            [("nginx", "nginx (recommended)"), ("apache", "Apache")],
            default="nginx",
        )
        return value
    return "nginx"


def choose_webserver(source_kind, options, prompter, existing=None, default_none=False):
    value = options.webserver or "auto"
    if value in ("none", "nginx", "apache"):
        return None if value == "none" else value
    if value != "auto":
        raise ConfigError("webserver must be auto, none, nginx or apache")
    if source_kind in ("nginx", "apache"):
        return source_kind
    if default_none:
        if not prompter.interactive:
            return None
        kind = prompter.choice(
            "The backup does not describe a web server. Set one up?",
            [("none", "no web server (XMPP only)"),
             ("nginx", "nginx"),
             ("apache", "Apache")],
            default="none",
        )
        return None if kind == "none" else kind
    default_kind = "nginx"
    if existing is not None and existing.installed and existing.kind in ("nginx", "apache"):
        default_kind = existing.kind
    if prompter.interactive:
        kind = prompter.choice(
            "Which web server should front Prosody's HTTP/BOSH/WebSocket endpoints?",
            [("nginx", "nginx (recommended)"), ("apache", "Apache"),
             ("none", "none (XMPP only)")],
            default=default_kind,
        )
        return None if kind == "none" else kind
    return default_kind


def ensure_webserver(config, kind, options, reporter, prompter):
    if not kind or kind == "none":
        return None
    host = config.host
    info = detect_webserver(host)
    explicit = (options.webserver or "auto") in ("nginx", "apache")
    if info is not None and info.installed and info.kind != kind and not explicit:
        if prompter.interactive:
            use_existing = prompter.yes_no(
                "%s is already installed. Use it instead of installing %s?"
                % (info.kind, kind), default=True)
            if use_existing:
                kind = info.kind
        else:
            kind = info.kind
    if info is None or info.kind != kind or not info.installed:
        if options.install_missing is False:
            reporter.warn("%s is not installed; web proxy setup skipped" % kind)
            return info if info is not None and info.kind == kind else None
        if options.install_missing is None and not prompter.interactive:
            reporter.warn(
                "%s is not installed; pass --install-missing to install it automatically"
                % kind
            )
            return info if info is not None and info.kind == kind else None
        if options.dry_run:
            reporter.info("Would install %s: %s"
                          % (kind, " ".join(web_packages(host, kind))))
            return None
        packages = web_packages(host, kind)
        if not packages:
            raise ConfigError("do not know the %s package name for %s"
                              % (kind, host.pretty_name))
        installer = PackageInstaller(host, reporter, dry_run=False)
        if options.install_missing is None and prompter.interactive:
            if not prompter.yes_no(
                    "Install %s (%s) now?" % (kind, ", ".join(packages)),
                    default=True):
                return None
        installer.install(packages, why=kind)
        info = detect_webserver(host)
        if info is None or info.kind != kind:
            raise ProctorError(
                "%s was installed but could not be detected" % kind,
                hint="check that its config directory exists and the binary is in PATH",
            )
    return info


def ensure_certbot(config, options, reporter, prompter):
    if which("certbot"):
        _enable_certbot_timer(config.host)
        return True
    if options.install_missing is False:
        reporter.warn("certbot is not installed; certificate issuance will fail")
        return False
    if options.install_missing is None and not prompter.interactive:
        reporter.warn(
            "certbot is not installed; pass --install-missing to install it automatically"
        )
        return False
    if options.dry_run:
        reporter.info("Would install certbot: %s" % " ".join(certbot_packages(config.host)))
        return False
    installer = PackageInstaller(config.host, reporter, dry_run=False)
    if options.install_missing is None and prompter.interactive:
        if not prompter.yes_no("Install certbot now?", default=True):
            return False
    installer.install(certbot_packages(config.host), why="certbot")
    _enable_certbot_timer(config.host)
    return bool(which("certbot"))


def _enable_certbot_timer(host):
    if host.init != "systemd" or not which("systemctl"):
        return
    # Most packages ship a certbot.timer (or a cron job). Enable the timer when
    # present; failure is harmless for cron-based renewals.
    proc = run(["systemctl", "list-unit-files", "certbot.timer"], check=False)
    output = (proc.stdout or b"").decode("utf-8", "replace")
    if "certbot.timer" in output:
        run(["systemctl", "enable", "--now", "certbot.timer"], check=False)


def firewall_ports(proxy, tls, http_port=5280, https_port=5281):
    """Ports an XMPP host needs exposed, based on its web-server shape."""
    ports = {5222, 5269}
    if proxy:
        ports.add(80)
        if tls:
            ports.add(443)
    else:
        ports.add(int(http_port))
        if tls:
            ports.add(int(https_port))
    return sorted(ports)


def ensure_firewall(host, ports, options, reporter, prompter):
    """Offer to open ports in an already-active UFW/firewalld setup.

    Proctor never edits iptables/nftables directly and never changes a firewall
    during a non-interactive run unless --firewall yes was passed explicitly.
    """
    value = getattr(options, "firewall", "auto") or "auto"
    if value not in ("auto", "yes", "no"):
        raise ConfigError("firewall must be auto, yes or no")
    if value == "no" or not ports:
        return {"status": "skipped", "backend": None, "ports": list(ports)}

    backend = None
    active = False
    if which("ufw"):
        proc = run(["ufw", "status"], check=False)
        text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        if "Status: active" in text:
            backend = "ufw"
            active = True
            commands = [["ufw", "allow", "%d/tcp" % port] for port in ports]
    if not active and which("firewall-cmd"):
        proc = run(["firewall-cmd", "--state"], check=False)
        text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        if "running" in text:
            backend = "firewalld"
            active = True
            commands = [["firewall-cmd", "--permanent", "--add-port=%d/tcp" % port]
                        for port in ports]
            commands.append(["firewall-cmd", "--reload"])
    if not active:
        return {"status": "inactive", "backend": None, "ports": sorted(ports)}

    if value == "auto":
        if not prompter.interactive or prompter.assume_yes:
            return {
                "status": "skipped",
                "backend": backend,
                "ports": sorted(ports),
                "hint": "firewall is active; pass --firewall yes to open these ports",
            }
        if not prompter.yes_no(
                "%s is active. Open %s for XMPP/HTTP?" % (
                    backend, ", ".join(str(port) for port in sorted(ports))),
                default=True):
            return {"status": "skipped", "backend": backend,
                    "ports": sorted(ports)}

    reporter.step("Opening firewall ports with %s: %s" % (
        backend, ", ".join(str(port) for port in sorted(ports))))
    failed = []
    for command in commands:
        proc = run(command, check=False)
        if proc.returncode != 0:
            failed.append(" ".join(command))
    if failed:
        reporter.warn("could not update %s: %s" % (backend, "; ".join(failed)))
        return {"status": "failed", "backend": backend,
                "ports": sorted(ports), "failed": failed}
    return {"status": "opened", "backend": backend, "ports": sorted(ports)}


def _wanted_certbot(source_le, source_web, options, prompter, setup=False):
    value = options.certbot or "auto"
    if value == "no":
        return False
    if value == "yes":
        return True
    if value != "auto":
        raise ConfigError("certbot must be auto, yes or no")
    if source_le and source_le.get("present"):
        return True
    if source_web and source_web.get("certbot"):
        return True
    if setup:
        if not prompter.interactive or prompter.assume_yes:
            # A non-interactive `setup --yes` must not silently contact
            # Let's Encrypt; require --certbot yes and --email.
            return False
        return prompter.yes_no(
            "Obtain a Let's Encrypt certificate with certbot?", default=True)
    if prompter.interactive:
        return prompter.yes_no(
            "Install certbot / obtain TLS certificates on this host?", default=True)
    return False


def prepare_restore(config, manifest, options, reporter, prompter):
    host = config.host
    source = manifest.get("source") or {}
    source_prosody = source.get("prosody") or {}
    source_web = source.get("webserver") or {}
    source_le = source.get("letsencrypt") or {}
    domains = (normalize_domains(options.domains) if options.domains
               else (source_prosody.get("virtualhosts") or []))
    result = {
        "prosody_installed": prosody_installed(host),
        "prosody_new": False,
        "web": None,
        "web_dict": None,
        "certbot": False,
        "issue_certbot": False,
        "domains": list(domains),
        "cert_name": (_safe_cert_name(options.domains[0]) if options.domains
                       else _choose_cert_name(source_le, domains, source_prosody)),
        "proxy": None,
        "proxy_paths": list(source_web.get("proxy_paths") or PROXY_PATHS),
        "backend_host": _backend_host(source_prosody),
        "backend_port": _backend_port(source_prosody),
        "skipped": False,
    }
    if options.dry_run:
        result["plan"] = _restore_plan(config, options, source)
        return result
    prosody_action = ensure_prosody(config, options, reporter, prompter)
    result["prosody_new"] = prosody_action.get("new", False)
    result["prosody_installed"] = prosody_action.get("installed", False)
    if not prosody_action.get("installed"):
        result["skipped"] = True
        if prosody_action.get("declined"):
            reporter.warn("Prosody is not installed; files will be restored without a service")
        return result

    existing = detect_webserver(host)
    source_kind = source_web.get("kind")
    auto_web = (options.webserver or "auto") == "auto"
    if auto_web and source_kind in ("nginx", "apache") and not source_web.get("proxied"):
        # The source web server existed but was not proxying XMPP; don't
        # reconfigure an unrelated web server silently.
        kind = None
        if options.proxy_http is True:
            kind = source_kind
    else:
        kind = choose_webserver(source_kind, options, prompter,
                                existing=existing, default_none=True)
        if (kind is None and options.proxy_http is True
                and (options.webserver or "auto") == "auto"):
            kind = pick_web_kind(existing, prompter)
    if kind and (options.webserver or "auto") == "auto":
        options.webserver = kind
    proxy = options.proxy_http
    if proxy is None:
        if (options.webserver or "auto") in ("nginx", "apache"):
            proxy = True
        elif source_web:
            proxy = bool(source_web.get("proxied"))
        else:
            proxy = bool(kind)
    web = ensure_webserver(config, kind, options, reporter, prompter) if kind else None
    if kind and web is None:
        reporter.warn(
            "web server setup was skipped; restored Prosody HTTP endpoints may only "
            "listen on localhost"
        )
    result["web"] = web
    result["web_dict"] = web.as_dict() if web is not None else None
    result["proxy"] = bool(proxy)
    if result["proxy"] and web is None:
        reporter.warn(
            "the backup used a web proxy but no web server is being configured; "
            "restored HTTP endpoints may only listen on localhost"
        )

    wanted = _wanted_certbot(source_le, source_web, options, prompter, setup=False)
    result["certbot"] = bool(wanted)
    le_restored = bool(
        (manifest.get("targets") or {}).get("letsencrypt")
        or (manifest.get("components") or {}).get("letsencrypt")
    )
    result["issue_certbot"] = bool(
        wanted and (not source_le.get("present") or not le_restored)
    )
    result["certbot_installed"] = False
    if wanted:
        result["certbot_installed"] = ensure_certbot(config, options, reporter, prompter)
    return result


def _restore_plan(config, options, source):
    source_web = source.get("webserver") or {}
    source_prosody = source.get("prosody") or {}
    kind = options.webserver
    if kind == "auto":
        source_kind = source_web.get("kind") or "none"
        kind = source_kind if source_web.get("proxied") else "none"
    return {
        "install_prosody": not prosody_installed(config.host),
        "webserver": kind,
        "certbot": options.certbot,
        "domains": options.domains or source_prosody.get("virtualhosts") or [],
    }


def _backend_host(source_prosody):
    interfaces = source_prosody.get("http_interfaces") or []
    if interfaces:
        value = interfaces[0]
        if value not in ("*", "0.0.0.0", "::", "[::]"):
            return value
    return "127.0.0.1"


def _backend_port(source_prosody):
    ports = source_prosody.get("http_ports") or []
    return int(ports[0]) if ports else 5280


def _choose_cert_name(source_le, domains, source_prosody=None):
    # Prefer the certificate name actually referenced by the source Prosody
    # config, then the certbot renewal name, then the first domain.
    for path in (source_prosody or {}).get("certificate_files") or []:
        match = re.search(r"/etc/letsencrypt/live/([^/]+)/", str(path))
        if match:
            return _safe_cert_name(match.group(1))
    names = (source_le or {}).get("cert_names") or []
    if names:
        return _safe_cert_name(names[0])
    if domains:
        return _safe_cert_name(domains[0])
    return None


# ---------------------------------------------------------------------------
# Web server and certificate configuration

def certificate_root(cert_name):
    return os.path.join(CERTBOT_LIVE, cert_name)


def has_certificate(cert_name):
    if not cert_name:
        return False
    root = certificate_root(cert_name)
    return (os.path.isfile(os.path.join(root, "fullchain.pem"))
            and os.path.isfile(os.path.join(root, "privkey.pem")))


def _safe_cert_name(value):
    value = str(value).strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value or "proctor-xmpp"


def normalize_domains(values):
    result = []
    for value in values or []:
        for item in re.split(r"[\s,]+", str(value)):
            item = item.strip().lower()
            if not item:
                continue
            if "://" in item:
                item = item.split("://", 1)[1]
            item = item.split("/", 1)[0].rstrip(".")
            if item.startswith("*."):
                raise ConfigError(
                    "wildcard domains need DNS-01 validation and are not supported "
                    "by the HTTP-01 certbot flow: %s" % item
                )
            if not re.match(
                    r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                    r"[A-Za-z]{2,63}$", item):
                raise ConfigError("invalid XMPP/HTTP domain: %s" % item)
            if item not in result:
                result.append(item)
    return result


def _lua_string(value):
    import json
    return json.dumps(str(value), ensure_ascii=True)


def prosody_config_text(layout, domains, admin, tls, cert_name, storage="internal_hashed",
                        proxy=True, http_port=5280, http_interface="*"):
    if not domains:
        raise ConfigError("at least one XMPP domain is required")
    admin = admin or ("admin@" + domains[0])
    modules = [
        "roster", "sasl", "tls", "dialback", "disco", "carbons", "ping",
        "version", "time", "http", "bosh", "websocket", "http_upload", "muc",
        "vcard", "private", "blocklist", "smacks",
    ]
    lines = [
        "-- Generated by proctor for %s" % ", ".join(domains),
        "-- Review this file before exposing the service to the internet.",
        "",
        "admins = { %s }" % _lua_string(admin),
        "data_path = %s" % _lua_string(layout.data_dir),
        "authentication = \"internal_hashed\"",
    ]
    if storage in ("sql", "sqlite", "sqlite3"):
        database = os.path.join(layout.data_dir, "prosody.sqlite")
        lines.extend([
            "storage = \"sql\"",
            "sql = { driver = \"SQLite3\"; database = %s }" % _lua_string(database),
        ])
        layout.storage = "sql"
        layout.sql = {"engine": "sqlite", "path": database, "database": database,
                      "driver": "SQLite3"}
    else:
        lines.append("storage = %s" % _lua_string(storage or "internal_hashed"))
        layout.storage = storage or "internal_hashed"
    cert_root = certificate_root(cert_name) if cert_name else None
    lines.extend([
        "",
        "modules_enabled = {",
        "    " + "; ".join(_lua_string(item) for item in modules) + ";",
        "}",
        "",
        "allow_registration = false",
        "c2s_require_encryption = %s" % ("true" if tls else "false"),
        "s2s_require_encryption = %s" % ("true" if tls else "false"),
        "consider_bosh_secure = true",
        "consider_websocket_secure = true",
        "cross_domain_bosh = false",
        "cross_domain_websocket = false",
        "http_upload_file_size_limit = 10485760",
        "http_ports = { %d }" % int(http_port),
        "http_interfaces = { %s }" % _lua_string(
            "127.0.0.1" if proxy else (http_interface or "*")),
        "https_ports = { 5281 }" if (tls and not proxy) else "https_ports = { }",
        "c2s_ports = { 5222 }",
        "s2s_ports = { 5269 }",
        "legacy_ssl_ports = { }",
        "",
        "log = {",
        "    { levels = { min = \"info\" }; to = \"file\"; filename = %s };"
        % _lua_string(os.path.join(layout.log_dir, "prosody.log")),
        "}",
    ])
    if tls and not proxy and cert_root:
        # Direct HTTPS/BOSH (no reverse proxy) needs its own certificate. The
        # nginx/Apache path terminates TLS, so Prosody should not also bind 5281.
        lines.extend([
            "",
            "https_ssl = {",
            "    key = %s;" % _lua_string(os.path.join(cert_root, "privkey.pem")),
            "    certificate = %s;" % _lua_string(
                os.path.join(cert_root, "fullchain.pem")),
            "}",
        ])
    lines.append("")
    for domain in domains:
        lines.append("VirtualHost %s" % _lua_string(domain))
        if tls and cert_root:
            per_domain = certificate_root(domain)
            root = per_domain if has_certificate(domain) else cert_root
            lines.extend([
                "    ssl = {",
                "        key = %s;" % _lua_string(os.path.join(root, "privkey.pem")),
                "        certificate = %s;" % _lua_string(
                    os.path.join(root, "fullchain.pem")),
                "    }",
            ])
        lines.append("")
    if domains:
        lines.append("Component %s \"muc\"" % _lua_string("conference." + domains[0]))
    lines.append("")
    return "\n".join(lines)


def _proxy_nginx(backend_host, backend_port, proxy_paths):
    headers = [
        "proxy_set_header Host $host;",
        "proxy_set_header X-Real-IP $remote_addr;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $connection_upgrade;",
        "proxy_http_version 1.1;",
        "proxy_read_timeout 3600s;",
    ]
    blocks = []
    for path in proxy_paths:
        if path == "/upload":
            blocks.append(
                "    location ^~ /upload {\n"
                "        proxy_pass http://%s:%d;\n"
                "        client_max_body_size 100m;\n"
                "        %s\n"
                "    }" % (backend_host, backend_port, "\n        ".join(headers))
            )
        elif path == "/xmpp-websocket":
            blocks.append(
                "    location ^~ /xmpp-websocket {\n"
                "        proxy_pass http://%s:%d;\n"
                "        %s\n"
                "    }" % (backend_host, backend_port, "\n        ".join(headers))
            )
        else:
            blocks.append(
                "    location ^~ %s {\n"
                "        proxy_pass http://%s:%d;\n"
                "        %s\n"
                "    }" % (path, backend_host, backend_port,
                            "\n        ".join(headers))
            )
    return "\n".join(blocks)


def nginx_config_text(domains, tls, cert_name, proxy, proxy_paths,
                      backend_host="127.0.0.1", backend_port=5280):
    names = " ".join(domains)
    root = certificate_root(cert_name) if cert_name else ""
    acme = (
        "    location ^~ /.well-known/acme-challenge/ {\n"
        "        root /var/www/html;\n"
        "        default_type text/plain;\n"
        "    }"
    )
    proxy_block = _proxy_nginx(backend_host, backend_port, proxy_paths) if proxy else ""
    fallback = "    location / {\n        return 404;\n    }"
    head = (
        "# Managed by proctor. Manual changes may be overwritten.\n"
        "map $http_upgrade $connection_upgrade {\n"
        "    default upgrade;\n"
        "    '' close;\n"
        "}\n\n"
    )
    if not tls:
        return head + (
            "server {\n"
            "    listen 80;\n"
            "    listen [::]:80;\n"
            "    server_name %s;\n"
            "%s\n"
            "%s\n"
            "%s\n"
            "}\n" % (names, acme, proxy_block, fallback)
        ).replace("\n\n\n", "\n\n")
    http = (
        "server {\n"
        "    listen 80;\n"
        "    listen [::]:80;\n"
        "    server_name %s;\n"
        "%s\n"
        "    location / {\n"
        "        return 301 https://$host$request_uri;\n"
        "    }\n"
        "}\n" % (names, acme)
    )
    https = (
        "server {\n"
        "    listen 443 ssl;\n"
        "    listen [::]:443 ssl;\n"
        "    server_name %s;\n"
        "    ssl_certificate %s/fullchain.pem;\n"
        "    ssl_certificate_key %s/privkey.pem;\n"
        "    ssl_protocols TLSv1.2 TLSv1.3;\n"
        "    ssl_session_cache shared:SSL:10m;\n"
        "    ssl_session_timeout 1d;\n"
        "%s\n"
        "%s\n"
        "%s\n"
        "}\n" % (names, root, root, acme, proxy_block, fallback)
    )
    return head + http + "\n" + https


def _proxy_apache(backend_host, backend_port, proxy_paths):
    lines = ["    ProxyPreserveHost On", "    ProxyRequests Off"]
    for path in proxy_paths:
        if path == "/xmpp-websocket":
            target = "ws://%s:%d/xmpp-websocket" % (backend_host, backend_port)
        else:
            target = "http://%s:%d%s" % (backend_host, backend_port, path)
        lines.append("    ProxyPass %s %s" % (path, target))
        lines.append("    ProxyPassReverse %s %s" % (path, target))
    lines.append("    RequestHeader set X-Forwarded-Proto \"https\"")
    lines.append("    AllowEncodedSlashes NoDecode")
    return "\n".join(lines)


def apache_config_text(domains, tls, cert_name, proxy, proxy_paths,
                       backend_host="127.0.0.1", backend_port=5280):
    names = domains[0]
    aliases = " ".join(domains[1:])
    root = certificate_root(cert_name) if cert_name else ""
    proxy_block = _proxy_apache(backend_host, backend_port, proxy_paths) if proxy else ""
    http = (
        "# Managed by proctor. Manual changes may be overwritten.\n"
        "<VirtualHost *:80>\n"
        "    ServerName %s\n"
        "%s"
        "    DocumentRoot /var/www/html\n"
        "    <Directory /var/www/html>\n"
        "        Require all granted\n"
        "    </Directory>\n"
        "    Alias /.well-known/acme-challenge/ /var/www/html/.well-known/acme-challenge/\n"
        "%s\n"
        "%s\n"
        "</VirtualHost>\n" % (
            names,
            ("    ServerAlias %s\n" % aliases) if aliases else "",
            proxy_block,
            ("    RewriteEngine On\n"
             "    RewriteCond %%{REQUEST_URI} !^/\\.well-known/acme-challenge/\n"
             "    RewriteRule ^ https://%%{HTTP_HOST}%%{REQUEST_URI} [R=301,L]\n")
            if tls else "",
        )
    )
    if not tls:
        return http
    https = (
        "<IfModule mod_ssl.c>\n"
        "<VirtualHost *:443>\n"
        "    ServerName %s\n"
        "%s"
        "    SSLEngine on\n"
        "    SSLCertificateFile %s/fullchain.pem\n"
        "    SSLCertificateKeyFile %s/privkey.pem\n"
        "    SSLProtocol all -SSLv3 -TLSv1 -TLSv1.1\n"
        "    DocumentRoot /var/www/html\n"
        "    <Directory /var/www/html>\n"
        "        Require all granted\n"
        "    </Directory>\n"
        "    Alias /.well-known/acme-challenge/ /var/www/html/.well-known/acme-challenge/\n"
        "%s\n"
        "</VirtualHost>\n"
        "</IfModule>\n" % (
            names,
            ("    ServerAlias %s\n" % aliases) if aliases else "",
            root, root, proxy_block,
        )
    )
    return http + "\n" + https


def web_config_text(info, domains, tls, cert_name, proxy, proxy_paths,
                    backend_host="127.0.0.1", backend_port=5280):
    if info is None:
        return None
    if info.kind == "nginx":
        return nginx_config_text(domains, tls, cert_name, proxy, proxy_paths,
                                 backend_host, backend_port)
    if info.kind == "apache":
        return apache_config_text(domains, tls, cert_name, proxy, proxy_paths,
                                  backend_host, backend_port)
    raise ConfigError("unsupported web server: %s" % info.kind)


def web_config_path(config, info):
    if info.kind == "nginx":
        if info.conf_dir:
            return os.path.join(info.conf_dir, "proctor-xmpp.conf"), None
        if info.sites_available:
            return (os.path.join(info.sites_available, "proctor-xmpp.conf"),
                    os.path.join(info.sites_enabled, "proctor-xmpp.conf")
                    if info.sites_enabled else None)
        return os.path.join(info.config_root, "proctor-xmpp.conf"), None
    if info.kind == "apache":
        if config.host.family() == "debian" and info.sites_available:
            return (os.path.join(info.sites_available, "proctor-xmpp.conf"),
                    os.path.join(info.sites_enabled, "proctor-xmpp.conf")
                    if info.sites_enabled else None)
        if info.conf_dir:
            return os.path.join(info.conf_dir, "proctor-xmpp.conf"), None
        return os.path.join(info.config_root, "proctor-xmpp.conf"), None
    raise ConfigError("unsupported web server: %s" % info.kind)


def apply_web_config(config, info, text, reporter, dry_run=False):
    path, link = web_config_path(config, info)
    if dry_run:
        reporter.info("Would write %s" % path)
        return {"path": path, "link": link, "dry_run": True}
    ensure_dir(os.path.dirname(path), mode=0o755)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                managed = handle.readline().startswith("# Managed by proctor")
            if not managed:
                stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                backup = path + ".pre-proctor-" + stamp
                import shutil
                shutil.copy2(path, backup)
                reporter.info("Saved existing web config to %s" % backup)
        except OSError:
            pass
    atomic_write(path, text.encode("utf-8"), mode=0o644)
    _relabel([os.path.dirname(path)])
    _warn_if_not_included(info, path, reporter)
    if link:
        ensure_dir(os.path.dirname(link), mode=0o755)
        try:
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(path, link)
        except OSError as exc:
            raise ConfigError("could not enable web server config %s: %s" % (link, exc))
    if info.kind == "apache" and config.host.family() == "debian":
        if which("a2enmod"):
            run(["a2enmod", "proxy", "proxy_http", "proxy_wstunnel", "ssl",
                 "headers", "rewrite"], check=False)
        if which("a2ensite"):
            run(["a2ensite", "proctor-xmpp"], check=False)
    _test_web_config(config, info, reporter)
    controller = _web_controller(config, info.service)
    if not controller.available:
        reporter.warn(
            "no service manager detected; start/reload %s manually" % info.service
        )
        return {"path": path, "link": link, "dry_run": False}
    controller.enable()
    if controller.is_active() is False:
        if not controller.start():
            raise ProctorError("could not start %s" % info.service)
    else:
        controller.reload()
    return {"path": path, "link": link, "dry_run": False}


def _warn_if_not_included(info, path, reporter):
    if not info.main_config or not os.path.isfile(info.main_config):
        return
    try:
        with open(info.main_config, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return
    if info.kind == "nginx":
        if "conf.d" in path and "conf.d" not in text:
            reporter.warn("nginx.conf does not appear to include conf.d/*.conf")
        elif "sites-enabled" in path and "sites-enabled" not in text:
            reporter.warn("nginx.conf does not appear to include sites-enabled/*")
    elif info.kind == "apache":
        if "conf.d" in path and "conf.d" not in text and "IncludeOptional" not in text:
            reporter.warn("httpd.conf does not appear to include conf.d/*.conf")
        if "sites-enabled" in path and "sites-enabled" not in text:
            reporter.warn("apache2.conf does not appear to include sites-enabled/*")


def _test_web_config(config, info, reporter):
    if not info.test_command:
        return
    proc = run(info.test_command, check=False)
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise ConfigError(
            "%s configuration test failed:\n%s" % (info.kind, output),
            hint="fix the generated config, or restore an older safety backup",
        )


def obtain_certificate(web, domains, email, cert_name, reporter, dry_run=False,
                       backend="webroot"):
    if not domains:
        return False
    cert_name = _safe_cert_name(cert_name or domains[0])
    if has_certificate(cert_name):
        return True
    if dry_run:
        reporter.info("Would request a certificate for %s with certbot" % ", ".join(domains))
        return False
    if not email:
        raise ConfigError("certbot requires an email address",
                          hint="pass --email or configure provision.email")
    command = ["certbot", "certonly", "--cert-name", cert_name,
               "--email", email, "--agree-tos", "--non-interactive",
               "--keep-until-expiring", "--preferred-challenges", "http"]
    if backend == "webroot":
        ensure_dir(os.path.join(WEBROOT, ".well-known", "acme-challenge"), mode=0o755)
        command.extend(["--webroot", "-w", WEBROOT])
    else:
        command.append("--standalone")
    for domain in domains:
        command.extend(["-d", domain])
    reporter.step("Requesting a Let's Encrypt certificate for %s" % ", ".join(domains))
    try:
        run(command, timeout=None)
    except ExternalCommandError as exc:
        reporter.warn("certificate request failed: %s" % exc)
        return False
    if not has_certificate(cert_name):
        reporter.warn("certbot finished but %s was not created" % certificate_root(cert_name))
        return False
    return True


# ---------------------------------------------------------------------------
# Restore finalisation and full setup

def finalize_restore(config, manifest, target, prepared, options, reporter, prompter):
    if options.dry_run:
        return {"plan": prepared.get("plan")}
    domains = list(prepared.get("domains") or [])
    source = manifest.get("source") or {}
    source_web = source.get("webserver") or {}
    cert_name = prepared.get("cert_name") or _choose_cert_name(
        source.get("letsencrypt") or {}, domains,
        source.get("prosody") or {}) or _safe_cert_name(domains[0] if domains else "prosody")
    web = prepared.get("web")
    email = options.email
    tls = has_certificate(cert_name)
    if web is not None and not domains:
        reporter.warn("no XMPP domains were found in the backup; web proxy setup skipped")
        return {
            "kind": web.kind, "domains": [], "cert_name": cert_name,
            "tls": tls, "proxy": bool(prepared.get("proxy")), "config": None,
            "certificate_requested": False, "skipped": True,
        }
    proxy = bool(prepared.get("proxy"))
    proxy_paths = prepared.get("proxy_paths") or list(PROXY_PATHS)
    backend_host = prepared.get("backend_host") or "127.0.0.1"
    backend_port = prepared.get("backend_port") or 5280
    result = {
        "kind": web.kind if web is not None else None,
        "domains": domains,
        "cert_name": cert_name,
        "tls": tls,
        "proxy": proxy,
        "config": None,
        "certificate_requested": False,
    }
    if prepared.get("certbot") and not tls:
        # The manifest said certbot was in use but the expected certificate is
        # absent (for example the archive excluded /etc/letsencrypt).
        prepared["issue_certbot"] = True
    if prepared.get("issue_certbot") and not tls:
        if not email and prompter.interactive:
            email = prompter.text("Email address for Let's Encrypt", required=True)
        if email:
            result["certificate_requested"] = True
            if web is not None:
                http_text = web_config_text(web, domains, False, cert_name, proxy,
                                            proxy_paths, backend_host, backend_port)
                apply_web_config(config, web, http_text, reporter)
            if obtain_certificate(web, domains, email, cert_name, reporter,
                                  backend="webroot" if web is not None else "standalone"):
                tls = True
                result["tls"] = True
        else:
            result["certificate_error"] = "no email supplied; skipped certbot issuance"
            reporter.warn(result["certificate_error"])
    if web is not None:
        text = web_config_text(web, domains, tls, cert_name, proxy, proxy_paths,
                               backend_host, backend_port)
        applied = apply_web_config(config, web, text, reporter)
        result["config"] = applied.get("path")
    grant_certificate_access(target, reporter)
    if prepared.get("certbot") and not prepared.get("certbot_installed"):
        reporter.warn("certbot is not installed; automatic certificate renewal is unavailable")
    if prepared.get("certbot"):
        install_renewal_hook(target, web)
        if web is not None:
            configure_renewal_webroot()
        elif source_web.get("kind") in ("nginx", "apache"):
            reporter.warn(
                "certbot was used on the source host but no web server is configured "
                "here; automatic renewal may need manual attention"
            )
    result["firewall"] = ensure_firewall(
        config.host, firewall_ports(bool(proxy and web is not None), tls, backend_port),
        options, reporter, prompter,
    )
    return result


def setup_server(config, options, reporter):
    host = config.host
    prompter = Prompter(assume_yes=options.assume_yes)
    if not options.dry_run and not is_root():
        raise PermissionDenied("setting up Prosody and a web server requires root")
    if not options.dry_run and not prompter.interactive and not options.assume_yes:
        raise ConfigError(
            "setup is interactive; run it in a terminal or pass --yes with the "
            "required --domain/--webserver/--certbot options"
        )
    if options.assume_yes and options.install_missing is None:
        options.install_missing = True
    if options.assume_yes and not options.domains:
        raise ConfigError("--domain is required with --yes",
                          hint="pass --domain example.com (repeatable)")
    if not options.dry_run:
        prosody_action = ensure_prosody(config, options, reporter, prompter)
        if not prosody_action.get("installed"):
            raise ProctorError(
                "cannot continue without Prosody",
                hint="run setup interactively, or pass --yes so package installation is allowed",
            )
    domains = normalize_domains(options.domains)
    if not domains:
        domains = normalize_domains([prompter.text(
            "XMPP domain(s), comma separated", required=True
        )])
    if not domains:
        raise ConfigError("at least one domain is required",
                          hint="pass --domain example.com (repeatable)")
    admin = options.admin
    if admin is None:
        default_admin = "admin@" + domains[0]
        admin = (prompter.text("Admin JID", default=default_admin)
                 if prompter.interactive else default_admin)
    if "@" in admin:
        local_admin, admin_host = admin.split("@", 1)
        admin = local_admin + "@" + admin_host.lower()
    if "@" not in admin:
        raise ConfigError("admin must be a full JID, for example admin@example.com")
    admin_domain = admin.split("@", 1)[1]
    if admin_domain not in domains:
        raise ConfigError(
            "admin domain %s is not one of the configured domains: %s"
            % (admin_domain, ", ".join(domains))
        )
    storage = options.storage
    if storage is None:
        if prompter.interactive:
            storage = prompter.choice(
                "Prosody storage backend?",
                [("internal_hashed", "internal hashed files (recommended)"),
                 ("sqlite", "SQLite database")],
                default="internal_hashed",
            )
        else:
            storage = "internal_hashed"
    existing_web = detect_webserver(host)
    kind = choose_webserver(None, options, prompter, existing=existing_web,
                            default_none=False)
    certbot_wanted = _wanted_certbot({}, {}, options, prompter, setup=True)
    email = options.email
    if certbot_wanted and not email and not options.dry_run:
        email = prompter.text("Email address for Let's Encrypt", required=True)
    proxy = options.proxy_http
    if proxy is None:
        if kind and prompter.interactive:
            proxy = prompter.yes_no(
                "Proxy HTTP/BOSH/WebSocket/upload endpoints through %s?" % kind,
                default=True,
            )
        else:
            proxy = bool(kind)
    if kind is None:
        proxy = False
    if kind and (options.webserver or "auto") == "auto":
        options.webserver = kind
    if options.dry_run:
        layout = layout_for_host(config)
        tls = has_certificate(_safe_cert_name(domains[0]))
        config_text = prosody_config_text(
            layout, domains, admin, tls, domains[0], storage=storage,
            proxy=proxy, http_port=options.http_port,
            http_interface=options.http_interface,
        )
        web_preview = None
        if kind:
            preview_info = WebServerInfo()
            preview_info.kind = kind
            web_preview = web_config_text(
                preview_info, domains, False, _safe_cert_name(domains[0]),
                bool(proxy), list(PROXY_PATHS), "127.0.0.1", options.http_port,
            )
        return {
            "dry_run": True,
            "domains": domains,
            "admin": admin,
            "webserver": kind,
            "storage": storage,
            "certbot": bool(certbot_wanted),
            "proxy": bool(proxy),
            "install_prosody": not prosody_installed(host),
            "packages": {
                "prosody": prosody_packages(host),
                "webserver": web_packages(host, kind) if kind else [],
                "certbot": certbot_packages(host) if certbot_wanted else [],
            },
            "prosody_config_preview": config_text,
            "web_config_preview": web_preview,
        }
    web = ensure_webserver(config, kind, options, reporter, prompter) if kind else None
    if certbot_wanted:
        ensure_certbot(config, options, reporter, prompter)
    layout = layout_for_host(config)
    if os.path.exists(layout.config_file):
        if not options.force and not prompter.yes_no(
                "%s already exists. Overwrite it with a generated configuration?"
                % layout.config_file, default=True):
            raise Aborted("setup cancelled; existing Prosody config was kept")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = layout.config_file + ".pre-proctor-" + stamp
        try:
            import shutil
            shutil.copy2(layout.config_file, backup)
            reporter.info("Saved existing config to %s" % backup)
        except OSError as exc:
            reporter.warn("could not back up existing config: %s" % exc)
    _prepare_prosody_dirs(layout, reporter)
    controller = _controller_for(config, layout.service_name)
    if controller.available and controller.is_active():
        reporter.step("Stopping %s before writing configuration" % layout.service_name)
        if not controller.stop():
            raise ProctorError("%s did not stop; refusing to overwrite its config"
                               % layout.service_name)
    cert_name = _safe_cert_name(domains[0])
    tls = has_certificate(cert_name)
    if web is not None:
        http_text = web_config_text(web, domains, False, cert_name, proxy,
                                    list(PROXY_PATHS), "127.0.0.1", options.http_port)
        apply_web_config(config, web, http_text, reporter)
    if certbot_wanted:
        tls = obtain_certificate(web, domains, email, cert_name, reporter,
                                 backend="webroot" if web is not None else "standalone")
    if tls:
        grant_certificate_access(layout, reporter)
    if certbot_wanted:
        install_renewal_hook(layout, web)
        if web is not None:
            configure_renewal_webroot()
    if not tls:
        reporter.warn(
            "no TLS certificate is configured; XMPP clients cannot use STARTTLS. "
            "Run certbot or provide certificates, then update the Prosody config."
        )
    config_text = prosody_config_text(
        layout, domains, admin, tls, cert_name, storage=storage,
        proxy=bool(proxy), http_port=options.http_port,
        http_interface=options.http_interface,
    )
    atomic_write(layout.config_file, config_text.encode("utf-8"), mode=0o644)
    _chown_path(layout.config_file, 0, 0)
    _relabel([layout.config_dir, layout.data_dir, layout.log_dir])
    reporter.step("Checking generated Prosody configuration")
    from .service import validate_prosody_config
    status, output = validate_prosody_config(layout.config_file)
    if status is False:
        raise ConfigError("generated Prosody config failed its check:\n%s" % output)
    controller.enable()
    if options.start:
        if not controller.start():
            raise ProctorError("Prosody did not start")
    if web is not None:
        text = web_config_text(web, domains, tls, cert_name, proxy,
                               list(PROXY_PATHS), "127.0.0.1", options.http_port)
        applied = apply_web_config(config, web, text, reporter)
    else:
        applied = None
    if tls:
        _chown_prosody_paths(layout)
        grant_certificate_access(layout, reporter)
    admin_result = _register_admin(config, layout, admin, options, prompter, reporter)
    firewall = ensure_firewall(
        host, firewall_ports(bool(proxy), tls, options.http_port), options,
        reporter, prompter,
    )
    return {
        "domains": domains,
        "admin": admin,
        "prosody": layout.as_dict(),
        "webserver": web.as_dict() if web is not None else None,
        "storage": storage,
        "proxy": bool(proxy),
        "certbot": bool(certbot_wanted),
        "certbot_failed": bool(certbot_wanted and not tls),
        "tls": tls,
        "cert_name": cert_name,
        "web_config": applied.get("path") if applied else None,
        "admin_created": admin_result,
        "firewall": firewall,
    }


def _prepare_prosody_dirs(layout, reporter):
    ensure_dir(layout.config_dir, mode=0o755)
    ensure_dir(layout.data_dir, mode=0o750)
    ensure_dir(layout.log_dir, mode=0o750)
    for directory in layout.cert_dirs:
        ensure_dir(directory, mode=0o755)
    uid = uid_for(layout.user)
    gid = gid_for(layout.group)
    if uid is not None and gid is not None:
        for path in [layout.data_dir, layout.log_dir]:
            try:
                os.chown(path, uid, gid)
            except OSError as exc:
                reporter.warn("could not chown %s: %s" % (path, exc))


def _chown_prosody_paths(layout):
    uid = uid_for(layout.user)
    gid = gid_for(layout.group)
    if uid is None or gid is None:
        return
    for path in [layout.data_dir, layout.log_dir] + list(layout.cert_dirs):
        try:
            os.chown(path, uid, gid)
        except OSError:
            pass


def _chown_path(path, uid, gid):
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def _register_admin(config, layout, admin, options, prompter, reporter):
    if not admin or "@" not in admin:
        return False
    if not which("prosodyctl"):
        return False
    password = options.admin_password
    if password is None:
        password = prompter.password("Password for %s (leave empty to create later):" % admin)
    if not password:
        reporter.warn("admin account was not created; use: prosodyctl register USER DOMAIN PASSWORD")
        return False
    local, domain = admin.split("@", 1)
    proc = run(["prosodyctl", "register", local, domain, password], check=False)
    if proc.returncode != 0:
        output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        reporter.warn("could not create admin %s: %s" % (admin, output.strip()))
        return False
    reporter.success("Created admin account %s" % admin)
    return True


def _relabel(paths):
    if not is_root() or not os.path.isdir("/sys/fs/selinux"):
        return
    if not which("restorecon"):
        return
    existing = [path for path in paths if os.path.exists(path)]
    if not existing:
        return
    run(["restorecon", "-R"] + existing, check=False)


def grant_certificate_access(layout, reporter=None):
    """Let the Prosody user read certbot's private keys and traverse its tree.

    Certbot creates /etc/letsencrypt with mode 0700. Making live/ and archive/
    readable is not enough: without execute permission on the parent directory
    Prosody cannot resolve the live symlinks. Prefer POSIX ACLs when setfacl is
    available, and fall back to a group-readable private key plus traversal
    permissions.
    """
    if layout is None or not os.path.isdir(CERTBOT_ROOT):
        return False
    user = getattr(layout, "user", None) or "prosody"
    group = getattr(layout, "group", None) or user
    if which("setfacl") and uid_for(user) is not None:
        acl = ["u:%s" % user]
        commands = [
            ["setfacl", "-m", acl[0] + ":x", CERTBOT_ROOT],
            ["setfacl", "-R", "-m", acl[0] + ":rX", CERTBOT_LIVE],
            ["setfacl", "-R", "-m", acl[0] + ":rX", CERTBOT_ARCHIVE],
        ]
        results = []
        for command in commands:
            if not os.path.isdir(command[-1]):
                continue
            proc = run(command, check=False)
            results.append(proc.returncode == 0)
        if results and all(results):
            return True

    gid = gid_for(group)
    # Traversal-only on the parent; the live and archive directories need to be
    # listable so Prosody can resolve Certbot's timestamped symlink targets.
    for path in (CERTBOT_ROOT, CERTBOT_LIVE, CERTBOT_ARCHIVE):
        if not os.path.isdir(path):
            continue
        try:
            os.chmod(path, 0o711 if path == CERTBOT_ROOT else 0o755)
        except OSError:
            pass
    if gid is None:
        if reporter is not None:
            reporter.warn(
                "cannot grant %s access to Certbot private keys: group %r does not exist"
                % (user, group)
            )
        return False
    bases = [CERTBOT_LIVE, CERTBOT_ARCHIVE]
    for base in bases:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            try:
                os.chmod(dirpath, 0o755)
            except OSError:
                pass
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    if name.startswith("privkey"):
                        os.chown(path, -1, gid)
                        os.chmod(path, 0o640)
                    elif name.endswith((".pem", ".crt")):
                        os.chmod(path, 0o644)
                except OSError:
                    continue
    return True


def install_renewal_hook(layout, web=None, dry_run=False):
    """Keep certbot renewals readable by Prosody and reload the services."""
    path = "/etc/letsencrypt/renewal-hooks/deploy/proctor-permissions"
    if dry_run:
        return path
    ensure_dir(os.path.dirname(path), mode=0o755)
    user = layout.user or "prosody"
    group = layout.group or "prosody"
    services = [layout.service_name or "prosody"]
    if web is not None and web.service:
        services.append(web.service)
    reload_lines = []
    for service in dict.fromkeys(services):
        reload_lines.append(
            'if command -v systemctl >/dev/null 2>&1; then '
            'systemctl try-reload-or-restart %s >/dev/null 2>&1 || true; fi' % shlex.quote(service)
        )
        reload_lines.append(
            'if command -v rc-service >/dev/null 2>&1; then '
            'rc-service %s reload >/dev/null 2>&1 || true; fi' % shlex.quote(service)
        )
        reload_lines.append(
            'if command -v sv >/dev/null 2>&1; then '
            'sv reload %s >/dev/null 2>&1 || true; fi' % shlex.quote(service)
        )
        reload_lines.append(
            'if command -v service >/dev/null 2>&1; then '
            'service %s reload >/dev/null 2>&1 || true; fi' % shlex.quote(service)
        )
    script = "\n".join([
        "#!/bin/sh",
        "# Managed by proctor: certbot deploy hook after renewal.",
        "chmod 711 %s 2>/dev/null || true" % shlex.quote(CERTBOT_ROOT),
        "if command -v setfacl >/dev/null 2>&1; then",
        "    setfacl -m u:%s:x %s 2>/dev/null || true"
        % (shlex.quote(user), shlex.quote(CERTBOT_ROOT)),
        "    setfacl -R -m u:%s:rX %s %s 2>/dev/null || true"
        % (shlex.quote(user), shlex.quote(CERTBOT_LIVE),
           shlex.quote(CERTBOT_ARCHIVE)),
        "else",
        "    chmod 711 %s 2>/dev/null || true" % shlex.quote(CERTBOT_ROOT),
        "    for d in %s %s; do"
        % (shlex.quote(CERTBOT_LIVE), shlex.quote(CERTBOT_ARCHIVE)),
        "        [ -d \"$d\" ] || continue",
        "        chmod 755 \"$d\" 2>/dev/null || true",
        "        find \"$d\" -type d -exec chmod 755 {} + 2>/dev/null || true",
        "    done",
        "    for f in /etc/letsencrypt/archive/*/privkey*.pem; do",
        "        [ -e \"$f\" ] || continue",
        "        chgrp %s \"$f\" 2>/dev/null || true" % shlex.quote(group),
        "        chmod 640 \"$f\" 2>/dev/null || true",
        "    done",
        "    for f in /etc/letsencrypt/archive/*/*.pem; do",
        "        case \"$f\" in *privkey*) continue ;; esac",
        "        [ -e \"$f\" ] && chmod 644 \"$f\" 2>/dev/null || true",
        "    done",
        "fi",
    ] + reload_lines + [""])
    atomic_write(path, script.encode("utf-8"), mode=0o755)
    return path


def configure_renewal_webroot(dry_run=False):
    """Rewrite certbot renewal configs to use the webroot flow.

    Restored certificates may have been issued with the nginx or Apache plugin.
    A new host may not have those certbot plugins, but it does have the
    generated vhost and /var/www/html, so webroot renewal is the portable
    choice.
    """
    if dry_run:
        return 0
    changed = 0
    for path in sorted(glob.glob(os.path.join(CERTBOT_RENEWAL, "*.conf"))):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        auth_value = None
        for line in lines:
            match = re.match(r"\s*authenticator\s*=\s*(\S+)", line)
            if match:
                auth_value = match.group(1).lower()
                break
        if auth_value and auth_value not in ("nginx", "apache", "standalone",
                                              "webroot", "webroot-path"):
            # Preserve DNS-01/manual renewal; those need their plugins and
            # credentials, which Proctor does not manage.
            continue
        output = []
        in_params = False
        found_params = False
        saw_auth = False
        saw_webroot = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_params = stripped.lower() == "[renewalparams]"
                found_params = found_params or in_params
            if in_params and stripped.startswith("authenticator"):
                output.append("authenticator = webroot\n")
                saw_auth = True
                continue
            if in_params and stripped.startswith("webroot_path"):
                output.append("webroot_path = %s\n" % WEBROOT)
                saw_webroot = True
                continue
            output.append(line)
        if not found_params:
            continue
        if not saw_auth:
            output.append("authenticator = webroot\n")
        if not saw_webroot:
            output.append("webroot_path = %s\n" % WEBROOT)
        try:
            atomic_write(path, "".join(output).encode("utf-8"), mode=0o600)
            changed += 1
        except OSError:
            continue
    return changed
