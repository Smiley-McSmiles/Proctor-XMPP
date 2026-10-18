# Security policy

Proctor handles backups that contain XMPP user data, password hashes, private
TLS keys and database credentials. Treat the backup repository, exported bundles
and decryption keys as sensitive.

## Supported versions

Security fixes are provided for the latest 1.1.x release and the `main` branch.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's security advisory
feature:

<https://github.com/Smiley-McSmiles/Proctor-XMPP/security/advisories/new>

Do **not** open a public issue for a vulnerability that could expose backups,
credentials, private keys or restored systems.

Include, when possible:

- affected Proctor version (`proctor version`),
- operating system/BSD, init system and Python version,
- the command and options used,
- whether the issue affects backup, restore, migration, encryption,
  provisioning or archive parsing,
- a minimal reproduction using synthetic data (never real credentials or user
  data),
- impact and any suggested fix.

## Scope

Relevant examples include:

- archive extraction escaping the intended destination,
- accepting tampered manifests or checksums,
- exposing secrets in logs, manifests or command lines,
- incorrect ownership/permission handling during restore,
- package-installation or web-configuration injection through untrusted input,
- unsafe handling of TLS keys or Let's Encrypt renewal material.

General hardening questions, documentation errors and non-security bugs can use
the public issue tracker.
