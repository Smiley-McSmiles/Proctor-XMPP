# Migrating a Prosody server with Proctor

This runbook moves a Prosody XMPP deployment (same XMPP domain) to another
machine, including users, rosters, vcards, uploads, MUC data, SQL storage,
configuration and certificates. It works across Linux distributions and BSDs.

> **Renaming the XMPP domain is not supported.** JIDs (`user@example.com`) are
> identities embedded in rosters, messages and client configuration. Proctor
> migrates a server; it does not rename identities. If you need a new domain,
> run it as a new VirtualHost and migrate users deliberately.

## 0. Understand the cutover

A safe move uses a maintenance window:

1. Freeze writes on the old server (stop Prosody or accept a brief delta).
2. Take the final backup.
3. Move and import the bundle.
4. Restore/migrate on the new server.
5. Point DNS/SRV at the new server and start it.
6. Test, then decommission the old server.

You can do a dry run earlier without touching DNS or stopping the old service.

## 1. Preflight on the old server

```sh
sudo proctor doctor
sudo proctor discover
```

Confirm:

- Prosody config path, `data_path`, storage backend and service name.
- The detected SQL engine and that `pg_dump`/`mysqldump` is installed if needed.
- Free space in the repository (a copy of the data plus database).
- Version of Prosody (`prosodyctl about`). Note it for the target.

Fix warnings before proceeding. `doctor` checks certificates, modules, service
state and backup freshness.

## 2. Back up the old server

The default stops Prosody briefly for a consistent snapshot. In a maintenance
window this is preferred:

```sh
sudo proctor backup --verify --tag pre-migration --note "before move to new host"
sudo proctor verify latest
sudo proctor users latest --all | head
```

If you cannot stop the service:

```sh
sudo proctor backup --live --verify
```

For SQLite, `--live` still takes a transactionally consistent database copy. The
filesystem data is copied while the server runs, so a short stop is safer.

The manifest records the source nginx/Apache kind, whether it proxied
BOSH/WebSocket/upload paths, and Let's Encrypt certificate names. This is what
lets a fresh target be provisioned with the same web/TLS shape.

If Proctor's detected paths are wrong, pass `--prosody-config` or set
`prosody.config` in `/etc/proctor/config.json` first.

## 3. Export a portable bundle

```sh
sudo proctor export latest -o /root/site.proctorbundle
sha256sum /root/site.proctorbundle
ls -l /root/site.proctorbundle
```

The bundle contains the archive, manifest and checksum. It is safe to copy over
SSH/rsync. Encrypted backups stay encrypted; keep the age/GPG private key
available on the target (or decrypt before export, which weakens transport
security).

```sh
scp /root/site.proctorbundle root@new.example.com:/root/
```

## 4. Prepare the new server

Install the same or newer Prosody release and the same database engine.

Debian/Ubuntu:

```sh
sudo apt install prosody
```

Fedora/RHEL:

```sh
sudo dnf install prosody
```

Arch:

```sh
sudo pacman -S prosody
```

Alpine:

```sh
sudo apk add prosody
```

Void:

```sh
sudo xbps-install -S prosody
```

FreeBSD:

```sh
sudo pkg install prosody
```

OpenBSD:

```sh
sudo pkg_add prosody
```

NetBSD:

```sh
sudo pkgin install prosody
```

Install Proctor on the target, then:

```sh
sudo proctor init
sudo proctor doctor
sudo systemctl stop prosody      # or rc-service/rcctl/service/sv
```

Or let Proctor provision a host that has no Prosody at all:

```sh
sudo proctor migrate latest --install-missing
```

The interactive flow asks before installing Prosody, nginx or Apache, and
certbot. It then reproduces the domains, reverse proxy paths and TLS layout
recorded in the backup. See [SETUP.md](SETUP.md).

The target Prosody must be stopped for the migration. Proctor stops and later
optionally starts it through the detected service manager.

### Database preparation

- **SQLite**: nothing to create; Proctor writes the database to the mapped
  `data_path`.
- **PostgreSQL**: create the role/database and make sure the target config's
  `sql` block points at it. Proctor restores with `psql` but does not create the
  database. Example:

  ```sh
  sudo -u postgres createuser -P prosody
  sudo -u postgres createdb -O prosody prosody
  ```

- **MySQL/MariaDB**:

  ```sh
  mysql -u root -p -e "CREATE DATABASE prosody CHARACTER SET utf8mb4;"
  mysql -u root -p -e "CREATE USER 'prosody'@'localhost' IDENTIFIED BY '...';"
  mysql -u root -p -e "GRANT ALL ON prosody.* TO 'prosody'@'localhost';"
  ```

  Put the same credentials in the target Prosody config so Proctor can connect.

If the target must use a different database engine, do not force it. Install the
same engine and restore. Then use Prosody's migrator to convert:

```sh
# Example only; verify against your Prosody version's documentation.
sudo prosodyctl mod_storage_sql --help
```

## 5. Import and dry run

```sh
sudo proctor import /root/site.proctorbundle --verify
sudo proctor list
sudo proctor info latest
sudo proctor restore latest --dry-run --yes
```

The dry run shows the path map, selected components and planned destinations. It
does not stop the service or write files.

Check:

- `/etc/prosody` vs `/usr/local/etc/prosody`
- `/var/lib/prosody` vs `/var/db/prosody` or `/var/prosody`
- Log path
- Cert directory
- Database path/driver

If a mapping is wrong:

```sh
sudo proctor restore latest --dry-run --yes \
  --map /etc/prosody=/usr/local/etc/prosody \
  --map /var/lib/prosody=/var/db/prosody \
  --map /var/log/prosody=/var/log/prosody
```

`--map` may be repeated. The longest matching source path wins.

## 6. Migrate

```sh
sudo proctor migrate latest --yes                  # target already prepared
# or, for a fresh host:
sudo proctor migrate latest --yes --install-missing \
  --webserver nginx --certbot auto --email admin@example.com
```

What happens:

1. Archive checksum, embedded manifest and every member are verified.
2. The target layout is detected (or OS defaults are used).
3. The current target installation is backed up automatically, tagged
   `protected` (unless `--no-safety-backup`).
4. Prosody is stopped.
5. Files are extracted with modes and ownership. Source `prosody`/`_prosody`
   ownership maps to the target service user by name.
6. Absolute paths in restored Lua/config files are rewritten to the target
   layout.
7. SQLite is written atomically (old DB kept as `*.pre-proctor-*`); PostgreSQL/
   MySQL are restored through their clients.
8. `prosodyctl check config` is run when available.
9. The service is started only if it was running before, or with `--start`.

Useful variants:

```sh
# Keep the target's existing config, but restore data/database/certs
sudo proctor migrate latest --yes --keep-config

# Only restore user data and database
sudo proctor migrate latest --yes --only data,database

# Leave the service stopped after migration
sudo proctor migrate latest --yes --no-start
```

Default behavior overwrites the target config with the migrated source config
(after the safety backup). This is usually what you want; use `--keep-config`
when the target's config is managed by Ansible/Puppet/NixOS.

## 7. Start and verify

Proctor prints a checklist. Work through it:

```sh
sudo prosodyctl check config
sudo systemctl start prosody        # if not started by Proctor
sudo journalctl -u prosody -n 100   # or the BSD/Alpine/Void equivalent
sudo proctor doctor
```

Verify with a real client:

- Login as a normal user (password hashes moved with the data/database).
- Check the roster.
- Send a message to another local user.
- Join a MUC and verify history/archives.
- Test HTTP upload / BOSH / WebSocket (ports and proxy settings).
- Test server-to-server federation.
- Check certificate chain for each VirtualHost.

Validate account counts:

```sh
sudo proctor users latest --all
```

## 8. DNS, SRV and firewall cutover

For `example.com` served by `xmpp.example.com`:

```dns
_xmpp-client._tcp.example.com. 300 IN SRV 0 5 5222 xmpp.example.com.
_xmpp-server._tcp.example.com. 300 IN SRV 0 5 5269 xmpp.example.com.
```

- Lower TTLs before the window.
- Open 5222, 5269 and 5280/5281 as needed.
- If using a proxy, update backend addresses.
- Keep the old server running read-only or stopped until clients reconnect.
- Watch logs for s2s errors after cutover.

## 9. Database engine conversions

Proctor intentionally refuses to write a PostgreSQL dump into a SQLite target or
vice versa. Convert before or after the move using Prosody's migrator, not by
editing dumps.

Typical approach:

1. Restore onto a target configured with the **same** engine.
2. Confirm the service works.
3. Reconfigure the target for the new engine.
4. Use Prosody's storage migration tooling to copy between backends.
5. Run `proctor backup` again and verify user counts.

See the Prosody documentation for `prosody-migrator` / `mod_storage_sql` for your
version; the command surface changed across releases.

## 10. Certificates and Let's Encrypt

Proctor backs up:

- certificate directories configured in Prosody (inside and outside the config
  directory), and
- referenced `/etc/letsencrypt/{live,archive,renewal,accounts,renewal-hooks}`
  trees plus `cli.ini` and `ssl-dhparams.pem` when present.

The Let's Encrypt tree is kept at the same absolute path on the target so
`live/*` symlinks and renewal configs remain valid.

After cutover:

```sh
sudo certbot renew --dry-run
sudo prosodyctl check certs
```

If the target uses a different ACME client or path, adjust `ssl` paths in the
config and renew.

## 11. Rollback

Before changing DNS:

- The old server still has its data and config.
- The target has an automatic `pre-restore` safety backup in the repository.

If migration fails:

```sh
sudo proctor list
sudo proctor restore "<pre-restore-id>" --yes   # on the old or new host
```

Restoring the safety backup returns the target to its pre-migration state. On
the old server, simply start Prosody again if you did not wipe it.

Do not decommission the old host until clients, MUCs and federation have been
verified for an agreed period.

## 12. External data and SELinux

Proctor archives what is on the Prosody host. If authentication, uploads,
archives or caching use external services, back those up separately:

- LDAP / Active Directory / OAuth backends (account data lives outside Prosody),
- Redis or another external cache,
- S3/object storage used by upload modules,
- external MUC archive or message-archive services,
- a separate database server (Proctor dumps the SQL database, but the database
  server itself has its own backups and roles).

On Fedora/RHEL with SELinux, Proctor relabels the restored config, data, log and
certificate paths with `restorecon -R` when running as root. If you use custom
SELinux policy, verify the contexts with `ls -Z` and adjust policy.

## 13. Common migration issues

| Symptom | Cause / fix |
|---|---|
| `Prosody is not installed/configured on this host` | Install Prosody and/or run with `--prosody-config /path/prosody.cfg.lua`; migration still copies files but will not start the service. |
| Config check fails after migration | Missing module (install `prosody-modules` or the distro package), wrong cert path, or a target-specific include. The service is left stopped. |
| `database backend mismatch` | Restore to the same engine or use Prosody's migrator. |
| `pg_dump is not installed` | Install `postgresql-client`; for MySQL install `mariadb-client`/`mysql-client`. |
| `Prosody is not installed` and no prompt | Run interactively or pass `--install-missing`; non-interactive runs never install packages by default. |
| certbot failed | Domains must resolve to the target and port 80 must be reachable. Fix DNS/firewall and re-run; Proctor leaves the HTTP vhost in place. |
| Web proxy not created | The backup may predate web-server metadata. Pass `--webserver nginx` or `--webserver apache` with `--install-missing`. |
| Ports not listening | Service not started (`--start`), firewall, or Prosody config still bound to old paths/ports. |
| Users missing | Check `storage` (`internal_hashed` vs `sql`) and that the database dump restored; `proctor users latest --all`. |
| Certificate renewal broken | `/etc/letsencrypt` was excluded or renewal configs point at the old host; restore certs and rerun certbot. |
| Permission denied during restore | Run as root; Proctor must set ownership and write under `/etc`, `/var/lib` or `/var/db`. |
| Target config overwritten unexpectedly | Use `--keep-config` or restore the automatic safety backup. |
