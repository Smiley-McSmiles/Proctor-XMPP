## 1.1.0 — 2026-10-18

- Added interactive `proctor setup` for new servers.
- Detect a missing Prosody installation and offer to install it with the native
  package manager (apt, dnf/yum, zypper, pacman, apk, xbps, emerge, pkg, pkg_add,
  pkgin, brew).
- Interactive nginx/Apache selection, HTTP/BOSH/WebSocket reverse proxy
  generation, Let's Encrypt certbot issuance, Prosody TLS configuration,
  admin account creation and service enablement.
- Backups now record the source web server (kind, proxy paths) and Let's Encrypt
  certificate metadata.
- `restore`/`migrate --install-missing` can provision a fresh host and reproduce
  the recorded nginx/Apache + certbot setup.
- Added `--webserver`, `--certbot`, `--email`, `--proxy-http` restore/migrate
  options and `init --setup`.
- Fixed a number of portability and validation issues; test suite is now 55
  cases.

# Changelog

## 1.0.0 — 2026-10-18

Initial release.

- Consistent backups of Prosody config, data, certificates, custom plugins,
  optional logs and system integration files.
- SQLite online backup API, PostgreSQL `pg_dump`, MySQL `mysqldump`.
- Per-member SHA-256, embedded and sidecar manifests, archive checksums and
  `verify`.
- Restore and migration with detection of target layout, path rewriting,
  ownership mapping, safety backup, dry-run and selective components.
- Portable export/import bundles.
- GFS retention, protected tags, dry-run pruning and audit history.
- age/GPG encryption.
- systemd timer and cron scheduling.
- Detection for Debian/Ubuntu, Fedora/RHEL, Arch, openSUSE, Alpine, Void,
  Gentoo, source installs, FreeBSD, OpenBSD, NetBSD and DragonFly BSD.
- Stdlib-only Python 3.8+ implementation, unittest suite, CI matrix.
