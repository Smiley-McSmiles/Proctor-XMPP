"""Prosody installation discovery and a small, safe Lua-config reader."""

from __future__ import print_function

import glob
import os
import re
import urllib.parse

from .errors import DetectionError
from .hostinfo import derive_data_dir, derive_log_dir, find_config
from .util import is_within, run, uid_name, gid_name, which

CERT_KEYS = {
    "certificate", "key", "certificate_chain", "ssl_certificate", "ssl_key",
    "cafile", "capath",
}

SQL_DRIVERS = {
    "sqlite": "sqlite", "sqlite3": "sqlite",
    "postgresql": "postgres", "postgres": "postgres", "pgsql": "postgres",
    "mysql": "mysql", "mariadb": "mysql",
}


def _long_bracket_end(text, start):
    """If text[start] starts a Lua long bracket, return (content_start, level)."""
    if start >= len(text) or text[start] != "[":
        return None
    index = start + 1
    level = 0
    while index < len(text) and text[index] == "=":
        level += 1
        index += 1
    if index < len(text) and text[index] == "[":
        return index + 1, level
    return None


def strip_lua_comments(text):
    """Remove Lua comments while preserving strings and line structure."""
    output = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in ("'", '"'):
            quote = char
            output.append(char)
            index += 1
            while index < length:
                current = text[index]
                output.append(current)
                if current == "\\" and index + 1 < length:
                    index += 1
                    output.append(text[index])
                elif current == quote:
                    index += 1
                    break
                index += 1
            continue
        bracket = _long_bracket_end(text, index)
        if bracket is not None:
            content_start, level = bracket
            closing = "]" + ("=" * level) + "]"
            end = text.find(closing, content_start)
            if end == -1:
                output.append(text[index:])
                break
            output.append(text[index:end + len(closing)])
            index = end + len(closing)
            continue
        if char == "-" and index + 1 < length and text[index + 1] == "-":
            bracket = _long_bracket_end(text, index + 2)
            if bracket is not None:
                content_start, level = bracket
                closing = "]" + ("=" * level) + "]"
                end = text.find(closing, content_start)
                if end == -1:
                    break
                skipped = text[index:end + len(closing)]
                output.append("\n" * skipped.count("\n"))
                index = end + len(closing)
                continue
            end = text.find("\n", index)
            if end == -1:
                break
            output.append("\n")
            index = end + 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


ASSIGN_RE = re.compile(
    r"(?<![\w.])([A-Za-z_][\w.]*)\s*=\s*(?:\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)')",
    re.MULTILINE,
)
QUOTED_RE = re.compile(r"\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'")


def _balanced_block(text, start):
    """Return the contents of a Lua { ... } block beginning at start."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char in ("'", '"'):
            quote = char
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    break
                index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
        index += 1
    return None


def _table_for(text, key):
    match = re.search(r"(?<![\w.])" + re.escape(key) + r"\s*=\s*\{", text)
    if not match:
        return None
    return _balanced_block(text, match.end() - 1)


def _strings(value):
    result = []
    for match in QUOTED_RE.finditer(value or ""):
        result.append(_unescape(match.group(1) if match.group(1) is not None else match.group(2)))
    return result


def _assignments(text):
    values = {}
    for match in ASSIGN_RE.finditer(text):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        # Last assignment wins, matching Lua execution order closely enough
        # for the settings Proctor consumes.
        values[key] = _unescape(value)
    return values


def _unescape(value):
    if value is None:
        return None
    return value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'")


def _resolve(base_dir, value):
    if not value:
        return None
    value = os.path.expanduser(os.path.expandvars(str(value)))
    if not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return os.path.normpath(value)


def parse_prosody_config(path, _seen=None, _depth=0):
    """Parse the subset of Prosody's Lua config that Proctor needs."""
    path = os.path.abspath(path)
    if _seen is None:
        _seen = set()
    if path in _seen or _depth > 8:
        return _empty_parse(path)
    _seen.add(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DetectionError("cannot read Prosody config %s: %s" % (path, exc))
    text = strip_lua_comments(raw)
    config_dir = os.path.dirname(path)
    values = _assignments(text)
    result = _empty_parse(path)
    result["values"] = values
    result["text"] = text
    result["config_dir"] = config_dir
    result["virtualhosts"] = _regex_strings(text, r"(?m)^\s*VirtualHost\s+[\"']([^\"']+)[\"']")
    result["components"] = _regex_strings(text, r"(?m)^\s*Component\s+[\"']([^\"']+)[\"']")
    result["plugin_paths"] = [_resolve(config_dir, item) for item in _strings(_table_for(text, "plugin_paths"))]
    result["modules"] = _strings(_table_for(text, "modules_enabled"))
    log_block = _table_for(text, "log")
    log_values = (_strings_from_regex(log_block, r"filename\s*=\s*[\"']([^\"']+)[\"']")
                  if log_block else [])
    if not log_values:
        log_values = _strings_from_regex(text, r"(?m)^\s*filename\s*=\s*[\"']([^\"']+)[\"']")
    result["log_files"] = [_resolve(config_dir, item) for item in log_values]
    result["certificate_files"] = [
        _resolve(config_dir, values[key]) for key in values
        if key in ("certificate", "ssl_certificate", "certificate_chain") and values[key]
    ]
    result["key_files"] = [
        _resolve(config_dir, values[key]) for key in values
        if key in ("key", "ssl_key") and values[key]
    ]
    if values.get("cafile"):
        result["certificate_dirs"].append(os.path.dirname(_resolve(config_dir, values["cafile"])))
    if values.get("capath"):
        result["certificate_dirs"].append(_resolve(config_dir, values["capath"]))

    cert_dir_value = values.get("certificates")
    if cert_dir_value:
        result["certificate_dirs"].append(_resolve(config_dir, cert_dir_value))

    sql_block = _table_for(text, "sql")
    if sql_block:
        sql_values = _assignments(sql_block)
        port_match = re.search(r"(?<![\w.])port\s*=\s*(\d+)", sql_block)
        if port_match:
            sql_values["port"] = int(port_match.group(1))
        result["sql"] = sql_values
        if sql_values.get("password"):
            result["sql_has_password"] = True

    includes = []
    for pattern in _regex_strings(text, r"(?m)^\s*Include\s+[\"']([^\"']+)[\"']"):
        includes.append(_resolve(config_dir, pattern))
    for pattern in includes:
        for included in sorted(glob.glob(pattern)):
            if os.path.isfile(included):
                result["includes"].append(included)
                child = parse_prosody_config(included, _seen=_seen, _depth=_depth + 1)
                result["virtualhosts"].extend(child["virtualhosts"])
                result["components"].extend(child["components"])
                result["modules"].extend(child["modules"])
                result["plugin_paths"].extend(child["plugin_paths"])
                result["log_files"].extend(child["log_files"])
                result["certificate_files"].extend(child["certificate_files"])
                result["key_files"].extend(child["key_files"])
                result["certificate_dirs"].extend(child["certificate_dirs"])
                if not result["sql"] and child["sql"]:
                    result["sql"] = child["sql"]

    result["virtualhosts"] = _unique(result["virtualhosts"])
    result["components"] = _unique(result["components"])
    result["modules"] = _unique(result["modules"])
    result["plugin_paths"] = _unique(result["plugin_paths"])
    result["log_files"] = _unique(result["log_files"])
    result["certificate_files"] = _unique(result["certificate_files"])
    result["key_files"] = _unique(result["key_files"])
    result["certificate_dirs"] = _unique(result["certificate_dirs"])
    return result


def _regex_strings(text, pattern):
    return [match.group(1) for match in re.finditer(pattern, text)]


def _strings_from_regex(text, pattern):
    return [match.group(1) for match in re.finditer(pattern, text or "")]


def _table_numbers(text, key):
    block = _table_for(text, key)
    if not block:
        return []
    return [int(value) for value in re.findall(r"\b(\d{2,5})\b", block)]


def _table_strings(text, key):
    block = _table_for(text, key)
    if not block:
        return []
    return _strings(block)


def _unique(values):
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _empty_parse(path):
    return {
        "path": path,
        "config_dir": os.path.dirname(path),
        "values": {},
        "virtualhosts": [],
        "components": [],
        "plugin_paths": [],
        "modules": [],
        "log_files": [],
        "certificate_files": [],
        "key_files": [],
        "certificate_dirs": [],
        "includes": [],
        "sql": {},
        "sql_has_password": False,
    }


def normalize_sql(parsed, data_dir):
    raw = parsed.get("sql") or {}
    driver_raw = str(raw.get("driver") or "SQLite3")
    engine = SQL_DRIVERS.get(driver_raw.lower(), driver_raw.lower())
    result = {
        "driver": driver_raw,
        "engine": engine,
        "database": raw.get("database") or raw.get("dbname") or "",
        "host": raw.get("host") or "localhost",
        "port": raw.get("port"),
        "username": raw.get("username") or raw.get("user") or "",
        "password": raw.get("password") or "",
        "configured": bool(raw),
    }
    if engine == "sqlite":
        database = result["database"] or "prosody.sqlite"
        if not os.path.isabs(database):
            database = os.path.join(data_dir, database)
        result["database"] = os.path.normpath(database)
        result["path"] = result["database"]
    else:
        result["database"] = result["database"] or "prosody"
        result["path"] = None
        try:
            result["port"] = int(result["port"]) if result["port"] else None
        except (TypeError, ValueError):
            result["port"] = None
    return result


class ProsodyInstall(object):
    def __init__(self, host, config_file, parsed, overrides=None):
        self.host = host
        self.config_file = os.path.abspath(config_file)
        self.config_dir = os.path.dirname(self.config_file)
        self.parsed = parsed
        overrides = overrides or {}
        self.warnings = []

        data_dir = overrides.get("data_dir") or parsed["values"].get("data_path")
        if data_dir:
            self.data_dir = _resolve(self.config_dir, data_dir)
        else:
            self.data_dir = derive_data_dir(host, self.config_dir)

        log_files = [item for item in parsed["log_files"] if item]
        log_dir = overrides.get("log_dir")
        if log_dir:
            self.log_dir = _resolve(self.config_dir, log_dir)
        elif log_files:
            self.log_dir = os.path.dirname(log_files[0])
        else:
            self.log_dir = derive_log_dir(host, self.config_dir)

        cert_dirs = list(overrides.get("cert_dirs") or [])
        if not cert_dirs:
            cert_dirs.append(os.path.join(self.config_dir, "certs"))
            cert_dirs.extend(parsed["certificate_dirs"])
            for path in parsed["certificate_files"] + parsed["key_files"]:
                cert_dirs.append(os.path.dirname(path))
        self.cert_dirs = _unique([os.path.normpath(item) for item in cert_dirs])
        self.letsencrypt_roots = []
        le_candidates = list(parsed["certificate_files"]) + list(parsed["key_files"]) \
            + list(parsed["certificate_dirs"]) + list(self.cert_dirs)
        for path in le_candidates:
            if not path:
                continue
            real = os.path.realpath(path)
            if "/etc/letsencrypt/" not in path and "/etc/letsencrypt/" not in real:
                continue
            for leaf in ("/etc/letsencrypt/live", "/etc/letsencrypt/archive",
                         "/etc/letsencrypt/renewal", "/etc/letsencrypt/accounts",
                         "/etc/letsencrypt/renewal-hooks", "/etc/letsencrypt/cli.ini",
                         "/etc/letsencrypt/ssl-dhparams.pem"):
                self.letsencrypt_roots.append(leaf)
            break
        if not self.letsencrypt_roots:
            # Common layout: /etc/prosody/certs contains symlinks into
            # /etc/letsencrypt/live/<domain>/.
            for directory in self.cert_dirs:
                if not os.path.isdir(directory):
                    continue
                try:
                    names = os.listdir(directory)
                except OSError:
                    continue
                for name in names:
                    path = os.path.join(directory, name)
                    if not os.path.islink(path):
                        continue
                    if "/etc/letsencrypt/" in os.path.realpath(path):
                        for leaf in ("/etc/letsencrypt/live", "/etc/letsencrypt/archive",
                                     "/etc/letsencrypt/renewal", "/etc/letsencrypt/accounts",
                                     "/etc/letsencrypt/renewal-hooks", "/etc/letsencrypt/cli.ini",
                                     "/etc/letsencrypt/ssl-dhparams.pem"):
                            self.letsencrypt_roots.append(leaf)
                        break
                if self.letsencrypt_roots:
                    break
        self.letsencrypt_roots = _unique(self.letsencrypt_roots)

        plugin_dirs = list(overrides.get("plugin_dirs") or [])
        plugin_dirs.extend(parsed["plugin_paths"])
        self.plugin_dirs = _unique([os.path.normpath(item) for item in plugin_dirs])

        config_text = parsed.get("text", "")
        self.http_ports = _table_numbers(config_text, "http_ports")
        self.http_interfaces = _table_strings(config_text, "http_interfaces")
        self.https_ports = _table_numbers(config_text, "https_ports")
        self.c2s_ports = _table_numbers(config_text, "c2s_ports")
        self.s2s_ports = _table_numbers(config_text, "s2s_ports")
        self.storage = overrides.get("storage") or parsed["values"].get("storage") or "internal_hashed"
        self.authentication = parsed["values"].get("authentication") or "internal_hashed"
        self.sql = normalize_sql(parsed, self.data_dir)
        if self.storage == "sql" and not self.sql["configured"]:
            self.sql["configured"] = True
        self.user = overrides.get("user") or parsed["values"].get("prosody_user") or host.defaults.get("user") or "prosody"
        self.group = overrides.get("group") or parsed["values"].get("prosody_group") or host.defaults.get("group") or "prosody"
        self.service_name = overrides.get("service") or "prosody"
        self.virtualhosts = parsed["virtualhosts"]
        self.components = parsed["components"]
        self.modules_enabled = parsed["modules"]
        self._version = None

        for directory, label in ((self.data_dir, "data"), (self.log_dir, "log")):
            if directory and not os.path.isdir(directory):
                self.warnings.append("%s directory does not exist: %s" % (label, directory))
            elif directory and os.path.islink(directory):
                self.warnings.append(
                    "%s directory is a symlink (%s); set the real path in the proctor "
                    "config so its contents are backed up" % (label, directory)
                )
        if os.path.islink(self.config_dir):
            self.warnings.append(
                "Prosody config directory is a symlink (%s); set prosody.config to "
                "the real file path" % self.config_dir
            )
        for directory in self.cert_dirs:
            if not os.path.isdir(directory):
                self.warnings.append("certificate directory does not exist: %s" % directory)
            elif os.path.islink(directory):
                self.warnings.append(
                    "certificate directory is a symlink (%s); set cert_dirs to the real "
                    "path so its contents are backed up" % directory
                )
        for path in parsed["certificate_files"]:
            if not os.path.exists(path):
                self.warnings.append("certificate referenced by config is missing: %s" % path)

    @property
    def version(self):
        if self._version is None:
            self._version = detect_version(self.config_file)
        return self._version

    def config_summary(self):
        values = self.parsed.get("values", {})
        safe_values = {}
        for key in ("data_path", "storage", "authentication", "certificates", "plugin_paths",
                    "prosody_user", "prosody_group", "http_ports", "https_ports",
                    "http_external_url", "cross_domain_websocket"):
            if key in values:
                safe_values[key] = values[key]
        sql = dict(self.sql)
        sql.pop("password", None)
        return {
            "config_file": self.config_file,
            "config_dir": self.config_dir,
            "data_dir": self.data_dir,
            "log_dir": self.log_dir,
            "cert_dirs": self.cert_dirs,
            "certificate_files": self.parsed.get("certificate_files") or [],
            "plugin_dirs": self.plugin_dirs,
            "storage": self.storage,
            "authentication": self.authentication,
            "sql": sql,
            "virtualhosts": self.virtualhosts,
            "components": self.components,
            "modules_enabled": self.modules_enabled,
            "http_ports": self.http_ports,
            "http_interfaces": self.http_interfaces,
            "https_ports": self.https_ports,
            "c2s_ports": self.c2s_ports,
            "s2s_ports": self.s2s_ports,
            "user": self.user,
            "group": self.group,
            "values": safe_values,
        }

    def module_dirs(self):
        candidates = [
            "/usr/lib/prosody/modules",
            "/usr/lib64/prosody/modules",
            "/usr/local/lib/prosody/modules",
            "/usr/pkg/lib/prosody/modules",
            "/opt/prosody/lib/prosody/modules",
            "/usr/local/lib/prosody/modules",
        ]
        result = []
        for candidate in candidates:
            if os.path.isdir(candidate) and candidate not in result:
                result.append(candidate)
        for directory in self.plugin_dirs:
            if os.path.isdir(directory) and directory not in result:
                result.append(directory)
        return result

    def available_modules(self):
        available = set()
        for directory in self.module_dirs():
            try:
                for name in os.listdir(directory):
                    if name.endswith(".lua"):
                        stem = name[:-4]
                        available.add(stem[4:] if stem.startswith("mod_") else stem)
            except OSError:
                continue
        return sorted(available)

    def missing_modules(self):
        available = set(self.available_modules())
        if not available:
            return []
        return [name for name in self.modules_enabled if name not in available
                and name not in ("tls", "dialback", "sasl", "carbons", "roster", "ping")]

    def user_accounts(self):
        """List filesystem-backed accounts as {host: sorted users}."""
        result = {}
        if not os.path.isdir(self.data_dir):
            return result
        for entry in sorted(os.listdir(self.data_dir)):
            host_dir = os.path.join(self.data_dir, entry)
            accounts_dir = os.path.join(host_dir, "accounts")
            if not os.path.isdir(accounts_dir):
                continue
            users = []
            for name in sorted(os.listdir(accounts_dir)):
                full = os.path.join(accounts_dir, name)
                if not os.path.isfile(full):
                    continue
                if name.endswith(".list"):
                    continue
                if name.endswith(".dat"):
                    name = name[:-4]
                users.append(urllib.parse.unquote(name))
            if users:
                result[entry] = users
        return result

    def as_dict(self):
        return {
            "config_file": self.config_file,
            "config_dir": self.config_dir,
            "data_dir": self.data_dir,
            "log_dir": self.log_dir,
            "cert_dirs": self.cert_dirs,
            "letsencrypt_roots": self.letsencrypt_roots,
            "plugin_dirs": self.plugin_dirs,
            "storage": self.storage,
            "authentication": self.authentication,
            "sql": {key: value for key, value in self.sql.items() if key != "password"},
            "user": self.user,
            "group": self.group,
            "service": self.service_name,
            "virtualhosts": self.virtualhosts,
            "components": self.components,
            "modules_enabled": self.modules_enabled,
            "version": self.version,
            "warnings": self.warnings,
            "includes": self.parsed.get("includes", []),
        }


def detect_version(config_file=None):
    candidates = []
    if which("prosodyctl"):
        candidates.append(["prosodyctl", "about"])
        candidates.append(["prosodyctl", "--version"])
    if which("prosody"):
        candidates.append(["prosody", "--version"])
    for command in candidates:
        try:
            proc = run(command, check=False, timeout=25)
        except Exception:
            continue
        output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        match = re.search(r"(?i)prosody\s+(\d+\.\d+(?:\.\d+)?[^\s]*)", output)
        if match:
            return match.group(1).rstrip(".")
    # Fallback: a distro package version is not the same as the Prosody
    # release, so do not guess from package metadata.
    return None


def detect_prosody(host, config=None, overrides=None, require=True):
    overrides = overrides or {}
    requested = config or overrides.get("config") or os.environ.get("PROSODY_CONFIG")
    if requested:
        requested = os.path.abspath(os.path.expanduser(requested))
        if not os.path.isfile(requested):
            raise DetectionError(
                "Prosody config not found: %s" % requested,
                hint="pass --prosody-config or set prosody.config in the proctor config",
            )
        path = requested
    else:
        path = find_config(host)
    if not path:
        if require:
            raise DetectionError(
                "could not find prosody.cfg.lua",
                hint="install Prosody, pass --prosody-config, or set prosody.config in %s"
                     % (host.defaults.get("config_file") or "the proctor config"),
            )
        return None
    parsed = parse_prosody_config(path)
    return ProsodyInstall(host, path, parsed, overrides=overrides)


def user_group_names(install):
    return uid_name_for(install.user), gid_name_for(install.group)


def uid_name_for(name):
    from .util import uid_for
    value = uid_for(name)
    return name if value is not None else None


def gid_name_for(name):
    from .util import gid_for
    value = gid_for(name)
    return name if value is not None else None
