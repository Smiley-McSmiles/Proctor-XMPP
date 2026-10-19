# Troubleshooting

## Start here

```sh
sudo proctor discover
sudo proctor doctor
sudo proctor -v backup --live
```

`discover` shows exactly what Proctor detected. Most problems are a mismatched
Prosody config path, storage backend, or service name.

## Prosody is not installed

Run the guided installer:

```sh
sudo proctor setup
```

or let a restore/migration provision the target:

```sh
sudo proctor migrate latest --install-missing
```

`setup --dry-run` shows the package commands and generated config without root
or changes. Non-interactive runs do not install packages unless
`--install-missing` is explicit (restore/migrate) or `--yes` is used (setup).

## Package installation failed

Proctor refreshes package indexes for apt, dnf/yum, zypper, apk and xbps at the
first package install. If the refresh failed (offline mirror, stale metadata,
proxy authentication), update manually:

```sh
sudo apt update          # Debian/Ubuntu
sudo dnf makecache       # Fedora/RHEL
sudo zypper refresh      # openSUSE
sudo apk update          # Alpine
sudo xbps-install -S     # Void
sudo pkg update          # FreeBSD (manual; may switch package branch)
```

Then re-run `proctor setup` or `proctor migrate ... --install-missing`. On very
old releases the package names may differ (especially Apache, certbot and
Python-versioned BSD packages); install them manually and re-run.

## certbot failed

HTTP-01 requires:

- every `-d` domain resolves to this host,
- inbound TCP 80 reaches nginx/Apache,
- `/.well-known/acme-challenge/` is not blocked by a CDN, proxy, or firewall.

Check with:

```sh
dig +short example.com
curl -I http://example.com/.well-known/acme-challenge/test
sudo certbot certificates
```

Proctor leaves the HTTP vhost active if issuance fails. Fix DNS/firewall and
re-run `proctor setup`, or run `certbot certonly --webroot -w /var/www/html -d ...`
manually and update the Prosody/web config.

## Web config test failed after provisioning

Proctor writes `proctor-xmpp.conf` and runs `nginx -t` or
`apachectl configtest`. On some layouts the main config does not include
`conf.d` or `sites-enabled`:

- nginx: ensure `nginx.conf` contains `include /etc/nginx/conf.d/*.conf;` or
  `include /etc/nginx/sites-enabled/*;`.
- Apache Debian: `a2ensite proctor-xmpp`; modules via `a2enmod proxy proxy_http
  proxy_wstunnel ssl headers rewrite`.
- Apache RHEL/FreeBSD: ensure `mod_ssl`, `mod_proxy`, `mod_proxy_http` and
  `mod_proxy_wstunnel` are loaded, and `conf.d` is included.

Fix the include and re-run `proctor setup` or restart the web service.

## "no TLS certificate is configured"

The generated Prosody config requires TLS only when certificates exist. Run
`setup` with certbot, or place certificates in `/etc/prosody/certs` and reference
them in `ssl` blocks. Without TLS, clients cannot use STARTTLS and
`c2s_require_encryption` is set to false by the generated config.

## Prosody cannot read Let's Encrypt certificates

Certbot normally makes `/etc/letsencrypt` mode 0700. The Prosody service user
therefore needs permission on the parent directory as well as the `live/` and
`archive/` trees. Check the effective path and ACLs:

```sh
sudo namei -l /etc/letsencrypt/live/example.com/privkey.pem
sudo getfacl -p /etc/letsencrypt /etc/letsencrypt/live /etc/letsencrypt/archive
```

Proctor applies this when it provisions or restores certificates. To repair it
manually (this is the approach used by many Prosody + Certbot guides):

```sh
sudo apt install acl
sudo setfacl -m u:prosody:x /etc/letsencrypt
sudo setfacl -R -m u:prosody:rX /etc/letsencrypt/live /etc/letsencrypt/archive
```

Re-run `proctor setup` or the restore if you change permissions, and verify with
`sudo -u prosody test -r /etc/letsencrypt/live/example.com/privkey.pem`.

## "could not find prosody.cfg.lua"

Proctor searches the OS default plus `/etc/prosody`, `/usr/local/etc/prosody`,
`/usr/pkg/etc/prosody` and `/opt/prosody`. Point it at the real file:

```sh
sudo proctor discover --config /etc/proctor/config.json   # after setting prosody.config
sudo proctor backup --prosody-config /path/to/prosody.cfg.lua
```

or edit:

```json
{ "prosody": { "config": "/usr/local/etc/prosody/prosody.cfg.lua" } }
```

`PROSODY_CONFIG` is also honoured.

## Wrong data/log/cert path

The Prosody config may use a variable or a generated layout Proctor cannot read.
Set explicit overrides:

```json
{
  "prosody": {
    "data_dir": "/srv/prosody/data",
    "log_dir": "/var/log/prosody",
    "cert_dirs": ["/etc/prosody/certs", "/etc/letsencrypt/live/example.com"]
  }
}
```

`--map` is for restore-time relocation; the config overrides are for detection.

## "no supported service manager detected"

Proctor knows systemd, OpenRC, runit, s6, SysV, FreeBSD `service`, OpenBSD
`rcctl` and NetBSD `/etc/rc.d`. For an unusual setup, use an online backup:

```sh
sudo proctor backup --live
```

In a container with no init system, use `--live` for backups and stop the
container before restoring:

```sh
proctor backup --live
```

or define commands:

```json
{
  "prosody": {
    "service_manager": "custom",
    "commands": {
      "stop": ["/usr/local/sbin/xmppctl", "stop", "{name}"],
      "start": ["/usr/local/sbin/xmppctl", "start", "{name}"],
      "status": ["/usr/local/sbin/xmppctl", "status", "{name}"]
    }
  }
}
```

The `status` command must exit 0 when running.

## Backup stops Prosody for too long

The default is a consistent stopped backup. For large data trees:

- use `--live` (SQLite is still consistent; files may change);
- exclude logs (`logs` are off by default);
- exclude uploads if they are backed up separately (they live under
  `data_path/http_upload` or `http_file_share`);
- schedule during low traffic;
- increase the service stop timeout only by fixing the underlying slow shutdown;
  Proctor waits up to 45 seconds.

## "database backend mismatch"

The backup's engine is not the target's engine. Restore onto the same backend,
then use Prosody's `prosody-migrator`. Proctor will not paste SQL rows across
engines.

- SQLite → PostgreSQL/MySQL, or the reverse.
- PostgreSQL ↔ MySQL.

## `pg_dump`, `psql`, `mysqldump` or `mysql` missing

| System | Packages |
|---|---|
| Debian/Ubuntu | `postgresql-client`, `mariadb-client` |
| Fedora/RHEL | `postgresql`, `mariadb` |
| Arch | `postgresql-libs`, `mariadb-clients` |
| Alpine | `postgresql-client`, `mariadb-client` |
| Void | `postgresql-client`, `mariadb-client` |
| FreeBSD | `postgresql1x-client`, `mariadb-client` |
| OpenBSD | `postgresql-client`, `mariadb-client` |
| NetBSD | `postgresql1x-client`, `mariadb-client` |

For PostgreSQL peer authentication, run Proctor as the database owner or use
`.pgpass`. For MySQL, Proctor uses a temporary mode-0600 defaults file when the
Prosody config contains a password.

## "SQLite database not found"

`storage = "sql"` is set but the database path does not exist. Check the `sql`
block. A relative `database = "prosody.sqlite"` is resolved under `data_path`.

If the server is not using SQL at all, remove `storage = "sql"` or use
`--skip-database`.

## Restore says "refusing to write through symlink"

A parent directory (for example `/var/lib/prosody` or `/etc/prosody`) is a
symlink. This is common with some container/image layouts. Inspect it:

```sh
ls -ld /etc/prosody /var/lib/prosody
```

If the symlink is intentional, use `--force`. Otherwise move it and re-run.

## Restore says "permission denied"

Run as root. Restoring ownership and writing under `/etc`, `/var/lib`,
`/var/db`, `/var/prosody` or `/etc/letsencrypt` requires root.

## Config check fails and the service stays stopped

That is deliberate. Proctor will not start a server with an invalid config. Read
the output of `prosodyctl check config`, fix the module/cert/path, then start the
service. Common causes:

- third-party modules not installed on the target;
- cert paths that differ;
- target-specific `Include` files;
- a leftover old path that `--map` did not cover.

Use `--keep-config` if the target already has a known-good config.

## Certificate renewal is broken after migration

Verify that `/etc/letsencrypt/{live,archive,renewal}` was included and restored at
the same absolute path (Proctor maps Let's Encrypt identity). Then:

```sh
sudo certbot renew --dry-run
```

If your ACME client stores state elsewhere, add the path or reissue certs.

## Sidecar manifest is missing

If `<id>.manifest.json` was lost but the archive still exists and is
unencrypted, rebuild the sidecar and index by importing the archive in place:

```sh
sudo proctor import /var/backups/proctor/backups/<id>.tar.gz --repo /var/backups/proctor
sudo proctor verify <id>
```

For an encrypted archive, the sidecar is the only readable metadata; restore it
from a copy or re-export the backup from the source host.

## "backup verification failed"

The archive or sidecar was modified/truncated. Do not restore it.

```sh
sudo proctor verify <id>
sha256sum /var/backups/proctor/backups/<archive>
```

Restore another copy, or re-export/import the bundle. If the failure is only a
sidecar mismatch but the archive checksum is valid, the archive may still be
recoverable manually; inspect with `tar -tzf`.

## Where are the logs?

Proctor writes progress to stderr. Scheduled systemd runs go to the journal:

```sh
journalctl -u proctor-backup.service -n 100
```

Cron output goes to `/var/log/proctor-backup.log`. The repository also has an
audit log:

```sh
sudo proctor history --limit 100
```

## A scheduled backup never ran

```sh
sudo proctor schedule --show
systemctl list-timers proctor-backup.timer
cat /etc/cron.d/proctor
```

- systemd: ensure the timer is enabled and the host booted with systemd.
- cron: ensure the cron daemon is running and reads `/etc/cron.d`. BSDs usually
  do not; put the line in `/etc/crontab` instead.
- Check the command path embedded in the unit/cron line.
- Run the same command manually with `-v`.

## Prune removed more than expected

Retention is bucket-based, not a simple count. `keep_daily 7` keeps the newest
backup from each of the last seven *days that have backups*, not the last seven
backups. Use `--dry-run` (the default) first. Tag important backups:

```sh
sudo proctor backup --tag protected
```

## Restore overwrote my target config

Proctor takes an automatic safety backup before restoring/ migrating by default.
Find it with:

```sh
sudo proctor list
```

Backups tagged `pre-restore` are the automatic ones. To recover only the old
configuration, restore that backup with `--only config` (and `--only certs` if
needed), or extract the config directory from a manual copy. Use `--keep-config`
for future migrations where configuration is managed externally.

## Python version problems

Proctor needs Python 3.8+. `python3 --version` should confirm. On systems with
only a versioned binary (`python3.12`), install the `python3` package so the
`/usr/bin/env python3` shebang works, or invoke `python3.12 -m proctor`.

## Getting more detail

```sh
sudo proctor -vv backup --live --verify
sudo proctor doctor --json
sudo proctor discover --json
```

When reporting a bug include the OS, init system, Prosody version, `discover`
JSON (redact hostnames if needed), and the failing command's stderr.
