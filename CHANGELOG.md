# Changelog

## Unreleased

- coturn/`mod_turn_external` support: `proctor setup --turn`, `proctor turn
  --setup`, TURN vhost on nginx/Apache with ACME webroot, certificate ACLs for
  the coturn user, firewalld service (including the UDP relay range), UFW rules
  and SELinux port labels.
- `proctor turn` / `doctor` checks: realm, REST API secret, conflicting auth,
  relay range, certificates, live STUN/TURN realm+nonce probe, realm mismatch,
  firewall and SELinux state.
- Backups record TURN settings; restore/migrate recreate coturn.
- 78-case unittest suite.

## 1.0.1 — 2026-10-18

- Refresh package indexes before native package installation on apt, dnf/yum,
  zypper, apk and xbps.
- Certbot access repairs for `/etc/letsencrypt` itself, POSIX ACL support, a
  renewal deploy hook that keeps Prosody's certificate access working, and
  permission repair before Prosody starts after a restore.
- Direct BOSH/HTTPS configuration (`https_ssl`) when no reverse proxy is used.
- Opt-in UFW/firewalld port opening during setup and provisioning.
- `--firewall` setup/restore/migrate option; `--only certs` now includes the
  Let's Encrypt tree.
- Certbot renewal conversion removes old nginx/Apache installer directives.
- 61-case unittest suite.

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
- Interactive `proctor setup` for new servers.
- Detection of missing Prosody and native package installation (apt, dnf/yum,
  zypper, pacman, apk, xbps, emerge, pkg, pkg_add, pkgin, brew).
- Interactive nginx/Apache selection, HTTP/BOSH/WebSocket reverse proxy
  generation, Let's Encrypt certbot issuance, Prosody TLS configuration, admin
  account creation and service enablement.
- Backups record the source web server kind, proxy paths and Let's Encrypt
  certificate metadata.
- `restore`/`migrate --install-missing` can provision a fresh host and reproduce
  the recorded nginx/Apache + certbot setup.
- `--webserver`, `--certbot`, `--email`, `--proxy-http` restore/migrate options
  and `init --setup`.
- Stdlib-only Python 3.8+ implementation, 55-case unittest suite and CI matrix.
- Multi-platform `package.sh`: portable tarball, Debian/RPM/Arch/Void packages,
  Alpine/OpenBSD recipes and `dist/SHA256SUMS`.
