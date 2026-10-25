"""coturn detection, configuration and health checks for Prosody TURN.

Prosody's ``mod_turn_external`` only hands clients a hostname, port and shared
secret; the TURN server itself lives in coturn. This module keeps the coturn
side (config, web server ACME vhost, firewall, SELinux and certificates) in one
place so ``setup`` and ``doctor`` agree on what "working" means.
"""

from __future__ import print_function

import glob
import os
import re
import socket
import struct

from .errors import ConfigError, ProctorError
from .util import atomic_write, ensure_dir, is_root, is_within, run, uid_for, gid_for, which

COTURN_CONF = "/etc/coturn/turnserver.conf"
COTURN_BINARY = "turnserver"
COTURN_PIDFILE = "/run/coturn/turnserver.pid"
COTURN_SERVICE = "coturn"
COTURN_ROOT = "/etc/coturn"
COTURN_DEFAULT_PORTS = (3478, 5349)
DEFAULT_RELAY_MIN = 49152
DEFAULT_RELAY_MAX = 65535
DEFAULT_TURN_PORT = 3478
DEFAULT_TURN_TLS_PORT = 5349


class TurnConfig(object):
    """Parsed subset of ``turnserver.conf`` plus the values it implies."""

    def __init__(self):
        self.path = COTURN_CONF
        self.exists = False
        self.values = {}
        self.comments = {}
        self.errors = []
        self.realm = None
        self.secret = None
        self.use_auth_secret = False
        self.lt_cred_mech = False
        self.no_auth = False
        self.stun_only = False
        self.no_stun = False
        self.fingerprint = False
        self.port = DEFAULT_TURN_PORT
        self.tls_port = DEFAULT_TURN_TLS_PORT
        self.alt_port = None
        self.alt_tls_port = None
        self.min_port = DEFAULT_RELAY_MIN
        self.max_port = DEFAULT_RELAY_MAX
        self.external_ip = None
        self.cert = None
        self.pkey = None
        self.listening_ips = []
        self.relay_ips = []
        self.no_tls = False
        self.no_tcp = False
        self.no_udp = False
        self.user = "coturn"
        self.group = "coturn"
        self.raw = ""

    def as_dict(self):
        return {
            "path": self.path,
            "exists": self.exists,
            "realm": self.realm,
            "port": self.port,
            "tls_port": self.tls_port,
            "min_port": self.min_port,
            "max_port": self.max_port,
            "external_ip": self.external_ip,
            "cert": self.cert,
            "pkey": self.pkey,
            "use_auth_secret": self.use_auth_secret,
            "lt_cred_mech": self.lt_cred_mech,
            "no_auth": self.no_auth,
            "stun_only": self.stun_only,
            "no_tls": self.no_tls,
            "user": self.user,
            "group": self.group,
            "errors": list(self.errors),
        }


_FLAG_KEYS = {
    "use-auth-secret", "lt-cred-mech", "no-auth", "no-tls", "stun-only",
    "no-stun", "fingerprint", "no-tcp", "no-udp", "no-dtls", "dtls",
    "no-multicast-peers", "log-binding", "simple-log", "syslog",
}


def _unescape(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def parse_turnserver_conf(path=COTURN_CONF):
    """Parse the simple ``key=value`` and bare-flag syntax of coturn."""
    result = TurnConfig()
    result.path = path
    if not os.path.isfile(path):
        return result
    result.exists = True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        result.errors.append("cannot read %s: %s" % (path, exc))
        return result
    result.raw = text
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            match = re.match(r"#\s*([A-Za-z0-9_-]+)\s*=\s*(.*)$", line)
            if match:
                result.comments[match.group(1).lower()] = _unescape(match.group(2))
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            result.values[key.strip().lower()] = _unescape(value)
        else:
            result.values[line.lower()] = "1"
    values = result.values
    result.realm = values.get("realm")
    result.secret = values.get("static-auth-secret")
    result.use_auth_secret = _flag(values, "use-auth-secret")
    result.lt_cred_mech = _flag(values, "lt-cred-mech")
    result.no_auth = _flag(values, "no-auth")
    result.stun_only = _flag(values, "stun-only")
    result.no_stun = _flag(values, "no-stun")
    result.fingerprint = _flag(values, "fingerprint")
    result.no_tls = _flag(values, "no-tls")
    result.no_tcp = _flag(values, "no-tcp")
    result.no_udp = _flag(values, "no-udp")
    result.port = _int(values.get("listening-port"), DEFAULT_TURN_PORT)
    result.tls_port = _int(values.get("tls-listening-port"), DEFAULT_TURN_TLS_PORT)
    # coturn treats 0 as "listening port plus one"; only flag 0 explicitly.
    result.alt_port = _int(values.get("alt-listening-port"), 0) or None
    result.alt_tls_port = _int(values.get("alt-tls-listening-port"), 0) or None
    result.min_port = _int(values.get("min-port"), DEFAULT_RELAY_MIN)
    result.max_port = _int(values.get("max-port"), DEFAULT_RELAY_MAX)
    result.external_ip = values.get("external-ip")
    result.cert = values.get("cert")
    result.pkey = values.get("pkey")
    result.listening_ips = _multi(values, "listening-ip")
    result.relay_ips = _multi(values, "relay-ip")
    result.user = values.get("user") or "coturn"
    result.group = values.get("group") or result.user
    if result.min_port > result.max_port:
        result.errors.append("min-port is greater than max-port")
    if result.use_auth_secret and result.lt_cred_mech:
        result.errors.append("use-auth-secret and lt-cred-mech are both enabled")
    if result.use_auth_secret and result.no_auth:
        result.errors.append("use-auth-secret and no-auth are both enabled")
    return result


def _flag(values, key):
    if key not in values:
        return False
    return str(values[key]).strip().lower() not in ("0", "off", "no", "false", "f")


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _multi(values, key):
    value = values.get(key)
    if not value:
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def detect_coturn(path=None):
    config = parse_turnserver_conf(path or COTURN_CONF)
    return config


def coturn_installed():
    return bool(which(COTURN_BINARY) or which("turnadmin"))


def coturn_packages(host):
    family = host.family() if hasattr(host, "family") else ""
    if family == "gentoo":
        return ["net-im/coturn"]
    if getattr(host, "system_id", "") == "openbsd":
        return ["coturn"]
    return ["coturn"]


def ensure_coturn(config, options, reporter, prompter):
    """Install coturn when explicitly requested or interactively accepted."""
    if coturn_installed():
        return {"installed": True, "new": False}
    if getattr(options, "install_missing", None) is False:
        return {"installed": False, "declined": True}
    if getattr(options, "dry_run", False):
        reporter.info("Would install coturn")
        # Dry runs still preview the generated configuration.
        return {"installed": True, "new": False, "planned": True}
    if options.install_missing is None:
        if not prompter.interactive:
            return {"installed": False, "declined": True}
        if not prompter.yes_no(
                "coturn is not installed. Install the %s package now?"
                % config.host.package_manager, default=True):
            return {"installed": False, "declined": True}
    from .provision import PackageInstaller
    PackageInstaller(config.host, reporter).install(
        coturn_packages(config.host), why="coturn")
    if not coturn_installed():
        raise ProctorError(
            "coturn was installed but turnserver is not in PATH",
            hint="check the package manager output and whether /usr/bin/turnserver exists",
        )
    return {"installed": True, "new": True}


def turn_realm(domains, explicit=None):
    if explicit:
        return explicit
    if not domains:
        return None
    return "turn." + domains[0]


def turnserver_config_text(turn, secret, realm, cert=None, pkey=None,
                           external_ip=None, min_port=DEFAULT_RELAY_MIN,
                           max_port=DEFAULT_RELAY_MAX, port=DEFAULT_TURN_PORT,
                           tls_port=DEFAULT_TURN_TLS_PORT, fingerprint=True,
                           no_tls=False, preserve=None):
    """Render a complete, commented turnserver.conf for a fresh install.

    ``preserve`` is the previous file; its non-Proctor lines are kept so local
    tweaks survive a re-run.
    """
    lines = [
        "# Managed by proctor. Local additions below are preserved.",
        "listening-port=%d" % int(port),
        "tls-listening-port=%d" % int(tls_port),
        "min-port=%d" % int(min_port),
        "max-port=%d" % int(max_port),
        "",
        "# TURN REST API (mod_turn_external shared secret)",
        "realm=%s" % realm,
        "use-auth-secret",
        "static-auth-secret=%s" % secret,
    ]
    if fingerprint:
        lines.append("fingerprint")
    lines.extend([
        "",
        "# Service account (matches the packaged coturn unit)",
        "user=%s" % (turn.user or "coturn"),
        "group=%s" % (turn.group or "coturn"),
    ])
    if external_ip:
        lines.extend(["", "# Public address behind NAT", "external-ip=%s" % external_ip])
    if cert and pkey and not no_tls:
        lines.extend([
            "",
            "# TLS/DTLS certificates (also used for TURN over TLS on tls-listening-port)",
            "cert=%s" % cert,
            "pkey=%s" % pkey,
        ])
    elif no_tls:
        lines.append("no-tls")
    extras = _preserved_lines(preserve)
    if extras:
        lines.extend(["", "# Preserved from the previous configuration"] + extras)
    return "\n".join(lines) + "\n"


_MANAGED_PREFIXES = (
    "listening-port", "tls-listening-port", "min-port", "max-port", "realm",
    "use-auth-secret", "static-auth-secret", "fingerprint", "user", "group",
    "external-ip", "cert", "pkey", "no-tls", "listening-ip", "relay-ip",
    "no-udp", "no-tcp", "no-stun", "stun-only", "verbose", "log-file",
    "simple-log", "no-stdout-log", "syslog", "stale-nonce",
)


def _preserved_lines(previous):
    if previous is None or not getattr(previous, "exists", False):
        return []
    text = getattr(previous, "raw", "") or ""
    if not text and os.path.isfile(previous.path):
        try:
            with open(previous.path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return []
    result = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split("=", 1)[0].strip().lower()
        if key in _MANAGED_PREFIXES:
            continue
        result.append(line)
    return result


def _prosody_secret(prosody_config):
    """Return turn_external_secret from the Prosody config, if present."""
    if not prosody_config or not os.path.isfile(prosody_config):
        return None
    try:
        from .prosody import parse_prosody_config
        parsed = parse_prosody_config(prosody_config)
    except Exception:
        return None
    return parsed.get("values", {}).get("turn_external_secret")


def generate_secret():
    import binascii
    return binascii.hexlify(os.urandom(32)).decode("ascii")


def apply_turnserver_config(turn, secret, realm, cert=None, pkey=None,
                            external_ip=None, dry_run=False, reporter=None):
    text = turnserver_config_text(
        turn, secret, realm, cert=cert, pkey=pkey, external_ip=external_ip,
        port=turn.port if turn.exists else DEFAULT_TURN_PORT,
        tls_port=turn.tls_port if turn.exists else DEFAULT_TURN_TLS_PORT,
        min_port=turn.min_port if turn.exists else DEFAULT_RELAY_MIN,
        max_port=turn.max_port if turn.exists else DEFAULT_RELAY_MAX,
        # A fresh install without a certificate must not try to serve TLS with
        # coturn's built-in self-signed defaults. A certificate supplied by
        # setup re-enables TLS even if the previous config had no-tls set.
        no_tls=not (cert and pkey),
    )
    if dry_run:
        return text
    ensure_dir(os.path.dirname(turn.path), mode=0o755)
    if not turn.path and turn.exists:
        turn.path = COTURN_CONF
    if os.path.exists(turn.path):
        stamp = _timestamp()
        backup = turn.path + ".pre-proctor-" + stamp
        try:
            import shutil
            shutil.copy2(turn.path, backup)
            if reporter is not None:
                reporter.info("Saved existing coturn config to %s" % backup)
        except OSError:
            pass
    atomic_write(turn.path, text.encode("utf-8"), mode=0o640)
    _chown(turn.path, uid_for("root"), gid_for(turn.group))
    _label_coturn_files()
    return text


def _timestamp():
    import time
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _chown(path, uid, gid):
    if not is_root() or uid is None or gid is None:
        return
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def _label_coturn_files(dry_run=False):
    """Make sure SELinux knows about coturn's files after a manual restore."""
    if dry_run or not is_root() or not os.path.isdir("/sys/fs/selinux"):
        return
    if which("restorecon"):
        run(["restorecon", "-R", COTURN_ROOT], check=False)


# ---------------------------------------------------------------------------
# Web server (ACME / optional HTTPS) for the TURN hostname

def turn_web_config_text(kind, domain, cert_name=None, tls=False, webroot="/var/www/html"):
    root = "/etc/letsencrypt/live/%s" % (cert_name or domain)
    if kind == "nginx":
        lines = [
            "# Managed by proctor: ACME + HTTPS for the TURN hostname.",
            "server {",
            "    listen 80;",
            "    listen [::]:80;",
            "    server_name %s;" % domain,
            "",
            "    location ^~ /.well-known/acme-challenge/ {",
            "        root %s;" % webroot,
            "        default_type text/plain;",
            "        try_files $uri =404;",
            "    }",
            "",
            "    location / {",
            "        return 404;",
            "    }",
            "}",
        ]
        if tls:
            lines.extend([
                "server {",
                "    listen 443 ssl;",
                "    listen [::]:443 ssl;",
                "    server_name %s;" % domain,
                "    ssl_certificate     %s/fullchain.pem;" % root,
                "    ssl_certificate_key %s/privkey.pem;" % root,
                "    ssl_protocols TLSv1.2 TLSv1.3;",
                "    location / { return 404; }",
                "}",
            ])
        return "\n".join(lines) + "\n"
    if kind == "apache":
        lines = [
            "# Managed by proctor: ACME + HTTPS for the TURN hostname.",
            "<VirtualHost *:80>",
            "    ServerName %s" % domain,
            "    DocumentRoot %s" % webroot,
            "    <Directory %s>" % webroot,
            "        Require all granted",
            "    </Directory>",
            "    Alias /.well-known/acme-challenge/ %s/.well-known/acme-challenge/" % webroot,
            "</VirtualHost>",
        ]
        if tls:
            lines.extend([
                "<IfModule mod_ssl.c>",
                "<VirtualHost *:443>",
                "    ServerName %s" % domain,
                "    SSLEngine on",
                "    SSLCertificateFile %s/fullchain.pem" % root,
                "    SSLCertificateKeyFile %s/privkey.pem" % root,
                "    DocumentRoot %s" % webroot,
                "    <Directory %s>" % webroot,
                "        Require all granted",
                "    </Directory>",
                "</VirtualHost>",
                "</IfModule>",
            ])
        return "\n".join(lines) + "\n"
    raise ConfigError("unsupported web server for TURN: %s" % kind)


def ensure_turn_acme_webroot(web, dry_run=False, reporter=None):
    """Make /var/www/html writable by the web server and label it for SELinux."""
    if dry_run:
        return False
    from .provision import WEBROOT
    if web.kind == "nginx":
        user = "www-data" if _is_debian() else "nginx"
    else:
        user = "www-data" if _is_debian() else "apache"
    ensure_dir(os.path.join(WEBROOT, ".well-known", "acme-challenge"), mode=0o755)
    uid, gid = uid_for(user), gid_for(user)
    if uid is not None and gid is not None:
        _chown(WEBROOT, uid, gid)
        _chown(os.path.join(WEBROOT, ".well-known"), uid, gid)
        _chown(os.path.join(WEBROOT, ".well-known", "acme-challenge"), uid, gid)
    if is_root() and os.path.isdir("/sys/fs/selinux") and which("restorecon"):
        run(["restorecon", "-R", WEBROOT], check=False)
    return True


def _is_debian():
    try:
        with open("/etc/os-release", "r", errors="replace") as handle:
            for line in handle:
                if line.startswith("ID_LIKE=") and "debian" in line.lower():
                    return True
                if line.startswith("ID=") and (
                        "debian" in line.lower() or "ubuntu" in line.lower()):
                    return True
    except OSError:
        pass
    return False


def apply_turn_web_config(config, web, domain, cert_name=None, tls=False,
                          dry_run=False, reporter=None):
    """Write the TURN vhost through the existing web-server machinery."""
    from .provision import apply_web_config
    from .provision import WebServerInfo
    ensure_turn_acme_webroot(web, dry_run=dry_run, reporter=reporter)
    text = turn_web_config_text(web.kind, domain, cert_name=cert_name, tls=tls)
    if not hasattr(web, "conf_dir"):
        # Explicit --webserver choice: fill in the layout normally supplied by
        # detect_webserver so the vhost lands in the right directory.
        info = WebServerInfo()
        info.kind = web.kind
        info.config_root = "/etc/nginx" if web.kind == "nginx" else "/etc/httpd"
        info.main_config = os.path.join(
            info.config_root, "nginx.conf" if web.kind == "nginx" else "httpd.conf")
        info.conf_dir = os.path.join(info.config_root, "conf.d")
        info.sites_available = None
        info.sites_enabled = None
        info.service = "nginx" if web.kind == "nginx" else "httpd"
        info.test_command = [info.kind, "-t"] if web.kind == "nginx" else ["httpd", "-t"]
        info.installed = True
        web = info
    if dry_run:
        return {"path": None, "text": text, "kind": web.kind}
    result = apply_web_config(config, web, text, reporter, dry_run=False)
    result["text"] = text
    return result


# ---------------------------------------------------------------------------
# Firewall

def turn_ports(turn):
    """Ports that must be reachable for STUN/TURN/DTLS and TURN over TLS."""
    ports = set()
    if not turn.no_udp or not turn.no_tcp:
        ports.add(int(turn.port))
        if turn.alt_port:
            ports.add(int(turn.alt_port))
    if not turn.no_tls:
        ports.add(int(turn.tls_port))
        if turn.alt_tls_port:
            ports.add(int(turn.alt_tls_port))
    return sorted(ports)


def _ufw_active():
    if not which("ufw"):
        return None
    proc = run(["ufw", "status"], check=False)
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return "Status: active" in text


def _firewalld_active():
    if not which("firewall-cmd"):
        return None
    proc = run(["firewall-cmd", "--state"], check=False)
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return "running" in text


def _firewalld_zone():
    proc = run(["firewall-cmd", "--get-default-zone"], check=False)
    value = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return value or "public"


def turn_firewalld_service_xml(turn):
    """firewalld service definition, including the UDP relay range."""
    ports = []
    if not turn.no_udp:
        ports.append(("udp", str(turn.port)))
        if not turn.no_tls:
            ports.append(("udp", str(turn.tls_port)))
        ports.append(("udp", "%d-%d" % (turn.min_port, turn.max_port)))
    if not turn.no_tcp:
        ports.append(("tcp", str(turn.port)))
        if not turn.no_tls:
            ports.append(("tcp", str(turn.tls_port)))
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<service>",
        "  <short>coturn</short>",
        "  <description>STUN/TURN for XMPP calls (Prosody turn_external)</description>",
    ]
    for protocol, port in ports:
        lines.append('  <port protocol="%s" port="%s"/>' % (protocol, port))
    lines.append("</service>")
    return "\n".join(lines) + "\n"


def ensure_turn_firewall(turn, options, dry_run=False, reporter=None):
    value = getattr(options, "firewall", "auto") or "auto"
    ports = turn_ports(turn)
    result = {"backend": None, "ports": ports, "status": "skipped",
              "relay_range": "%d-%d" % (turn.min_port, turn.max_port)}
    if dry_run:
        result["status"] = "would-open"
        result["backend"] = "firewalld" if _firewalld_active() else (
            "ufw" if _ufw_active() else None)
        return result
    active_ufw = _ufw_active()
    if active_ufw:
        result["backend"] = "ufw"
        result["status"] = "would-open" if value == "auto" else value
        if value == "no":
            return result
        for port in ports:
            run(["ufw", "allow", "%d/udp" % port], check=False)
            run(["ufw", "allow", "%d/tcp" % port], check=False)
        run(["ufw", "allow", "%d:%d/udp" % (turn.min_port, turn.max_port)], check=False)
        result["status"] = "opened"
        return result
    if _firewalld_active():
        zone = _firewalld_zone()
        result["backend"] = "firewalld"
        result["zone"] = zone
        service = "coturn"
        path = "/etc/firewalld/services/%s.xml" % service
        ensure_dir(os.path.dirname(path), mode=0o755)
        atomic_write(path, turn_firewalld_service_xml(turn).encode("utf-8"), mode=0o644)
        run(["firewall-cmd", "--reload"], check=False)
        run(["firewall-cmd", "--permanent", "--zone=%s" % zone,
             "--add-service=%s" % service], check=False)
        run(["firewall-cmd", "--reload"], check=False)
        result["status"] = "opened"
        return result
    result["status"] = "inactive"
    return result


# ---------------------------------------------------------------------------
# SELinux

def turn_selinux_ports(turn):
    ports = set()
    if not turn.no_udp or not turn.no_tcp:
        ports.add((turn.port, "udp"))
        ports.add((turn.port, "tcp"))
    if not turn.no_tls:
        ports.add((turn.tls_port, "udp"))
        ports.add((turn.tls_port, "tcp"))
    return sorted(ports)


def _selinux_enabled():
    return os.path.isdir("/sys/fs/selinux") and which("getenforce")


def selinux_state():
    if not _selinux_enabled():
        return "disabled"
    proc = run(["getenforce"], check=False)
    value = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
        "utf-8", "replace").strip().lower()
    return value or "unknown"


def ensure_turn_selinux(turn, dry_run=False, reporter=None):
    state = selinux_state()
    result = {"state": state, "ports": [], "status": "skipped", "errors": []}
    if dry_run or state in ("disabled", "permissive"):
        result["status"] = "not-needed" if state == "disabled" else "skipped"
        return result
    if not which("semanage"):
        result["errors"].append("semanage is not installed (policycoreutils-python-utils)")
        return result
    for port, protocol in turn_selinux_ports(turn):
        proc = run(["semanage", "port", "-a", "-t", "turn_port_t",
                    "-p", protocol, str(port)], check=False)
        if proc.returncode == 0 or _semanage_exists(port, protocol):
            result["ports"].append("%d/%s" % (port, protocol))
        else:
            # turn_port_t may not exist on older policies; fall back to the
            # generic unreserved type so the listener is not denied.
            proc = run(["semanage", "port", "-a", "-t", "unreserved_port_t",
                        "-p", protocol, str(port)], check=False)
            if proc.returncode == 0:
                result["ports"].append("%d/%s" % (port, protocol))
            else:
                output = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
                    "utf-8", "replace").strip()
                if "already defined" not in output:
                    result["errors"].append("%d/%s: %s" % (port, protocol, output))
    _label_coturn_files()
    result["status"] = "ok" if not result["errors"] else "partial"
    if reporter is not None and result["errors"]:
        reporter.warn("SELinux port labelling incomplete: %s" % "; ".join(result["errors"]))
    return result


def _semanage_exists(port, protocol):
    if not which("semanage"):
        return False
    proc = run(["semanage", "port", "-l"], check=False)
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    pattern = re.compile(
        r"^(?:turn_port_t|unreserved_port_t)\s+%s\s+.*\b%d\b" % (protocol, port),
        re.M)
    return bool(pattern.search(text))


# ---------------------------------------------------------------------------
# Certificates

def grant_turn_certificate_access(turn, dry_run=False, reporter=None):
    """Let coturn read the Let's Encrypt key like Prosody does."""
    from .provision import CERTBOT_ROOT, CERTBOT_LIVE, CERTBOT_ARCHIVE
    if dry_run or not os.path.isdir(CERTBOT_ROOT):
        return False
    user = turn.user or "coturn"
    if which("setfacl") and uid_for(user) is not None:
        commands = [
            ["setfacl", "-m", "u:%s:x" % user, CERTBOT_ROOT],
            ["setfacl", "-R", "-m", "u:%s:rX" % user, CERTBOT_LIVE],
            ["setfacl", "-R", "-m", "u:%s:rX" % user, CERTBOT_ARCHIVE],
        ]
        for command in commands:
            if os.path.isdir(command[-1]):
                run(command, check=False)
        return True
    gid = gid_for(turn.group)
    if gid is None:
        return False
    for base in (CERTBOT_LIVE, CERTBOT_ARCHIVE):
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if name.startswith("privkey"):
                    path = os.path.join(dirpath, name)
                    try:
                        os.chown(path, -1, gid)
                        os.chmod(path, 0o640)
                    except OSError:
                        pass
    return True


def cert_paths(turn):
    """Return the certificate/key coturn will actually serve."""
    if turn.cert and turn.pkey:
        return turn.cert, turn.pkey
    return None, None


# ---------------------------------------------------------------------------
# Service control

def restart_coturn(turn, dry_run=False):
    if dry_run:
        return {"status": "dry-run"}
    if which("systemctl"):
        proc = run(["systemctl", "enable", "--now", COTURN_SERVICE], check=False)
        if proc.returncode != 0:
            output = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
                "utf-8", "replace").strip()
            return {"status": "failed", "error": output.splitlines()[-1]
                    if output else "systemctl failed"}
        return {"status": "started"}
    if which("rc-service"):
        run(["rc-service", COTURN_SERVICE, "restart"], check=False)
        return {"status": "restarted"}
    if which("service"):
        run(["service", COTURN_SERVICE, "restart"], check=False)
        return {"status": "restarted"}
    return {"status": "unknown"}


def validate_turnserver_conf(turn, dry_run=False):
    """Start turnserver briefly against the config and inspect the result.

    coturn has no separate config-test mode, so we ask for the version with
    ``-c``: it parses the configuration and exits before binding listeners.
    """
    if dry_run:
        return True, "dry-run"
    if not which(COTURN_BINARY):
        return None, "turnserver not installed"
    proc = run([COTURN_BINARY, "-c", turn.path, "--version"],
               check=False, timeout=15)
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return proc.returncode == 0, output.strip()


# ---------------------------------------------------------------------------
# Checks

def _socket_stun(host, port, timeout=3):
    packet = struct.pack(">HHI12s", 0x0001, 0x0000, 0x2112A442, b"proctorcheck")
    query = packet
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, (host, port))
        data, _addr = sock.recvfrom(2048)
    except (socket.error, OSError):
        return None
    finally:
        sock.close()
    if len(data) < 20:
        return None
    msg_type = struct.unpack(">H", data[:2])[0]
    if msg_type not in (0x0101, 0x0111, 0x0103):
        return None
    return data


def _stun_attributes(data):
    attributes = {}
    offset = 20
    while offset + 4 <= len(data):
        kind, length = struct.unpack(">HH", data[offset:offset + 4])
        value = data[offset + 4:offset + 4 + length]
        attributes[kind] = value
        offset += 4 + ((length + 3) & ~3)
    return attributes


def check_stun(host, port, timeout=3):
    data = _socket_stun(host, port, timeout)
    if data is None:
        return {"ok": False, "error": "no STUN response from %s:%d" % (host, port)}
    attributes = _stun_attributes(data)
    if "software" not in attributes and 0x8022 not in attributes:
        pass
    return {"ok": True, "port": port}


REALM_ATTR = 0x0014
NONCE_ATTR = 0x0015


def check_turn_auth(host, port, realm=None, timeout=3):
    """Send an unauthenticated ALLOCATE and inspect the 401 challenge."""
    data = _socket_stun_allocate(host, port, timeout)
    if data is None:
        return {"ok": False, "error": "no TURN response from %s:%d" % (host, port)}
    msg_type = struct.unpack(">H", data[:2])[0]
    attributes = _stun_attributes(data)
    if msg_type == 0x0103:
        # Allocate success without auth: coturn is in no-auth mode.
        return {"ok": False, "error": "TURN allocate was accepted without credentials"}
    if msg_type != 0x0113:
        return {"ok": False,
                "error": "unexpected TURN response 0x%04x (expected 401)" % msg_type}
    returned_realm = attributes.get(REALM_ATTR)
    returned_nonce = attributes.get(NONCE_ATTR)
    result = {
        "ok": bool(returned_realm and returned_nonce),
        "realm": returned_realm.decode("utf-8", "replace") if returned_realm else None,
        "nonce": returned_nonce is not None,
    }
    if realm and result["realm"] and result["realm"] != realm:
        result["ok"] = False
        result["error"] = "realm mismatch: coturn says %r, Prosody expects %r" % (
            result["realm"], realm)
    elif not returned_realm:
        result["error"] = "TURN server did not return an authentication realm"
    elif not returned_nonce:
        result["error"] = "TURN server did not return a nonce"
    return result


def _socket_stun_allocate(host, port, timeout=3):
    transaction = os.urandom(12)
    header = struct.pack(">HHI12s", 0x0003, 0x0000, 0x2112A442, transaction)
    # REQUESTED-TRANSPORT = UDP (0x0019)
    attribute = struct.pack(">HHB", 0x0019, 4, 17) + b"\x00\x00\x00"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(header + attribute, (host, port))
        data, _addr = sock.recvfrom(2048)
    except (socket.error, OSError):
        return None
    finally:
        sock.close()
    if len(data) < 20:
        return None
    return data


def firewall_state(turn):
    ufw = _ufw_active()
    firewalld = _firewalld_active()
    expected = set(turn_ports(turn))
    detail = {"ufw": bool(ufw), "firewalld": bool(firewalld), "ports": sorted(expected)}
    missing = []
    if ufw:
        proc = run(["ufw", "status", "numbered"], check=False)
        text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        for port in expected:
            if not re.search(r"\b%d(/tcp|/udp)?\b" % port, text):
                missing.append(port)
        detail["backend"] = "ufw"
    elif firewalld:
        proc = run(["firewall-cmd", "--list-all"], check=False)
        text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        if "coturn" in text:
            # Proctor opens the coturn service, whose XML contains the ports;
            # --list-all only prints the service name.
            info = run(["firewall-cmd", "--info-service=coturn"], check=False)
            text += ((info.stdout or b"") + (info.stderr or b"")).decode(
                "utf-8", "replace")
        for port in expected:
            if str(port) not in text:
                missing.append(port)
        detail["backend"] = "firewalld"
    else:
        detail["backend"] = None
    detail["missing"] = missing
    if detail["backend"] and missing:
        detail["ok"] = False
    elif not detail["backend"]:
        detail["ok"] = None
    else:
        detail["ok"] = True
    return detail


def turn_checks(config, install):
    """Return (name, status, detail) triples for ``doctor``."""
    checks = []
    turn = detect_coturn()
    if not turn.exists and not coturn_installed():
        return checks
    checks.append(("coturn", "ok" if coturn_installed() else "fail",
                   "installed" if coturn_installed() else "turnserver binary missing"))
    checks.append(("coturn-config", "ok" if turn.exists else "warn",
                   turn.path if turn.exists else "no %s" % turn.path))
    if turn.exists:
        if turn.realm:
            checks.append(("coturn-realm", "ok", turn.realm))
        else:
            checks.append(("coturn-realm", "fail", "realm is not set"))
        auth = turn.use_auth_secret or turn.lt_cred_mech
        if turn.no_auth:
            checks.append(("coturn-auth", "fail", "no-auth is enabled"))
        elif turn.use_auth_secret and turn.secret:
            checks.append(("coturn-auth", "ok", "TURN REST API secret configured"))
        elif auth:
            checks.append(("coturn-auth", "warn",
                           "lt-cred-mech without a static secret (mod_turn_external needs use-auth-secret)"))
        else:
            checks.append(("coturn-auth", "fail", "no use-auth-secret / static-auth-secret"))
        if turn.stun_only:
            checks.append(("coturn-mode", "fail", "stun-only is enabled; TURN allocations are disabled"))
        for error in turn.errors:
            checks.append(("coturn-config-error", "fail", error))
        checks.append(("coturn-relay", "ok", "%d-%d UDP" % (turn.min_port, turn.max_port)))
        if turn.no_tls:
            checks.append(("coturn-tls", "warn", "no-tls is set; TURN over TLS is disabled"))
        elif turn.cert and turn.pkey:
            for path in (turn.cert, turn.pkey):
                status = "ok" if os.path.isfile(path) else "fail"
                checks.append(("coturn-certificate", status, path))
        else:
            checks.append(("coturn-tls", "warn", "no cert/pkey; only plain TURN/STUN"))
    host = "127.0.0.1"
    if coturn_installed():
        stun = check_stun(host, turn.port)
        checks.append(("coturn-stun", "ok" if stun["ok"] else "warn",
                       "127.0.0.1:%d" % turn.port if stun["ok"] else stun["error"]))
        auth = check_turn_auth(host, turn.port, realm=turn.realm)
        status = "ok" if auth["ok"] else "warn"
        detail = auth.get("realm") or auth.get("error") or "TURN auth reachable"
        checks.append(("coturn-turn", status, detail))
    fw = firewall_state(turn)
    if fw["backend"]:
        if fw["missing"]:
            checks.append(("coturn-firewall", "fail",
                           "%s missing ports: %s" % (fw["backend"], ", ".join(
                               str(port) for port in fw["missing"]))))
        else:
            checks.append(("coturn-firewall", "ok", "%s allows TURN ports" % fw["backend"]))
    state = selinux_state()
    if state == "enforcing":
        lacking = []
        for port, protocol in turn_selinux_ports(turn):
            if not _semanage_exists(port, protocol):
                lacking.append("%d/%s" % (port, protocol))
        checks.append(("coturn-selinux", "fail" if lacking else "ok",
                       ("missing port labels: %s" % ", ".join(lacking)) if lacking
                       else "port labels present"))
        if not os.path.isdir(COTURN_ROOT):
            checks.append(("coturn-selinux-files", "fail", "%s is missing" % COTURN_ROOT))
    if install is not None:
        enabled = enabled_turn_modules(install)
        if enabled:
            if enabled == ["turn_external"]:
                secret = install.parsed.get("values", {}).get("turn_external_secret")
                if not secret:
                    checks.append(("prosody-turn", "fail", "turn_external enabled without turn_external_secret"))
                elif turn.secret and secret != turn.secret:
                    checks.append(("prosody-turn", "fail",
                                   "turn_external_secret does not match coturn static-auth-secret"))
                else:
                    checks.append(("prosody-turn", "ok", "turn_external configured"))
            else:
                checks.append(("prosody-turn", "warn",
                               "other TURN modules enabled: %s" % ", ".join(enabled)))
        if "turn_external" in (install.modules_enabled or []) and turn.realm:
            configured_host = install.parsed.get("values", {}).get("turn_external_host")
            if configured_host and configured_host != turn.realm:
                checks.append(("prosody-turn-host", "warn",
                               "Prosody advertises %s but coturn realm is %s"
                               % (configured_host, turn.realm)))
    return checks


def enabled_turn_modules(install):
    modules = list(getattr(install, "modules_enabled", []) or [])
    return [name for name in modules
            if name in ("turn_external", "external_services", "turncredentials")]


def prosody_turn_settings(domain, secret, port=DEFAULT_TURN_PORT):
    return {
        "turn_external_host": "turn." + domain if not domain.startswith("turn.") else domain,
        "turn_external_port": port,
        "turn_external_secret": secret,
    }


def setup_turn(config, options, reporter, prompter, domains, web, certbot_wanted,
               email=None, cert_name=None, realm=None):
    """Provision coturn end-to-end and return a report dictionary."""
    turn = detect_coturn()
    installed = ensure_coturn(config, options, reporter, prompter)
    result = {"installed": installed, "config": None, "web": None,
              "firewall": None, "selinux": None, "certificates": None,
              "secret": None, "realm": None, "dry_run": bool(options.dry_run)}
    if not installed.get("installed"):
        return result
    domain = realm or turn_realm(domains, getattr(options, "turn_realm", None)
                                 or getattr(options, "turn_host", None))
    result["realm"] = domain
    secret = getattr(options, "turn_secret", None) or generate_secret()
    result["secret"] = secret
    cert = pkey = None
    if cert_name:
        from .provision import has_certificate, certificate_root
        if has_certificate(cert_name):
            root = certificate_root(cert_name)
            cert = os.path.join(root, "fullchain.pem")
            pkey = os.path.join(root, "privkey.pem")
    if web is not None:
        try:
            result["web"] = apply_turn_web_config(
                config, web, domain, cert_name=cert_name, tls=bool(cert),
                dry_run=bool(options.dry_run), reporter=reporter)
        except ProctorError as exc:
            reporter.warn("could not write TURN vhost: %s" % exc)
    text = apply_turnserver_config(
        turn, secret, domain, cert=cert, pkey=pkey,
        external_ip=getattr(options, "turn_external_ip", None),
        dry_run=bool(options.dry_run), reporter=reporter)
    result["config"] = text
    result["config_path"] = COTURN_CONF
    if options.dry_run:
        result["firewall"] = ensure_turn_firewall(turn, options, dry_run=True)
        result["selinux"] = {"status": "dry-run"}
        return result
    grant_turn_certificate_access(turn, reporter=reporter)
    result["certificates"] = "granted"
    result["selinux"] = ensure_turn_selinux(turn, reporter=reporter)
    result["firewall"] = ensure_turn_firewall(turn, options, reporter=reporter)
    result["service"] = restart_coturn(turn)
    return result


def patch_prosody_config(prosody_config, settings, dry_run=False):
    """Add turn_external + its settings to a generated Prosody config."""
    if not prosody_config or not os.path.isfile(prosody_config):
        return False
    try:
        with open(prosody_config, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return False
    if not re.search(r'(?m)^\s*"turn_external"\s*;', text) \
            and "turn_external" not in text:
        text = re.sub(r"(modules_enabled\s*=\s*\{)", r'\1\n    "turn_external";',
                      text, count=1)
    missing = []
    for key, value in settings.items():
        pattern = r"(?m)^(\s*%s\s*=\s*).*$" % re.escape(key)
        if re.search(pattern, text):
            text = re.sub(pattern,
                          lambda m: m.group(1) + _lua_value(value), text, count=1)
        else:
            missing.append((key, value))
    if missing:
        text = text.rstrip("\n") + "\n\n" + "\n".join(
            "%s = %s" % (key, _lua_value(value)) for key, value in missing) + "\n"
    if dry_run:
        return text
    atomic_write(prosody_config, text.encode("utf-8"), mode=0o644)
    return True


def _lua_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    import json
    return json.dumps(str(value), ensure_ascii=True)
