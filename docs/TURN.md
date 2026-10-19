# coturn / TURN for XMPP calls

Prosody's `mod_turn_external` only tells clients where the TURN server is; it
never contacts it. coturn provides STUN/TURN, and Proctor can set it up,
place its ACME vhost on nginx or Apache, open the firewall, label SELinux ports
and check the result.

## Setup

```sh
sudo proctor setup --turn yes --domain example.com \
  --webserver nginx --certbot yes --email admin@example.com
```

Or, on a host where Prosody is already configured:

```sh
sudo proctor turn --setup --domain example.com --webserver nginx \
  --turn-host turn.example.com --certbot auto --email admin@example.com
```

`setup` asks interactively whether to enable TURN; with `--yes` it only does so
when `--turn yes` is passed, matching how `--certbot` behaves.

What Proctor does:

1. installs coturn (`net-im/coturn` on Gentoo, `coturn` elsewhere),
2. writes `/etc/coturn/turnserver.conf` with `realm`, `use-auth-secret`,
   `static-auth-secret`, the relay range, the coturn service user and TLS
   certificates when they exist,
3. writes a TURN vhost (`turn-<host>.conf`) on the detected nginx or Apache,
   with `/.well-known/acme-challenge/` pointing at `/var/www/html`,
4. ensures the web server owns the ACME webroot and relabels it for SELinux,
5. reuses the existing `turn.<domain>` certificate when present; request it with
   certbot first if you run `turn --setup` on an existing host,
6. grants the `coturn` user read/traverse access to `/etc/letsencrypt` (ACLs),
7. enables coturn, labels its ports for SELinux and opens the firewall ports,
8. adds `turn_external` and its settings to `prosody.cfg.lua`, validating and
   restarting Prosody.

Secrets are generated with `openssl`-grade randomness and written to both
coturn and Prosody. Re-running setup reuses the existing secret in the Prosody
config so the two never drift apart.

## Ports

| Port | Protocol | Purpose |
|---|---|---|
| 3478 | UDP/TCP | STUN/TURN |
| 5349 | UDP/TCP | TURN over TLS/DTLS |
| 49152–65535 | UDP | relay range (`min-port`/`max-port`) |

The relay range is the one people forget; calls fail without it.

## Firewall

- **firewalld:** Proctor writes `/etc/firewalld/services/coturn.xml` (ports plus
  the UDP relay range) and enables the service in the default zone. If the host
  uses a non-default zone, add it manually:
  `sudo firewall-cmd --permanent --zone=<zone> --add-service=coturn`.
- **UFW:** allows 3478/tcp+udp, 5349/tcp+udp and `49152:65535/udp`.
- `--firewall yes` applies these unattended; `auto` prompts interactively and
  skips under `--yes`.

## SELinux (RHEL/Fedora)

Proctor checks `getenforce` and, when enforcing, labels the TURN ports with
`semanage port -a -t turn_port_t`, falling back to `unreserved_port_t` if the
policy has no `turn_port_t`. It also runs `restorecon` on `/etc/coturn` and the
ACME webroot. `doctor` reports ports that are still unlabelled.

No SELinux boolean is required for the normal setup. Coturn runs as the
packaged `coturn` user from `/etc/coturn`; do not relocate the config.

## Checking an existing installation

```sh
sudo proctor turn            # or: sudo proctor doctor
```

`proctor turn` exits non-zero if anything is a hard failure. Checks include:

- coturn installed and `/etc/coturn/turnserver.conf` present,
- `realm` set,
- `use-auth-secret` + `static-auth-secret` configured, and `no-auth`/`stun-only`
  not enabled,
- `use-auth-secret` and `lt-cred-mech` not both set,
- `min-port`/`max-port` sane,
- certificates exist when `cert`/`pkey` are configured,
- a real STUN binding request and an unauthenticated TURN `ALLOCATE` against
  `127.0.0.1` (this reproduces the `prosodyctl check turn` realm/nonce
  warnings, plus a realm-mismatch check),
- firewalld or UFW contains the TURN ports and the relay range,
- SELinux labels the TURN ports,
- Prosody has `turn_external` enabled with a secret that matches coturn, and
  `turn_external_host` agrees with the coturn realm.

## The realm/nonce warnings

`prosodyctl check turn` sends an unauthenticated TURN `ALLOCATE`. A correctly
configured coturn answers `401` with `REALM` and `NONCE`. If you see:

```text
TURN server did not return an authentication realm. Is authentication enabled?
TURN server did not return a nonce
```

the usual cause is that `realm`, `use-auth-secret` and `static-auth-secret` are
still commented out in `/etc/coturn/turnserver.conf`. Run `sudo proctor turn`
to confirm which of these is missing, or `sudo proctor turn --setup` to write a
correct config.

## Migration

Backups record the `source.turn` block (host, port, realm, coturn ports and the
full `turnserver.conf`). `restore`/`migrate --install-missing` recreates coturn
on the target, regenerates the TURN vhost and certificate access, and keeps the
Prosody secret in sync.

## Known limits

- coturn behind NAT needs `--turn-external-ip PUBLIC_IP` (or `external-ip=` in
  the config); Proctor cannot infer the public address reliably.
- `proctor turn --setup` reuses an existing certificate but does not issue one
  itself. Run certbot for `turn.<domain>` (webroot) first, or use
  `proctor setup --turn yes --certbot yes`.
- Custom renewal schedules are not recreated on a new host; the distro certbot
  timer/cron plus Proctor's deploy hook handle renewals.
- A firewalld non-default zone must be selected manually.
