# Proctor — implementation plan

Proctor is a dependency-free Python 3 CLI (`proctor`) that manages backups for a
Prosody XMPP server and restores or migrates them onto another machine. It must
run on Debian/Ubuntu, Fedora/RHEL family, Arch, openSUSE, Alpine, Void, Gentoo,
FreeBSD, OpenBSD, NetBSD and DragonFly BSD, with automatic detection of the
Prosody layout, service manager and database backend.

## 1. Design constraints

- **No third-party Python dependencies.** Everything uses the standard library
  so the same code runs on every distro and BSD without `pip` or wheels.
- **Python 3.8+**, no compiled extensions.
- **Never hard-code one distro's layout.** Detect the Prosody config file, parse
  `data_path`, `plugin_paths`, `certificates`, log destinations, SQL settings,
  service user/group and enabled modules from the actual Lua configuration.
- **Distro/BSD aware defaults** for when the config omits a setting:
  `/var/lib/prosody` (Linux), `/var/db/prosody` (FreeBSD/NetBSD/DragonFly),
  `/var/prosody` (OpenBSD), `/usr/local/etc/prosody` vs `/etc/prosody`, and
  matching data/log/cert paths.
- **Service-manager abstraction**: systemd, OpenRC, runit, s6, SysV, FreeBSD
  `service`, OpenBSD `rcctl`, NetBSD `/etc/rc.d`, with custom command overrides.
- **Backup consistency**: stop Prosody by default; `--live` for online backups
  (SQLite uses the online backup API, filesystem data is copied carefully).
- **Portable archives**: Python `tarfile` with gzip/xz/zstd, POSIX paths,
  numeric ownership plus owner/group names, symlinks, modes, mtimes.
- **Integrity by default**: SHA-256 per member, manifest inside and beside the
  archive, archive checksum, `proctor verify`, pre-restore verification.
- **Safety**: repository lock, atomic index writes, safety backup before
  restore, dry-run for destructive operations, explicit confirmation.
- **Secrets**: backups contain credentials/config secrets; files are 0600,
  optional `age`/GPG encryption, no secrets printed in manifests or logs.

## 2. Repository layout

```
proctor-xmpp/
├── proctor/                 CLI package
│   ├── cli.py               argparse command dispatch, exit codes
│   ├── config.py            proctor JSON config + defaults/search paths
│   ├── hostinfo.py          OS/distro/init/service-manager detection
│   ├── prosody.py           Prosody discovery + Lua config parser
│   ├── database.py          SQLite/PostgreSQL/MySQL dump + restore
│   ├── archive.py           tar writer/reader, hashing, encryption, safe extract
│   ├── repository.py        backup index, locking, retention, prune
│   ├── backup.py            backup orchestration
│   ├── restore.py           restore/migration engine + path rewriting
│   ├── schedule.py          systemd timer / cron installer
│   ├── doctor.py            environment and backup-health checks
│   ├── util.py              shell/IO/hash/time helpers
│   ├── errors.py            typed errors + exit codes
│   └── __main__.py
├── tests/                   stdlib unittest suite, fake Prosody trees
├── docs/                    overview, migration runbook, format, man page
├── contrib/                 systemd units, cron, packaging helpers
├── completions/             bash/zsh completion
├── install.sh, Makefile, pyproject.toml, LICENSE
```

## 3. Backup contents (components)

| Component | What is captured | Default |
|---|---|---|
| `config` | entire Prosody config directory, `conf.d`, custom Lua, certs inside it | yes |
| `certs` | configured certificate dirs outside the config dir | yes |
| `letsencrypt` | `live`, `archive`, `renewal` dirs when referenced by config | auto |
| `data` | `data_path`: accounts, rosters, vcards, MUC logs, uploads, offline storage | yes |
| `plugins` | `plugin_paths` / custom modules outside config/data | yes |
| `logs` | Prosody logs | no (`--include-logs`) |
| `system` | systemd drop-ins, `/etc/default|sysconfig|conf.d/prosody` | yes |
| `database` | consistent SQLite copy or `pg_dump`/`mysqldump` | if SQL configured |

Manifest records source host, OS, Prosody version, paths, storage/SQL driver,
enabled modules, virtual hosts, per-file hashes/ownership, DB metadata and user
counts. Passwords are redacted. The manifest is stored both inside the archive
and as a sidecar so `list/info/verify` work without decrypting.

## 4. Commands

```
proctor init                  write config + create repository
proctor discover              show detected OS, init, Prosody paths, storage
proctor doctor                preflight/health checks (root, paths, DB tools, certs, freshness)
proctor backup                consistent backup (stops service unless --live)
proctor list                  list backups (--json)
proctor info ID               show manifest
proctor verify ID             checksum + member verification
proctor users ID              list XMPP accounts in a backup
proctor restore ID            restore onto this host (dry-run, selective)
proctor migrate ID            restore/migrate to a new host with path rewrite + checks
proctor export ID -o FILE     portable single-file bundle
proctor import FILE           import a bundle/archive into a repository
proctor diff ID               compare backup with the current install
proctor prune                 retention (GFS: last/daily/weekly/monthly), --apply
proctor delete ID             remove a backup
proctor schedule              install/remove systemd timer or cron job
proctor history               audit log of backup/restore operations
proctor config                show effective config (secrets redacted)
proctor version
```

Every data-producing command supports `--json`; destructive commands support
`--dry-run` and require `--yes` in non-interactive use.

## 5. Backup flow

1. Load config, resolve repo, take exclusive lock.
2. Detect host (OS, init) and Prosody (config → paths/storage/modules).
3. Preflight: config readable, data dir present, repo writable, free space.
4. Record service state; stop Prosody (unless `--live`), always restore state.
5. Dump SQL database to a temp work dir (SQLite online backup API; `pg_dump`;
   `mysqldump`), capture counts.
6. Stream files into the tar, hashing every member and preserving mode, owner
   names/IDs, symlinks and mtimes; skip sockets/devices with warnings.
7. Add DB dump, environment metadata (`prosodyctl about`, OS, modules) and the
   manifest; close archive; compute archive SHA-256; optionally encrypt.
8. Atomically publish archive, sidecar manifest and checksum; update index;
   optionally verify; append history; restart service; release lock.

## 6. Restore / migrate flow

1. Resolve backup, verify archive + manifest before touching the host.
2. Detect target Prosody (or fall back to OS defaults for a fresh host).
3. Build a path map: source roots → target roots (`config`, `data`, `logs`,
   cert dirs, plugin dirs); `--map OLD=NEW` overrides.
4. Check database engine compatibility; explain `prosody-migrator` if changing.
5. Optional safety backup of the current install.
6. Stop the target service; extract members with path rewriting, symlinks
   remapped, ownership mapped by **user name** (source `prosody` → target
   `prosody`, OpenBSD `_prosody`), modes preserved; config text has old paths
   rewritten to the target layout.
7. Restore database to the target SQLite path or via psql/mysql.
8. Validate config (`prosodyctl check config`) then optionally start.
9. Migration report: modules missing on target, version differences, certs,
   DNS/firewall/service checklist.

## 7. Integrity, retention and security

- Per-member SHA-256 + sizes; archive SHA-256; sidecar/embedded manifest match.
- Safe extraction: reject absolute paths, `..`, unsafe symlinks/hardlinks;
  never extract outside the mapped roots.
- Repo lock (`flock`), `0600` archives, atomic `os.replace`, fsync.
- GFS retention with protected tags; prune is dry-run unless `--apply`.
- Optional encryption through `age` or `gpg` (config-driven, never homemade
  crypto); encrypted backups keep readable sidecar metadata.
- Audit log in `<repo>/history.jsonl`.

## 8. Cross-platform matrix

| Family | Detection | Service control | Default paths |
|---|---|---|---|
| Debian/Ubuntu/Mint/Kali | os-release | systemd/SysV | `/etc/prosody`, `/var/lib/prosody` |
| Fedora/RHEL/Rocky/Alma/Amazon | os-release | systemd | same |
| Arch/Manjaro | os-release | systemd | same |
| openSUSE/SLES | os-release | systemd | same |
| Alpine | os-release | OpenRC | same |
| Gentoo | os-release | OpenRC | same |
| Void | os-release | runit | same |
| FreeBSD/DragonFly | uname | `service` | `/usr/local/etc/prosody`, `/var/db/prosody` |
| OpenBSD | uname | `rcctl` | `/etc/prosody`, `/var/prosody`, `_prosody` |
| NetBSD | uname | `/etc/rc.d` | `/usr/pkg/etc/prosody`, `/var/db/prosody` |
| Generic source install | parsed config | override commands | `/usr/local/etc/prosody`, `/usr/local/var/lib/prosody` |

Custom stop/start/status commands are configurable for anything unusual.

## 9. Test plan

- Unit: Lua comment/string parsing, includes, SQL/log/cert extraction, path
  defaults per OS, service command selection, retention math, safe member names.
- Integration: build a fake Prosody tree, run backup → list → info → verify →
  users → export → import → restore into a second root with path mapping, and
  compare file contents/hashes.
- SQLite round-trip and user enumeration from a backup.
- Corruption tests (tampered member, wrong checksum) fail cleanly.
- Dry-run makes no changes; non-root restore preserves files and skips chown.
- `python -m compileall`, full unittest suite.

## 10. Deliverables

Working CLI, tests, README quickstart, migration runbook, backup format spec,
man page, systemd/cron/OpenRC/runit examples, bash/zsh completions, installer,
pyproject, Makefile and CI matrix.

## 11. Provisioning (v1.1)

- Detect whether Prosody, nginx/Apache and certbot are installed.
- Interactive `proctor setup` asks for domains, admin, web server, proxy paths,
  TLS/certbot, email and storage; installs native packages, writes Prosody and
  web configs, obtains HTTP-01 certificates, enables services and creates an
  admin.
- `restore`/`migrate --install-missing` detects a fresh target, offers to install
  Prosody and the recorded web server, recreates the domain/proxy/TLS layout,
  reuses restored Let's Encrypt material, and otherwise offers certbot.
- Backups record source web-server kind/proxy paths and Let's Encrypt metadata.
- Package managers: apt, dnf/yum, zypper, pacman, apk, xbps, emerge, pkg,
  pkg_add, pkgin, brew.
- Split plan: `provision.py` (packages, detection, prompts), `provision.py`
  config generators (nginx, Apache, Prosody), and restore finalisation hooks.
- Tests cover pure config generation, domain validation, command mapping,
  dry-run setup, backup metadata and restore provisioning plans; package
  installation and live certbot are not run in the unit suite.

## 12. Explicit non-goals / limitations (documented)

- Changing the XMPP domain (JIDs) is not a supported rename operation; migrate
  keeps the domain and moves the server. XMPP identities are domain-bound.
- POSIX ACLs/xattrs are not stored by `tarfile`; modes/ownership/symlinks are.
- Converting storage backends (SQLite ↔ PostgreSQL) is delegated to Prosody's
  own `prosody-migrator`; Proctor detects and explains instead of corrupting.
