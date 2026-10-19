# Interactive setup and migration provisioning

Proctor can prepare a host that does not yet have Prosody, and can reproduce the
web-server/certbot shape recorded in a backup.

## `proctor setup`

```sh
sudo proctor setup
```

Proctor first detects:

- the distribution/BSD and package manager,
- whether `prosody`/`prosodyctl` and a config exist,
- whether nginx or Apache is installed and running,
- existing certbot/Let's Encrypt certificates.

It then asks before changing the host:

1. **XMPP domain(s)** — comma-separated, for example
   `example.com, conference.example.com`.
2. **Admin JID** — defaults to `admin@<first-domain>`.
3. **Web server** — nginx (recommended), Apache, or none. An already-installed
   web server is offered first.
4. **Proxy HTTP endpoints** — BOSH (`/http-bind`), WebSocket
   (`/xmpp-websocket`), uploads (`/upload`) and host-meta.
5. **certbot** — obtain a Let's Encrypt certificate using the HTTP-01 webroot
   flow, plus the account email.
6. **Storage** — internal hashed storage (recommended) or SQLite.
7. **Admin password** — hidden input; leave blank to create the account later.
8. **Firewall** — if UFW or firewalld is active, offer to open the XMPP and
   HTTP/HTTPS ports. This is skipped for non-interactive runs unless
   `--firewall yes` is passed.
9. **coturn / TURN** — whether to install coturn for audio/video calls, and the
   `turn.<domain>` hostname. See [TURN.md](TURN.md).

After confirmation it installs native packages, writes:

- the Prosody config (`/etc/prosody/prosody.cfg.lua` or the detected/alternate
  path), including VirtualHosts, MUC components, TLS paths, HTTP/BOSH/WebSocket
  settings and logging;
- an nginx/Apache vhost named `proctor-xmpp.conf`, with reverse proxy locations
  and ACME challenge handling;
- Let's Encrypt certificates under `/etc/letsencrypt/live/<cert-name>/`;
- read/traverse access to Certbot's tree for the Prosody user, using POSIX ACLs
  where available (including `/etc/letsencrypt` itself, not only `live/` and
  `archive/`).

It refreshes package indexes before package installation, enables and starts the
services, registers the admin account, and offers to open firewall ports.

Existing Prosody configuration is copied to
`prosody.cfg.lua.pre-proctor-<timestamp>` even with `--force`.

### Non-interactive

```sh
sudo proctor setup --yes \
  --domain example.com \
  --admin admin@example.com \
  --webserver nginx \
  --certbot yes \
  --email admin@example.com \
  --proxy-http \
  --storage internal_hashed
```

Notes:

- `--certbot auto` is deliberately treated as **no** under `--yes` unless you
  pass `--certbot yes --email ...`; package setup should not contact Let's
  Encrypt without an explicit request.
- `--firewall auto` skips firewall changes under `--yes`; pass
  `--firewall yes` to open UFW/firewalld ports unattended.
- `--webserver none` leaves Prosody as an XMPP-only server (HTTP endpoints bind
  publicly by default).
- `--no-start` installs and writes config but does not start Prosody.
- `--dry-run` prints the package list, Prosody config preview and web config
  preview; it makes no changes and does not require root.

### DNS and firewall prerequisites

For certbot HTTP-01:

- every requested domain must resolve to the target host,
- TCP port 80 must be reachable from the Internet,
- ports 443, 5222 and 5269 should be open for HTTPS and XMPP,
- if a proxy/CDN is in front, configure it to pass
  `/.well-known/acme-challenge/` through to the host.

Proctor can update an existing UFW or firewalld configuration when you confirm
the prompt, or with `--firewall yes`. Without that, open the ports yourself, for
example on Ubuntu:

```sh
sudo ufw allow 80,443,5222,5269/tcp
# only for direct BOSH/HTTPS without a reverse proxy:
sudo ufw allow 5280,5281/tcp
```

Certbot failure is reported but does not roll back the Prosody installation. Fix
DNS/ports and re-run `proctor setup`, or obtain certificates separately.

## What gets installed

| Family | Prosody | nginx | Apache | certbot |
|---|---|---|---|---|
| Debian/Ubuntu | `prosody` | `nginx` | `apache2` | `certbot`, `acl` |
| Fedora/RHEL family | `prosody` | `nginx` | `httpd`, `mod_ssl` | `certbot` |
| openSUSE | `prosody` | `nginx` | `httpd`, `mod_ssl` | `certbot` |
| Arch | `prosody` | `nginx` | `apache` | `certbot` |
| Alpine | `prosody` | `nginx` | `apache2` | `certbot` |
| Void | `prosody` | `nginx` | `apache` | `certbot` |
| Gentoo | `net-im/prosody` | `www-servers/nginx` | `www-servers/apache` | `app-crypt/certbot` |
| FreeBSD/DragonFly | `prosody` | `nginx` | `apache24` | `certbot` |
| NetBSD | `prosody` | `nginx` | `apache24` | `certbot` |
| OpenBSD | `prosody` | `nginx` | Apache if installed | `certbot` |

coturn is installed as `coturn` (`net-im/coturn` on Gentoo) when TURN is
enabled.

Exact package names can differ on older releases. Proctor refreshes package
indexes automatically where the package manager supports it (`apt-get update`,
`dnf makecache`, `apk update`, `zypper refresh`, `xbps-install -S`). A failed
refresh is reported and the install continues with cached indexes. FreeBSD
`pkg update` remains a manual choice because it can pull a new package branch.

## Generated file locations

- Prosody: detected config path; data under the detected `data_path`
  (`/var/lib/prosody`, `/var/db/prosody`, `/var/prosody`, …).
- nginx: `<nginx root>/conf.d/proctor-xmpp.conf`, or
  `sites-available/proctor-xmpp.conf` + `sites-enabled` symlink when that is the
  native layout.
- Apache (Debian): `/etc/apache2/sites-available/proctor-xmpp.conf` with
  `a2ensite`; other families: `<httpd root>/conf.d/proctor-xmpp.conf`.
- certbot: `/etc/letsencrypt/live/<cert-name>/{fullchain.pem,privkey.pem}`.
- Certbot leaves `/etc/letsencrypt` at mode 0700. Proctor grants the Prosody
  user read/traverse access to `/etc/letsencrypt`, `live/` and `archive/` with
  `setfacl` when available. If ACLs are unavailable, it falls back to a
  group-readable private key (`0640`) and directory traversal permissions.
  Restoring a backup re-applies this because POSIX ACLs are not portable through
  the tar archive.
- A certbot deploy hook (`/etc/letsencrypt/renewal-hooks/deploy/proctor-permissions`)
  re-applies access after renewal and reloads/restarts Prosody and the web
  server. Restored renewal configs are switched to the webroot flow when the old
  host used the nginx/Apache certbot plugin.
- When no reverse proxy is used, the generated Prosody config adds an
  `https_ssl` block for direct HTTPS/BOSH on port 5281, matching the manual
  Prosody/certbot setup guides. When nginx/Apache terminates TLS, Prosody binds
  HTTP on localhost only and does not open 5281.

Generated files are marked `Managed by proctor`; other site configs are not
modified.

## Migrating to a host without Prosody

A backup created by Proctor 1.0+ records the source web server kind, whether it
proxied XMPP HTTP paths, the proxy paths, and Let's Encrypt certificate names.

```sh
# target host
sudo proctor init
sudo proctor import /root/site.proctorbundle --verify
sudo proctor migrate latest --install-missing
```

If Prosody is missing, Proctor asks to install it and the recorded web server.
It then:

1. restores the Prosody config/data/database/certificates,
2. generates the nginx/Apache vhost for the backed-up domains and proxy paths,
3. reuses restored Let's Encrypt certificates when present,
4. otherwise offers to issue them with certbot,
5. repairs Prosody's access to Certbot's private keys,
6. reloads the web server, offers to open active firewall ports, and starts
   Prosody.

Without `--install-missing`, in a non-interactive run, Proctor restores files but
does not install packages. It prints what is missing.

Flags for a hands-off migration:

```sh
sudo proctor migrate latest --yes --install-missing \
  --webserver nginx --certbot auto --email admin@example.com
```

To force a web server other than the one recorded in the backup:

```sh
sudo proctor migrate latest --yes --install-missing --webserver apache
```

### Custom web-server configuration

Proctor recreates a standard XMPP reverse proxy, not an arbitrary custom config.
If the old host had bespoke nginx/Apache rules, either:

- add the custom server blocks to the generated `proctor-xmpp.conf`, or
- include the config in the backup explicitly:

  ```sh
  sudo proctor backup --include-path /etc/nginx
  ```

  and merge those files on the target after restore.

## Database provisioning boundary

`setup` can use internal hashed storage or a local SQLite database. It does not
install or initialize PostgreSQL/MySQL servers, create roles, or create
databases. For a migration with external SQL storage, prepare the database
server and credentials first (see [MIGRATION.md](MIGRATION.md)); `--install-missing`
installs the Prosody/web/certbot packages, not a database server.

## Security notes

- Setup and package installation require root. `--dry-run` does not.
- The admin password is read with hidden input and passed to `prosodyctl`;
  prefer creating the admin separately if process-list exposure is a concern.
- The generated proxy terminates HTTPS at nginx/Apache and forwards to
  `127.0.0.1:5280`; Prosody is bound to localhost when proxying.
- Prosody's c2s/s2s encryption is required only when a TLS certificate is
  available. Without one, the setup prints a prominent warning.
- The generated nginx config accepts uploads up to 100 MB at the proxy and
  10 MB in Prosody. Adjust both if needed.
- Certbot modifies only its own `/etc/letsencrypt` tree and the generated vhost.
