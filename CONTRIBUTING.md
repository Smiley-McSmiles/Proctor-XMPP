# Contributing to Proctor-XMPP

Thanks for helping improve Proctor. Bug reports, platform-specific findings,
migration reports and pull requests are welcome.

- Repository: <https://github.com/Smiley-McSmiles/Proctor-XMPP>
- Issues: <https://github.com/Smiley-McSmiles/Proctor-XMPP/issues>
- Security reports: see [SECURITY.md](SECURITY.md)

## Development requirements

- Python 3.8 or newer (the package uses only the standard library).
- `git`, `tar`, and a POSIX shell.
- Optional for local feature work: `gpg`, `zstd`, `sqlite3`, `nginx`, `apache2`/`httpd`.
- Root is only needed to exercise real service control, package installation,
  restore ownership and backup/restore against system paths.

There is no virtualenv requirement and no runtime dependency to install.

## Running the tests

```sh
make test
make compile
```

`make test` runs the full stdlib `unittest` suite and does not require root. The
suite builds synthetic Prosody trees and covers backup, verification, restore,
path rewriting, SQLite, encryption, retention, provisioning plans and web/TLS
config generation.

The suite intentionally does **not** run package-manager installs or contact
Let's Encrypt. If you change provisioning, test command generation and dry-run
paths rather than installing packages in CI.

## Pull requests

1. Fork the repository and create a topic branch from `main`.
2. Keep changes focused and include tests for behavior changes.
3. Run `make test` and `make compile`.
4. Update `README.md`, `CHANGELOG.md` and the relevant file in `docs/` when
   behavior or CLI options change.
5. Describe the platform(s) you tested and any environment-specific caveats.
6. Do not include real backups, private keys, passwords or production
   `prosody.cfg.lua` contents in an issue or PR.

For restore/provisioning changes, include the distribution/BSD, init system,
Prosody version, storage backend and whether nginx or Apache was involved.

## Style

- Python 3.8-compatible syntax, standard library only.
- Use the existing module layout and naming.
- Prefer small, testable functions; keep user-facing errors in `ProctorError`
  subclasses with actionable `hint` text.
- Keep logs out of stdout: progress goes to stderr, machine output goes to
  stdout.
- Preserve the backup/manifest format unless the change includes a format
  version bump and migration behavior.

## Adding a platform

A new Linux distribution or BSD normally needs:

- detection/default paths in `proctor/hostinfo.py`,
- package names in `proctor/provision.py` if provisioning is supported,
- service-manager behavior in `proctor/service.py` if it is not systemd/OpenRC/
  runit/s6/SysV/BSD rc,
- a test where the behavior can be isolated,
- updates to the platform tables in `README.md` and `docs/SETUP.md`.

## Commit messages

Prefer a short imperative subject and, where useful, a body explaining the
reason and the tested platforms. Example:

```text
Add Fedora provisioning package mapping

Use httpd/mod_ssl on the RHEL family and cover the command in the
provisioning dry-run tests.
```

## License

By contributing, you agree that your contributions are licensed under the MIT
License in this repository.
