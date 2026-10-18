"""Proctor's own JSON configuration (no third-party TOML parser required)."""

from __future__ import print_function

import copy
import json
import os

from . import __version__
from .errors import ConfigError
from .hostinfo import HostInfo
from .util import atomic_write_json, ensure_dir, is_root, read_json

CONFIG_VERSION = 1

DEFAULT_RETENTION = {
    "keep_last": 7,
    "keep_daily": 7,
    "keep_weekly": 4,
    "keep_monthly": 6,
}

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "repository": None,
    "prosody": {
        "config": None,
        "data_dir": None,
        "log_dir": None,
        "cert_dirs": [],
        "plugin_dirs": [],
        "user": None,
        "group": None,
        "service": "prosody",
        "service_manager": "auto",
        "commands": {},
    },
    "backup": {
        "include_logs": False,
        "include_system": True,
        "include_letsencrypt": "auto",
        "compression": "gz",
        "encrypt": False,
        "stop_service": True,
        "verify_after": False,
        "safety_backup": True,
        "min_free_bytes": 0,
    },
    "encryption": {
        "method": "age",
        "recipients": [],
        "identity": None,
    },
    "retention": copy.deepcopy(DEFAULT_RETENTION),
    "history_limit": 5000,
}


def default_config_path(host=None):
    override = os.environ.get("PROCTOR_CONFIG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if is_root():
        return "/etc/proctor/config.json"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "proctor", "config.json")


def default_repository(host=None):
    override = os.environ.get("PROCTOR_REPO")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if is_root():
        return "/var/backups/proctor"
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "proctor", "backups")


def search_config_paths(configured=None):
    candidates = []
    if configured:
        candidates.append(configured)
    env = os.environ.get("PROCTOR_CONFIG")
    if env:
        candidates.append(env)
    candidates.append(default_config_path())
    if is_root():
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        candidates.append(os.path.join(base, "proctor", "config.json"))
    candidates.append("/etc/proctor/config.json")
    result = []
    seen = set()
    for item in candidates:
        item = os.path.abspath(os.path.expanduser(item))
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config(object):
    def __init__(self, data, path=None, explicit=False, host=None):
        self.path = path
        self.explicit = explicit
        self.host = host or HostInfo()
        self.data = deep_merge(DEFAULT_CONFIG, data or {})
        self._normalize()

    def _normalize(self):
        for section in ("prosody", "backup", "encryption", "retention"):
            if not isinstance(self.data.get(section), dict):
                raise ConfigError("config section %r must be a JSON object" % section)
        repository = self.data.get("repository") or default_repository(self.host)
        self.data["repository"] = _expand_path(repository)
        prosody = self.data.setdefault("prosody", {})
        for key in ("config", "data_dir", "log_dir"):
            if prosody.get(key):
                prosody[key] = _expand_path(prosody[key])
        for key in ("cert_dirs", "plugin_dirs"):
            value = prosody.get(key) or []
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                raise ConfigError("prosody.%s must be a list of paths" % key)
            prosody[key] = [_expand_path(item) for item in value]
        if not prosody.get("service"):
            prosody["service"] = "prosody"
        if not prosody.get("service_manager"):
            prosody["service_manager"] = "auto"
        if not isinstance(prosody.get("commands") or {}, dict):
            raise ConfigError("prosody.commands must be a JSON object")
        prosody["commands"] = prosody.get("commands") or {}
        encryption = self.data.setdefault("encryption", {})
        if encryption.get("identity"):
            encryption["identity"] = _expand_path(encryption["identity"])
        identities = encryption.get("identities") or []
        if isinstance(identities, str):
            identities = [identities]
        encryption["identities"] = [_expand_path(item) for item in identities]
        recipients = encryption.get("recipients") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        if not isinstance(recipients, list):
            raise ConfigError("encryption.recipients must be a list")
        encryption["recipients"] = recipients
        retention = self.data.setdefault("retention", {})
        for key, value in DEFAULT_RETENTION.items():
            retention.setdefault(key, value)
            try:
                retention[key] = int(retention[key])
            except (TypeError, ValueError):
                raise ConfigError("retention.%s must be an integer" % key)
        compression = self.data.get("backup", {}).get("compression", "gz")
        if compression not in ("gz", "xz", "zstd", "none"):
            raise ConfigError("backup.compression must be one of gz, xz, zstd, none")
        letsencrypt = self.data.get("backup", {}).get("include_letsencrypt", "auto")
        if letsencrypt not in ("auto", "always", "never"):
            raise ConfigError("backup.include_letsencrypt must be auto, always or never")
        try:
            self.data.get("backup", {})["min_free_bytes"] = int(
                self.data.get("backup", {}).get("min_free_bytes", 0) or 0
            )
        except (TypeError, ValueError):
            raise ConfigError("backup.min_free_bytes must be an integer")

    @property
    def repository(self):
        return self.data["repository"]

    @property
    def prosody(self):
        return self.data.get("prosody", {})

    @property
    def backup(self):
        return self.data.get("backup", {})

    @property
    def encryption(self):
        return self.data.get("encryption", {})

    @property
    def retention(self):
        return self.data.get("retention", DEFAULT_RETENTION)

    def redacted(self):
        result = copy.deepcopy(self.data)
        # Credentials are read from the Prosody config at runtime; scrub any
        # password-like fields that a user may have added by mistake.
        def scrub(value):
            if isinstance(value, dict):
                for key in list(value):
                    if any(token in key.lower() for token in ("password", "secret", "token")):
                        value[key] = "***redacted***"
                    else:
                        scrub(value[key])
            elif isinstance(value, list):
                for item in value:
                    scrub(item)
        scrub(result)
        return result

    def as_dict(self):
        return copy.deepcopy(self.data)


def load_config(path=None, host=None, allow_missing=True):
    host = host or HostInfo()
    chosen = None
    explicit = bool(path)
    if explicit:
        candidate = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(candidate):
            chosen = candidate
        elif not allow_missing:
            raise ConfigError("config file not found: %s" % path)
        else:
            return Config({}, path=None, explicit=True, host=host)
    else:
        for candidate in search_config_paths():
            if os.path.isfile(candidate):
                chosen = candidate
                break
    if chosen is None:
        return Config({}, path=None, explicit=False, host=host)
    data = read_json(chosen, default={})
    if not isinstance(data, dict):
        raise ConfigError("config file must contain a JSON object: %s" % chosen)
    version = data.get("version", CONFIG_VERSION)
    if version > CONFIG_VERSION:
        raise ConfigError(
            "config version %s is newer than this proctor (%s)" % (version, CONFIG_VERSION)
        )
    return Config(data, path=chosen, explicit=explicit, host=host)


def initial_config(host=None, repository=None):
    host = host or HostInfo()
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["repository"] = repository or default_repository(host)
    data["prosody"]["config"] = None
    data["prosody"]["service"] = host.defaults.get("service", "prosody")
    return data


def write_config(path, data, mode=0o600):
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory, mode=0o700)
    if os.path.isdir(path):
        raise ConfigError("config path is a directory: %s" % path)
    atomic_write_json(path, data, mode=mode)
    return path


def _expand_path(value):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(value))))


def install_default_config(path=None, repository=None, force=False, host=None):
    host = host or HostInfo()
    path = path or default_config_path(host)
    if os.path.exists(path) and not force:
        raise ConfigError(
            "config already exists: %s" % path,
            hint="use --force to overwrite, or --path to choose another location",
        )
    data = initial_config(host, repository=repository)
    write_config(path, data)
    return path, data


def config_summary(config):
    return {
        "path": config.path,
        "repository": config.repository,
        "prosody": config.prosody,
        "backup": config.backup,
        "encryption": {
            "method": config.encryption.get("method"),
            "recipients": len(config.encryption.get("recipients") or []),
            "identity": config.encryption.get("identity"),
        },
        "retention": config.retention,
    }
