# Proctor-XMPP

[![Tests](https://github.com/Smiley-McSmiles/Proctor-XMPP/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Smiley-McSmiles/Proctor-XMPP/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.0.0-informational.svg)](CHANGELOG.md)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20BSD-lightgrey.svg)](#platform-support)

**Proctor** is a dependency-free command-line tool that backs up, restores and
migrates a [Prosody](https://prosody.im/) XMPP server. It is designed for real
servers: it understands Prosody's actual configuration, stops the service for a
consistent snapshot (or takes a safe online backup), dumps SQLite/PostgreSQL/
MySQL storage, preserves ownership and certificates, verifies every archive, and
can move the whole installation to a different Linux distro or BSD.

It can also provision a bare host: detect missing Prosody, nginx/Apache and
certbot, install them after confirmation, configure XMPP domains and TLS, and
reproduce the proxy/certificate shape recorded in a backup.

- **Repository:** <https://github.com/Smiley-McSmiles/Proctor-XMPP>
- **Issues:** <https://github.com/Smiley-McSmiles/Proctor-XMPP/issues>
- **Version:** 1.0.0

[Install](#install) · [Quick start](#quick-start) · [Setup a new server](#set-up-a-new-server-interactively) · [Migrate](docs/MIGRATION.md) · [Commands](#commands) · [Documentation](#documentation)

```
proctor backup      # consistent, verified backup
proctor verify      # checksums + archive integrity
proctor restore     # back onto this host
proctor migrate     # move to a new host with path rewriting
```

- No Python packages, no daemon, no database server of its own.
- Python 3.8+ standard library only.
- Linux (Debian/Ubuntu, Fedora/RHEL family, Arch, openSUSE, Alpine, Void, Gentoo,
  source installs) and FreeBSD, OpenBSD, NetBSD, DragonFly BSD.
- Detects systemd, OpenRC, runit, s6, SysV and BSD rc service managers.
- Backs up user data (internal hashed/plain accounts, rosters, vcards, MUC
  logs, uploads, offline storage), SQL databases, configuration, certs, custom
  plugins and optional logs.
- Detects a missing Prosody installation and interactively installs Prosody,
  nginx or Apache, and certbot; migration can reproduce the web/TLS layout from
  the backup on a bare host.

See [docs/PLAN.md](docs/PLAN.md) for the design and [docs/MIGRATION.md](docs/MIGRATION.md)
for a step-by-step server move.

---

## Install

### Any Unix with Python 3.8+

```sh
git clone https://github.com/Smiley-McSmiles/Proctor-XMPP.git
cd Proctor-XMPP
sudo ./install.sh                 # installs /usr/local/bin/proctor
# or: sudo make install PREFIX=/usr
```

### pip / pipx

```sh
pipx install git+https://github.com/Smiley-McSmiles/Proctor-XMPP.git
# or from a local checkout:
# pipx install .
# or
python3 -m pip install --user git+https://github.com/Smiley-McSmiles/Proctor-XMPP.git
```

The package has no runtime dependencies. Installers never modify Prosody.

### Python availability

| System | Command |
|---|---|
| Debian/Ubuntu/Mint/Kali | `sudo apt install python3` (usually present) |
| Fedora/RHEL/Rocky/Alma | `sudo dnf install python3` |
| Arch/Manjaro | `sudo pacman -S python` |
| openSUSE | `sudo zypper install python3` |
| Alpine | `sudo apk add python3` |
| Void | `sudo xbps-install -S python3` |
| Gentoo | `sudo emerge dev-lang/python` |
| FreeBSD/DragonFly | `sudo pkg install python3` |
| OpenBSD | `sudo pkg_add python3` |
| NetBSD | `sudo pkgin install python312` (or the current python3 package) |

Optional: `age` or `gnupg` for encrypted backups, `postgresql-client` for
PostgreSQL, `mariadb-client`/`mysql-client` for MySQL. Proctor checks these in
`proctor doctor`.

---

## Packaging

`package.sh` builds release artifacts without requiring the target distribution:

```sh
./package.sh --all
```

It produces, in `dist/`:

- `proctor-xmpp-<version>.tar.gz` — portable source tarball with `install.sh`
- `proctor-xmpp_<version>_all.deb` — Debian/Ubuntu/Mint
- `proctor-xmpp-<version>-1.<dist>.noarch.rpm` — Fedora/RHEL/openSUSE
- `proctor-xmpp-<version>-1-any.pkg.tar.xz` — Arch Linux
- `proctor-xmpp-<version>_1.noarch.xbps` — Void Linux, when `xbps-create` is present
- `dist/recipes/` — RPM spec, PKGBUILD, Void template, Alpine APKBUILD and OpenBSD port
- `dist/SHA256SUMS`

Targets can be selected individually (`./package.sh deb arch`), the RPM can be
built in a container (`./package.sh --container --rpm`), and `--version X.Y.Z`
updates the version everywhere before building. Missing build tools skip only
that target. See [docs/PACKAGING.md](docs/PACKAGING.md).

## Quick start

```sh
sudo proctor init           # writes /etc/proctor/config.json and a 0700 repo
sudo proctor doctor         # verifies paths, service manager, DB tools, certs
sudo proctor backup         # stops Prosody briefly, backs up, restarts it
sudo proctor list
sudo proctor verify latest
```

## Set up a new server interactively

`proctor setup` detects whether Prosody and a web server are installed and asks
before installing anything:

```sh
sudo proctor setup
```

The prompts cover:

- the XMPP domain(s),
- the admin JID,
- nginx, Apache, or no web server,
- whether to proxy BOSH / WebSocket / HTTP upload,
- whether to obtain a Let's Encrypt certificate with certbot and the email,
- the Prosody storage backend (internal hashed or SQLite),
- an admin password (hidden input),
- whether to open ports when UFW/firewalld is active.

It then refreshes package indexes, installs the native packages, writes
`prosody.cfg.lua`, generates a reverse-proxy vhost, obtains certificates, grants
the Prosody user access to Certbot's private keys (including `/etc/letsencrypt`
itself), enables/starts the services, and registers the admin. Existing Prosody config is backed up to
`*.pre-proctor-<timestamp>` before being replaced.

Non-interactive example:

```sh
sudo proctor setup --yes \
  --domain example.com \
  --webserver nginx --certbot yes --email admin@example.com \
  --firewall yes --proxy-http --storage internal_hashed
```

`proctor init --setup` does the same after creating the config/repository.
Every install path is preceded by a confirmation unless you pass `--yes`;
`--dry-run` prints the package commands and the generated Prosody/nginx or
Apache configuration without changing the host.

For a zero-downtime backup:

```sh
sudo proctor backup --live --verify
```

`--live` uses SQLite's online backup API and copies the data tree while Prosody
runs. Prosody's file-based account/vcard stores are usually updated atomically,
but a stopped service is still the only fully consistent snapshot. Scheduled
backups should normally use the default (brief stop).

Encryption is strongly recommended because backups contain database credentials
and private keys:

```json
{
  "encryption": {
    "method": "age",
    "recipients": ["age1..."]
  }
}
```

```sh
sudo proctor backup --encrypt
```

---

## Commands

| Command | Purpose |
|---|---|
| `proctor init` | Create the JSON config and 0700 backup repository. `--setup` runs guided installation. |
| `proctor setup` | Install/configure Prosody, nginx or Apache, certbot, TLS, domains, firewall ports and an admin account. |
| `proctor discover` | Show detected OS, init system, Prosody paths, storage, users. |
| `proctor doctor` | Preflight/health checks (paths, DB clients, cert expiry, freshness). |
| `proctor backup` | Create a backup. `--live`, `--verify`, `--include-logs`, `--only`, `--exclude`, `--exclude-path`, `--include-path`, `--encrypt`, `--compression gz\|xz\|zstd\|none`. |
| `proctor list` | List backups. |
| `proctor info ID` | Show the manifest. |
| `proctor verify ID` | Verify archive SHA-256, embedded/sidecar manifest and every member. |
| `proctor users ID` | List XMPP accounts in a backup (SQLite and filesystem stores). |
| `proctor restore ID` | Restore onto this host. `--dry-run`, `--only`, `--exclude`, `--keep-config`, `--map OLD=NEW`, `--no-safety-backup`, `--install-missing`, `--webserver`, `--certbot`, `--email`, `--firewall`. |
| `proctor migrate ID` | Restore/migrate to a new host with path rewriting, migration checks and optional package/web/certbot provisioning. |
| `proctor export ID -o FILE` | Create a single portable bundle (archive + manifest + checksum). |
| `proctor import FILE` | Import a bundle or raw archive into a repository. |
| `proctor diff ID` | Compare a backup with the current installation. |
| `proctor prune` | GFS retention; dry-run unless `--apply`. |
| `proctor delete ID` | Delete one backup. |
| `proctor schedule` | Install/remove/show a systemd timer or cron job. |
| `proctor history` | Audit log of backup/restore/prune operations. |
| `proctor config` | Show the effective configuration (secrets redacted). |

`--json` prints machine-readable output on stdout; progress and warnings go to
stderr. `ID` accepts an exact id, a unique prefix, or `latest`.

Exit codes: `0` success, `2` usage/config, `3` environment, `4` integrity,
`5` external command, `6` permission, `7` aborted.

---

## What is backed up

Proctor parses `prosody.cfg.lua` and every `Include` (for example `conf.d/*`)
instead of assuming one distribution's layout.

| Component | Contents | Default |
|---|---|---|
| `config` | Config directory, `conf.d`, custom Lua, certs stored inside it | yes |
| `data` | `data_path`: accounts, rosters, vcards, MUC logs, uploads, offline storage | yes |
| `database` | Consistent SQLite copy, `pg_dump`, or `mysqldump` | when SQL is configured |
| `certs` | Certificate directories outside the config directory | yes |
| `letsencrypt` | `/etc/letsencrypt/{live,archive,renewal,accounts,renewal-hooks}` when referenced by Prosody or certbot | auto |
| `plugins` | `plugin_paths` / custom modules outside config/data | yes |
| `system` | systemd drop-ins, `/etc/default/prosody`, `/etc/sysconfig/prosody`, `/etc/conf.d/prosody`, `/etc/rc.conf.d/prosody`, cron/logrotate entries | yes |
| `extra` | additional paths supplied with `--include-path` (proxy config, custom data) | only when requested |
| `logs` | Prosody log directory | no (`--include-logs`) |

For SQLite, the database file is **not** copied as part of `data`; the archive
contains a transactionally consistent copy made with the SQLite backup API.
Extra paths added with `--include-path` (for example an nginx site file) are
restored at the same absolute path unless you pass `--map OLD=NEW`.

A manifest records source OS/hostname, Prosody version, config summary, virtual
hosts, enabled modules, every file's SHA-256, mode, numeric and named ownership,
symlink targets, database metadata, user counts and warnings. It also records
the source nginx/Apache kind, whether HTTP/BOSH/WebSocket was proxied, proxy
paths and Let's Encrypt certificate names so a fresh target can be provisioned
with the same shape. Passwords are
redacted from the manifest. The manifest is stored both inside the archive and
as a readable sidecar so `list`, `info` and checksum verification work without
decrypting an encrypted archive.

Archive format details: [docs/BACKUP-FORMAT.md](docs/BACKUP-FORMAT.md).

---

## Restore vs. migrate

`restore` and `migrate` share the same engine:

1. Verify archive and manifest before touching the host.
2. Detect the target Prosody layout (or OS defaults on a fresh host).
3. Map source roots to target roots; `--map OLD=NEW` overrides.
4. Take a safety backup of the target (unless `--no-safety-backup`).
5. Stop the service, extract with ownership mapping, rewrite absolute paths in
   config files, restore the database.
6. Validate with `prosodyctl check config` and optionally start.

`migrate` additionally prints a post-move checklist (DNS/SRV, ports, missing
modules, certificates, testing, decommissioning the old host).

> **Same XMPP domain only.** Proctor moves a server, not identities. Renaming
> `@example.com` to `@new.example` is not supported because JIDs are embedded in
> rosters, messages and clients.

### Move to another server

On the old server:

```sh
sudo proctor doctor
sudo proctor backup --verify --tag pre-migration
sudo proctor export latest -o /root/site.proctorbundle
sha256sum /root/site.proctorbundle
```

On the new server (install Prosody first; stop it):

```sh
sudo proctor init
sudo proctor doctor
sudo proctor import /root/site.proctorbundle --verify
sudo proctor migrate latest --dry-run --install-missing
sudo proctor migrate latest --yes --install-missing
```

If Prosody is missing, Proctor offers to install it, then reproduces the web
server recorded in the backup (nginx or Apache), the BOSH/WebSocket proxy, and
the certbot/Let's Encrypt layout. Without `--install-missing`, a non-interactive
run will not install packages; it only restores files and prints what is
missing.

If the target uses unusual paths:

```sh
sudo proctor migrate latest --yes \
  --map /etc/prosody=/usr/local/etc/prosody \
  --map /var/lib/prosody=/var/db/prosody
```

The full runbook, database conversion notes, rollback plan and cutover checklist
are in [docs/MIGRATION.md](docs/MIGRATION.md).

---

## Databases

| Storage | Backup | Restore |
|---|---|---|
| `internal_hashed` / `internal_plain` | files under `data_path` | files, ownership mapped |
| SQLite3 | SQLite online backup API + `quick_check` | atomic file replacement, old DB kept as `*.pre-proctor-*` |
| PostgreSQL | `pg_dump --clean --if-exists --no-owner --no-privileges` | `psql -v ON_ERROR_STOP=1` |
| MySQL/MariaDB | `mysqldump --single-transaction --routines --triggers` | `mysql` client |

Restoring requires the **same database engine**. To change engines, use
Prosody's own `prosody-migrator`; Proctor refuses to silently write an
incompatible database. PostgreSQL/MySQL targets need an existing database and
user with appropriate privileges; Proctor does not create roles or databases.
PostgreSQL extensions referenced by the dump may require superuser rights on
the target; pre-create them if your policy restricts `CREATE EXTENSION`.

---

## Scheduling

```sh
sudo proctor schedule --install --at 03:30
sudo proctor schedule --show
sudo proctor schedule --remove
```

On systemd hosts this installs `proctor-backup.service` and a persistent
`proctor-backup.timer` with a randomized delay. Elsewhere it writes
`/etc/cron.d/proctor` when that directory exists. BSD cron daemons generally do
not read `/etc/cron.d`; add this to `/etc/crontab`:

```cron
30 3 * * * root /usr/local/bin/proctor backup --quiet
```

Void (runit), Alpine/Gentoo (OpenRC) and BSD systems can use cron for the daily
job; Proctor's service abstraction is used for stopping/starting Prosody, not
for scheduling. See `contrib/`.

Recommended retention:

```sh
sudo proctor prune --keep-last 7 --keep-daily 7 --keep-weekly 4 --keep-monthly 6
sudo proctor prune --apply --yes
```

The newest backup is never removed, and backups tagged `protected` or `keep`
survive pruning. Safety backups made before restore are tagged `protected`.

---

## Configuration

Proctor looks for, in order: `--config`, `$PROCTOR_CONFIG`,
`/etc/proctor/config.json` (root) or `~/.config/proctor/config.json`, then
`/etc/proctor/config.json`.

```json
{
  "version": 1,
  "repository": "/var/backups/proctor",
  "prosody": {
    "config": null,
    "data_dir": null,
    "log_dir": null,
    "cert_dirs": [],
    "plugin_dirs": [],
    "user": null,
    "group": null,
    "service": "prosody",
    "service_manager": "auto",
    "commands": {
      "stop": ["systemctl", "stop", "{name}"],
      "start": ["systemctl", "start", "{name}"],
      "status": ["systemctl", "is-active", "--quiet", "{name}"]
    }
  },
  "backup": {
    "include_logs": false,
    "include_system": true,
    "include_letsencrypt": "auto",
    "compression": "gz",
    "encrypt": false,
    "stop_service": true,
    "verify_after": false,
    "safety_backup": true
  },
  "encryption": {
    "method": "age",
    "recipients": [],
    "identity": null
  },
  "retention": {
    "keep_last": 7,
    "keep_daily": 7,
    "keep_weekly": 4,
    "keep_monthly": 6
  }
}
```

Any unset path is detected from the Prosody config and the OS. `commands` is
only needed for an init system Proctor does not know; it is a full override.

Other environment variables: `PROCTOR_REPO`, `PROCTOR_CONFIG`, `PROSODY_CONFIG`,
`PROCTOR_SERVICE_MANAGER`, `NO_COLOR`.

---

## Platform support

| Family | Service manager | Default layout |
|---|---|---|
| Debian, Ubuntu, Mint, Kali, Devuan | systemd / SysV | `/etc/prosody`, `/var/lib/prosody` |
| Fedora, RHEL, Rocky, Alma, Amazon | systemd | same |
| Arch, Manjaro, Artix | systemd / OpenRC / runit (detected) | same |
| openSUSE / SLES | systemd | same |
| Alpine | OpenRC | same |
| Gentoo | OpenRC | same |
| Void | runit | same |
| FreeBSD, DragonFly | `service` | `/usr/local/etc/prosody`, `/var/db/prosody` |
| OpenBSD | `rcctl` | `/etc/prosody`, `/var/prosody`, `_prosody` |
| NetBSD | `/etc/rc.d` | `/usr/pkg/etc/prosody`, `/var/db/prosody` |
| Source install | detected/override | `/usr/local/etc/prosody`, `/usr/local/var/lib/prosody` |

Proctor always prefers values from the actual config (`data_path`,
`plugin_paths`, log `filename`, certificate paths, SQL block, `prosody_user`)
over these defaults. If detection is wrong, set paths in
`/etc/proctor/config.json` or use `--prosody-config`.

---

## Security

- Archives and manifests are mode `0600`; the repository is `0700`.
- Backups contain SQL credentials, private keys, password hashes and all user
  data. Encrypt them (`--encrypt`) before copying off-host.
- The sidecar manifest stays readable even when the archive is encrypted. It
  contains hostnames, paths, usernames and notes, but no passwords; do not put
  secrets in `--note` or tags.
- Encryption uses `age` or GnuPG; Proctor does not implement its own crypto.
- Archives are verified before restore. Extraction rejects absolute paths,
  `..`, device nodes, unexpected members and writes through symlinked parents.
- Restores refuse to write into `/`, `/proc`, `/sys` or `/dev`.
- Mutating operations are serialized with a repository lock and the index is
  written atomically.
- Destructive commands prompt; `--yes` is required for non-interactive use.
- A safety backup is taken before restore by default.

Threat model: Proctor is a root administration tool. A malicious backup bundle
that an administrator explicitly imports is treated as a package of system
files; the safe-extraction checks prevent *accidental* archive traversal, not a
deliberately crafted manifest that an administrator chooses to restore.

---

## Limitations

- Changing the XMPP domain is not supported (JIDs are identities).
- POSIX ACLs and extended attributes are not preserved by Python's `tarfile`;
  modes, ownership, symlinks and mtimes are.
- Database engine conversion is delegated to `prosody-migrator`.
- PostgreSQL/MySQL credentials must already be usable (peer auth, `.pgpass`,
  service config); Proctor passes a configured password through the environment
  or a mode-0600 defaults file but does not create database users.
- Live filesystem backups can capture a file mid-write; use the default stopped
  backup for guaranteed consistency.
- Prosody's generated config from configuration-management tools (NixOS, some
  containers) may be replaced on the next deployment; path rewriting cannot
  edit the source of truth.
- Data held in external services (LDAP authentication, Redis, S3/object storage
  for uploads, external MUC archives) is not in Proctor's archive. Back those up
  with their own tools and keep the Prosody config that points at them.
- On Fedora/RHEL with SELinux, Proctor runs `restorecon -R` on restored paths
  when it is running as root. Verify contexts if you use custom SELinux policy.
- Symlinked Prosody config/data/plugin directories are rejected for backup
  rather than silently archiving only the link. Point `prosody.config`,
  `data_dir` or `plugin_dirs` at the real path. Let's Encrypt symlinks are
  handled explicitly. Restore refuses to write through symlinked parents unless
  `--force` is given.
- Migration recreates a **standard** nginx/Apache XMPP reverse proxy from the
  backed-up domains, proxy paths and web-server kind. It does not restore an
  arbitrary custom web-server configuration byte-for-byte; use
  `--include-path /etc/nginx` (or `/etc/apache2`, `/etc/httpd`) for that, or
  merge custom locations into the generated `proctor-xmpp.conf`.
- `setup` and `--install-missing` use the host's package manager. They need
  root, working package metadata, and reachable repositories. Package names can
  differ on very old releases; install manually and re-run if a package is not
  found.

---

## Testing

```sh
make test        # 55 stdlib unittest cases, no root required
make compile
```

The integration tests create a synthetic Prosody tree, run backup → list →
info → users → verify → export → import → restore with path mapping, and check
SQLite restoration, symlinks, config rewriting, corruption rejection and
retention.

---

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — interactive Prosody/nginx/Apache/certbot setup
- [docs/PACKAGING.md](docs/PACKAGING.md) — native packages, tarball and release artifacts
- [docs/MIGRATION.md](docs/MIGRATION.md) — server migration runbook
- [docs/BACKUP-FORMAT.md](docs/BACKUP-FORMAT.md) — archive/manifest specification
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common failures
- [docs/PLAN.md](docs/PLAN.md) — architecture and implementation plan
- [docs/proctor.1](docs/proctor.1) — man page
- [CHANGELOG.md](CHANGELOG.md) — release history

## Contributing

Bug reports, migration reports and pull requests are welcome at
<https://github.com/Smiley-McSmiles/Proctor-XMPP>. Please include your OS/BSD,
init system, Prosody version and `proctor discover --json` output (redacted)
when reporting a detection or restore problem.

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR. The test suite is
stdlib-only and runs without root:

```sh
make test
```

Security issues should be reported privately as described in
[SECURITY.md](SECURITY.md), not in a public issue.

## License

MIT — see [LICENSE](LICENSE).
