# Changelog

## 1.0.2 — 2026-10-25

- Generate Prosody configuration with current core module names
  (`saslauth`, `http_file_share`) and options, falling back to the legacy
  `sasl`/`http_upload` names when only those are installed. This fixes config
  checks on Prosody 0.12+/13 and removes deprecated `cross_domain_*` and
  `legacy_ssl_ports` options.
- Open firewall ports before certbot runs so HTTP-01 validation is not blocked
  by an active UFW/firewalld setup.
- coturn/`mod_turn_external` support: `proctor setup --turn`, `proctor turn
  --setup`, TURN vhost on nginx/Apache with ACME webroot, certificate ACLs for
  the coturn user, firewalld service (including the UDP relay range), UFW rules
  and SELinux port labels.
- `proctor turn` / `doctor` checks: realm, REST API secret, conflicting auth,
  relay range, certificates, live STUN/TURN realm+nonce probe, realm mismatch,
  firewall and SELinux state.
- Backups record TURN settings; restore/migrate recreate coturn.
- Generate an `upload.<domain>` HTTP file-sharing component alongside the MUC
  component, and include `conference.<domain>` and `upload.<domain>` in the
  generated certificate request and web-server vhost.
- Expand an existing Let's Encrypt certificate when the configured web/service
  hostnames are missing from its SANs, instead of silently reusing it.
- Detect multi-file Prosody modules such as `muc` and `pubsub` in `doctor`
  instead of warning that they are missing.
- Generate `storage = "internal"` for filesystem storage; `internal_hashed`
  is the authentication provider, not a Prosody storage driver. This fixes
  account creation and startup on Prosody 13.
- Recognize firewalld's `coturn` service and SELinux `unreserved_port_t`
  fallback labels in `doctor`; enable TURN over TLS when setup supplies a
  certificate even if the previous coturn config had none; skip private keys
  when checking certificate expiry.
- Enable `adhoc` and `admin_adhoc` in generated Prosody configs so server
  administration (XEP-0050/0133) is available to configured admins.
- Enable `invites`, `invites_adhoc` and `invites_register` so admins can create
  account invitation links (XEP-0401).
- Enable recommended Prosody features by default: `mam`, `pep`, `bookmarks`,
  `csi_simple` and `cloud_notify`, plus `mam` on the MUC component.
- Detect Prosody modules stored in `mod_<name>/` directories (such as
  `mod_mam`) so `doctor` does not report them as missing.
- Re-enable coturn TLS whenever setup supplies a certificate, even if the old
  `turnserver.conf` contained `no-tls`.
- Fix the TURN allocation probe's STUN message length, which made coturn drop
  the request and produced a false `no TURN response` warning.
- 88-case unittest suite.
- Drop the macOS CI job; Linux and FreeBSD cover the supported server paths.

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
