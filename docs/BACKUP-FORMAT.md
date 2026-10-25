# Proctor backup format (version 1)

This document describes the on-disk format so backups remain recoverable without
Proctor.

## Repository layout

```
<repository>/                     mode 0700
├── .proctor-repo.json            marker/version
├── index.json                    cache of backup metadata (mode 0600)
├── .lock                         flock serialization
├── history.jsonl                 audit log (mode 0600)
├── backups/
│   ├── <id>.tar.gz               archive (or .tar.xz / .tar.zst / .tar)
│   ├── <id>.tar.gz.sha256        archive checksum
│   ├── <id>.manifest.json        readable sidecar manifest + artifact info
│   └── ...
└── tmp/                          per-operation scratch (0700)
```

`index.json` can be rebuilt from the sidecar manifests if lost. An archive is
identified by `<UTC timestamp>-<random>` (for example
`20261018T193148Z-a1d0a2`). Encrypted archives end in `.age` or `.gpg`.

## Archive layout

A tar archive (PAX format) contains:

```
manifest.json                     machine-readable manifest (last member)
files/<absolute path without />   config, data, certs, plugins, logs, system files
db/database.sqlite                SQLite consistent copy
db/database.sql                 pg_dump / mysqldump output
meta/environment.json             OS, Python, service, Prosody paths, database metadata
meta/config-summary.json          parsed/redacted Prosody config summary
meta/prosodyctl-about.txt         prosodyctl about output, if available
meta/RESTORE.txt                  human recovery instructions
```

Paths are stored without the leading `/` (for example
`files/etc/prosody/prosody.cfg.lua`). The manifest maps every archive member
back to its original absolute path, so restore can relocate them.

Proctor uses streaming tar writes: the manifest is added after all files, once
hashes and ownership have been collected.

## Manifest fields

```json
{
  "format": "proctor-backup",
  "format_version": 1,
  "proctor_version": "1.0.2",
  "id": "20261018T193148Z-a1d0a2",
  "created_at": "2026-10-18T19:31:48Z",
  "source": {
    "hostname": "xmpp1",
    "fqdn": "xmpp1.example.com",
    "os": { "system_id": "linux", "family": "fedora", "init": "systemd" },
    "prosody_version": "0.12.4",
    "prosody": {
      "config_file": "/etc/prosody/prosody.cfg.lua",
      "data_dir": "/var/lib/prosody",
      "log_dir": "/var/log/prosody",
      "cert_dirs": ["/etc/prosody/certs"],
      "plugin_dirs": [],
      "storage": "sql",
      "sql": { "engine": "sqlite", "path": "/var/lib/prosody/prosody.sqlite" },
      "virtualhosts": ["example.com"],
      "modules_enabled": ["roster", "sasl", "tls"]
    },
    "service": { "name": "prosody", "manager": "systemd", "was_running": true },
    "webserver": {
      "kind": "nginx",
      "installed": true,
      "proxied": true,
      "proxy_paths": ["/http-bind", "/xmpp-websocket", "/upload"],
      "service": "nginx",
      "certbot": true,
      "cert_names": ["example.com"]
    },
    "letsencrypt": {
      "present": true,
      "cert_names": ["example.com"],
      "domains": ["example.com"],
      "authenticator": "webroot",
      "webroot_path": "/var/www/html"
    }
  },
  "targets": {
    "config": ["/etc/prosody"],
    "data": ["/var/lib/prosody"]
  },
  "components": {
    "config": { "files": 12, "bytes": 8192 },
    "data": { "files": 1843, "bytes": 91234567 }
  },
  "entries": [
    {
      "path": "/etc/prosody/prosody.cfg.lua",
      "arcname": "files/etc/prosody/prosody.cfg.lua",
      "type": "file",
      "size": 2048,
      "mode": 420,
      "uid": 0,
      "gid": 0,
      "uname": "root",
      "gname": "root",
      "mtime": 1760811108,
      "sha256": "3f...",
      "linkname": null,
      "component": "config"
    }
  ],
  "extras": [
    { "arcname": "db/database.sqlite", "size": 12288, "sha256": "...", "component": "database" },
    { "arcname": "meta/environment.json", "size": 3120, "sha256": "...", "component": "meta" }
  ],
  "database": {
    "engine": "sqlite",
    "driver": "SQLite3",
    "path": "/var/lib/prosody/prosody.sqlite",
    "dump": "db/database.sqlite",
    "sha256": "...",
    "size": 12288,
    "user_count": 312,
    "users": ["alice@example.com"]
  },
  "users": {
    "count": 312,
    "filesystem": { "example.com": ["carol"] },
    "preview": ["alice@example.com"]
  },
  "options": {
    "compression": "gz",
    "live": false,
    "encrypted": false,
    "encryption": { "enabled": false }
  },
  "note": "",
  "tags": ["pre-migration"],
  "warnings": [],
  "skipped": [],
  "stats": { "entries": 1855, "bytes": 91242759, "duration_seconds": 12.4, "user_count": 312 }
}
```

- `entries`: every directory, regular file and symlink copied from the source
  tree, with SHA-256 for regular files.
- `extras`: database dump and metadata files.
- `uname`/`gname` enable ownership remapping when numeric IDs differ between
  hosts (for example Linux `prosody` → OpenBSD `_prosody`).
- `source.prosody.sql.password` is **never** written; database credentials are
  redacted from manifests and `config-summary.json`.
- `targets` records the source roots used to build the path map at restore time.

## Sidecar manifest

`<id>.manifest.json`:

```json
{
  "format": "proctor-sidecar",
  "version": 1,
  "manifest": { ...the same manifest object as manifest.json... },
  "artifact": {
    "archive": "<id>.tar.gz",
    "sha256": "...",
    "size": 123456,
    "encrypted": false,
    "compression": "gz"
  }
}
```

The manifest also records the source web server kind, whether it proxied XMPP
HTTP paths, the proxy paths, and Let's Encrypt certificate names. Migration uses
this metadata to recreate a standard nginx/Apache reverse proxy and reuse or
renew certbot certificates. It is metadata only: arbitrary custom web-server
configuration is not archived unless you add it with `--include-path`.
.PP
`list`, `info`, `users`, and checksum checks can run without decrypting the
archive. `verify` compares the embedded `manifest.json` byte-for-byte
semantically with the sidecar and rejects a mismatch.

## Checksums and verification

1. `<archive>.sha256` contains `<sha256>  <filename>` and must match the index.
2. The manifest's `entries` and `extras` describe every member, size and hash.
3. `verify` iterates the tar, rejects unexpected/missing members, validates
   types, sizes and SHA-256.
4. Restore performs the same verification before modifying the host.

Corruption at the outer layer is detected before decompression; corruption
inside a member is detected by its per-file hash.

## Safe extraction

On restore Proctor rejects:

- absolute member names and `..` traversal,
- device nodes and FIFOs,
- hardlinks outside the archive,
- unexpected members not listed in the manifest,
- destinations of `/`, `/proc`, `/sys`, `/dev`,
- writing through an existing symlinked parent (unless `--force`).

Files are written to a temporary file in the destination directory, fsynced,
verified, chmod/chowned, then atomically renamed.

## Encryption

`--encrypt` uses the configured `age` recipients or GPG recipients:

- `age -r RECIPIENT -o <archive>.age <archive>`
- `gpg --encrypt --recipient RECIPIENT --output <archive>.gpg <archive>`

The plaintext archive is removed after encryption. The sidecar stays readable
and contains paths, hostnames, usernames and the note; do not store secrets in
`note` or tags. To restore, configure `encryption.identity` (age) or have the
GPG secret key available. Decryption is performed to a mode-0600 temporary file which is
deleted after use. If a backup is encrypted and credentials are unavailable,
`verify` can still validate the outer checksum but not members.

## Portable bundles

`proctor export` writes an uncompressed tar bundle:

```
BUNDLE.json          {"format":"proctor-bundle","version":1,"artifact":{...}}
manifest.json        the backup manifest
checksum.sha256      archive digest
archive/<archive>    the exact backup archive (still encrypted if applicable)
```

`proctor import` accepts a bundle or a raw archive (with an adjacent
`<id>.manifest.json` or `--manifest`). It validates the checksum and copies the
archive into the repository.

## Retention

Retention is grandfather-father-son over `created_at`:

- newest backup always kept,
- `keep_last N` newest backups,
- newest backup per day for `keep_daily N` days,
- newest per ISO week for `keep_weekly N` weeks,
- newest per month for `keep_monthly N` months,
- any backup tagged `protected` or `keep` is always kept.

`prune` is a dry run unless `--apply` is passed.

## Compatibility

`format_version` is `1`. A future Proctor refuses to read a newer format rather
than guessing. The format is intentionally simple: plain tar, JSON and SHA-256,
so a tar/gzip and a JSON parser are enough for manual recovery.
