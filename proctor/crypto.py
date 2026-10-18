"""Optional archive encryption via age or GnuPG (no homemade crypto)."""

from __future__ import print_function

import os

from .errors import ConfigError, ExternalCommandError, ProctorError
from .util import ensure_dir, run, which

ENCRYPTION_SUFFIXES = (".age", ".gpg")


def is_encrypted(path):
    return str(path).endswith(ENCRYPTION_SUFFIXES)


def strip_encryption(path):
    for suffix in ENCRYPTION_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)], suffix[1:]
    return path, None


def encryption_method(settings):
    return str((settings or {}).get("method") or "age").lower()


def validate_encryption(settings, enabled=True):
    settings = settings or {}
    if not enabled:
        return
    method = encryption_method(settings)
    recipients = settings.get("recipients") or []
    if method == "age":
        if not which("age"):
            raise ConfigError(
                "encryption requested but 'age' is not installed",
                hint="install age (apt/dnf/pkg/apk install age) or disable encryption",
            )
        if not recipients:
            raise ConfigError(
                "encryption requested but no age recipients are configured",
                hint="set encryption.recipients in the proctor config",
            )
    elif method in ("gpg", "gnupg"):
        if not which("gpg"):
            raise ConfigError(
                "encryption requested but 'gpg' is not installed",
                hint="install gnupg or disable encryption",
            )
        if not recipients:
            raise ConfigError(
                "encryption requested but no GPG recipients are configured",
                hint="set encryption.recipients in the proctor config",
            )
    else:
        raise ConfigError("unsupported encryption method: %s" % method)


def encrypt_file(source, destination, settings):
    settings = settings or {}
    method = encryption_method(settings)
    recipients = settings.get("recipients") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    if method == "age":
        command = ["age", "--force"]
        for recipient in recipients:
            command.extend(["-r", recipient])
        command.extend(["-o", destination, source])
    elif method in ("gpg", "gnupg"):
        command = ["gpg", "--batch", "--yes", "--trust-model", "always",
                   "--encrypt", "--output", destination]
        for recipient in recipients:
            command.extend(["--recipient", recipient])
        command.append(source)
    else:
        raise ConfigError("unsupported encryption method: %s" % method)
    try:
        run(command)
    except ExternalCommandError as exc:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise ProctorError("encryption failed: %s" % exc)
    os.chmod(destination, 0o600)
    return destination


def decrypt_file(source, destination, settings, workdir=None):
    settings = settings or {}
    method = encryption_method(settings)
    ensure_dir(os.path.dirname(destination) or workdir or ".", mode=0o700)
    if method == "age":
        if not which("age"):
            raise ConfigError(
                "backup is age-encrypted but 'age' is not installed",
                hint="install age to decrypt this backup",
            )
        command = ["age", "--decrypt"]
        identity = settings.get("identity")
        identities = settings.get("identities") or []
        if isinstance(identities, str):
            identities = [identities]
        if identity:
            identities = [identity] + list(identities)
        if not identities:
            raise ConfigError(
                "this backup is age-encrypted; configure encryption.identity",
                hint="set encryption.identity to the age private key path",
            )
        for item in identities:
            command.extend(["--identity", item])
        command.extend(["--force", "--output", destination, source])
    elif method in ("gpg", "gnupg"):
        if not which("gpg"):
            raise ConfigError("backup is GPG-encrypted but 'gpg' is not installed")
        command = ["gpg", "--batch", "--yes", "--decrypt", "--output", destination, source]
    else:
        raise ConfigError("unsupported encryption method: %s" % method)
    try:
        run(command)
    except ExternalCommandError as exc:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise ProctorError("decryption failed: %s" % exc)
    os.chmod(destination, 0o600)
    return destination


def encryption_summary(settings, enabled=True):
    settings = settings or {}
    return {
        "enabled": bool(enabled),
        "method": encryption_method(settings) if enabled else None,
        "recipients": len(settings.get("recipients") or []) if enabled else 0,
        "identity_configured": bool(settings.get("identity")) if enabled else False,
    }
