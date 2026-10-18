# Packaging Proctor-XMPP

`package.sh` builds release artifacts from any Linux distribution or BSD without
requiring `dpkg-deb`, `makepkg` or `xbps-create` (the Debian and Arch packages
are written by the stdlib helpers in `packaging/`).

```sh
./package.sh --all
```

Outputs go to `dist/`:

| Artifact | Target |
|---|---|
| `proctor-xmpp-<version>.tar.gz` | portable source tarball |
| `proctor-xmpp_<version>_all.deb` | Debian, Ubuntu, Mint, Kali, … |
| `proctor-xmpp-<version>-1.<dist>.noarch.rpm` | Fedora, RHEL, Rocky, Alma, openSUSE |
| `proctor-xmpp-<version>-1-any.pkg.tar.xz` | Arch Linux (`pacman -U`) |
| `proctor-xmpp-<version>_1.noarch.xbps` | Void Linux (`xbps-install -R dist`) |
| `dist/recipes/` | RPM spec, PKGBUILD, Void template, APKBUILD, OpenBSD port |
| `dist/SHA256SUMS` | checksums for release files |

## Targets

```sh
./package.sh                 # default: all targets this machine can build
./package.sh --tar
./package.sh --deb
./package.sh --rpm
./package.sh --arch
./package.sh --void
./package.sh --alpine        # writes the recipe, build with abuild on Alpine
./package.sh --openbsd       # writes the port, build with ports on OpenBSD
./package.sh --recipes
./package.sh --clean
```

Plain names work too: `./package.sh deb arch`.

A missing tool only skips that target and exits with status 3 in the child build;
`--all` continues with the others and reports `skipped:` in the summary.

## Version

```sh
./package.sh --version 1.1.0 --all
```

This invokes `packaging/set_version.py`, which updates:

- `proctor/__init__.py`
- `pyproject.toml`
- README version badge and version line
- man page `.TH` line
- `docs/BACKUP-FORMAT.md` example
- bug-report template placeholder
- a new `CHANGELOG.md` entry (add release notes there)

Pre-release versions such as `1.1.0_rc1` are accepted and mapped to `1.1.0~rc1`
for Debian/RPM ordering.

## Container RPM build

RPM builds need `rpmbuild`. On a non-RPM host:

```sh
./package.sh --container --rpm
```

This uses podman or docker with a Fedora image. Override the image with
`PKG_IMAGE=...`. RPMs, debs and tarballs are copied back to `dist/`.

## Package layout

All packages install the same layout:

```text
/usr/bin/proctor
/usr/lib/proctor/proctor/...
/usr/share/man/man1/proctor.1
/usr/share/bash-completion/completions/proctor
/usr/share/zsh/site-functions/_proctor
/usr/share/fish/vendor_completions.d/proctor.fish
/usr/share/doc/proctor-xmpp/...
/usr/share/licenses/proctor-xmpp/LICENSE
```

Proctor is a CLI, so there is no service, daemon, system user, firewall rule or
package post-install action.

Dependencies are intentionally minimal:

- Debian: `python3 (>= 3.8)`
- RPM: `python3 >= 3.8`
- Arch: `python`
- Void: `python3`
- Alpine: `python3`

`age`, GnuPG, PostgreSQL and MySQL clients are optional (`Suggests`/`optdepends`);
Proctor checks for them in `proctor doctor`.

## Cross-building helpers

- `packaging/mkdeb.py` writes the `ar`/`control.tar.gz`/`data.tar.gz` structure
  directly, so `.deb` files can be built on Fedora, Arch, Void or BSD.
- `packaging/mkarch.py` writes `.PKGINFO` plus the tar.xz payload, so Arch
  packages can be built without `makepkg` or `pacman`.
- `packaging/set_version.py` keeps all version strings in sync.

`install.sh` supports `DESTDIR` and `PREFIX` for staging:

```sh
DESTDIR=/tmp/stage PREFIX=/usr ./install.sh
```

## Recipes

`dist/recipes/` contains:

- `proctor-xmpp.spec` — RPM spec
- `arch/PKGBUILD` — Arch/AUR recipe
- `void/template` — copy to `srcpkgs/proctor-xmpp/template`
- `alpine/APKBUILD` — build with `abuild -r` on Alpine
- `openbsd/` — port Makefile and `pkg/DESCR`

These recipes use the release tarball and `install.sh`; no distro-specific
service integration is required.

## Release checklist

1. Update the version and changelog:
   ```sh
   ./package.sh --version 1.0.1
   ```
2. Run tests: `make test`.
3. Build: `./package.sh --all` (or the available targets).
4. Verify `dist/SHA256SUMS`.
5. Test the tarball in a temporary prefix:
   ```sh
   tar -xzf dist/proctor-xmpp-*.tar.gz -C /tmp
   PREFIX=/tmp/proctor-test/usr /tmp/proctor-xmpp-*/install.sh
   /tmp/proctor-test/usr/bin/proctor version
   ```
6. Attach `dist/` artifacts to the GitHub release.
7. Tag the release (`vX.Y.Z`).

The GitHub workflow runs the test suite and separately builds the tarball,
`.deb`, Arch package and recipes, then verifies the tarball install. RPM and Void
builds remain host-specific because their tools vary.
