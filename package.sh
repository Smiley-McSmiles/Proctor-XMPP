#!/usr/bin/env bash
# ==============================================================================
# Proctor-XMPP Multi-Platform Packaging Script
#
# Builds a portable source tarball, Debian/Ubuntu (.deb), Fedora/RHEL/openSUSE
# (.rpm), Arch Linux (.pkg.tar.xz), and Void Linux (.xbps) packages, plus
# Alpine/OpenBSD packaging recipes and dist/SHA256SUMS.
#
# Proctor is a CLI, so there is no service or daemon to package. The tarball's
# install.sh is the portable route for any distribution/BSD.
#
#   ./package.sh                     build everything this machine can build (default)
#   ./package.sh --deb --arch        selected packages (plain names work too)
#   ./package.sh --container --rpm   build in a Fedora container (podman/docker)
#   ./package.sh --version 1.1.0 --all
#   ./package.sh --help
# ==============================================================================
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

new_version=""
targets=()
container=0
while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            shift
            new_version=${1:-}
            [ -n "$new_version" ] || { echo "--version needs a number, e.g. --version 1.1.0" >&2; exit 2; }
            ;;
        --version=*) new_version=${1#*=} ;;
        --container) container=1 ;;
        -h|--help|help) SHOW_HELP=1 ;;
        --*) targets+=("${1#--}") ;;
        *) targets+=("$1") ;;
    esac
    shift
done
if [ -n "$new_version" ]; then
    python3 "$ROOT/packaging/set_version.py" "$new_version" || exit 2
fi
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' proctor/__init__.py)
[ -n "$VERSION" ] || { echo "could not read proctor/__init__.py version" >&2; exit 1; }
PKGVER=$(printf '%s' "$VERSION" | sed -E 's/[_-]?(rc|alpha|beta|pre|dev)/~\1/')
NAME=proctor-xmpp
DIST=$ROOT/dist
BUILD=$ROOT/build
SRCNAME=$NAME-$VERSION
URL=https://github.com/Smiley-McSmiles/Proctor-XMPP
DESC="Back up, restore and migrate a Prosody XMPP server"
MAINTAINER="WOOSAH <31331596+Smiley-McSmiles@users.noreply.github.com>"
DATESTAMP=$(date +"%a %b %d %Y")

SRC_ITEMS=(
    proctor docs completions contrib tests packaging
    pyproject.toml install.sh package.sh Makefile MANIFEST.in
    README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md LICENSE
    .editorconfig .github
)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m[SUCCESS]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
skip() { warn "$*"; exit 3; }

stage() {  # stage <destdir>
    local dest=$1
    rm -rf "$dest"
    mkdir -p "$dest"
    DESTDIR="$dest" PREFIX=/usr "$ROOT/install.sh" >/dev/null
    find "$dest" -name '*.pyc' -delete
    find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    local docdir="$dest/usr/share/doc/$NAME"
    local licdir="$dest/usr/share/licenses/$NAME"
    install -d "$docdir" "$licdir"
    for f in README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md LICENSE; do
        [ -e "$f" ] && install -m 0644 "$f" "$docdir/"
    done
    [ -e LICENSE ] && install -m 0644 LICENSE "$licdir/LICENSE"
    [ -d docs ] && cp -R docs "$docdir/"
}

build_tar() {
    [ -n "${PKG_TAR_DONE:-}" ] && [ -f "$DIST/$SRCNAME.tar.gz" ] && return 0
    log "tarball"
    mkdir -p "$DIST" "$BUILD"
    local t=$BUILD/$SRCNAME
    rm -rf "$t"
    mkdir -p "$t"
    for item in "${SRC_ITEMS[@]}"; do
        [ -e "$item" ] && cp -R "$item" "$t/"
    done
    find "$t" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$t" -name '*.pyc' -delete
    chmod -R u+rwX,go+rX,go-w "$t"
    chmod 755 "$t/install.sh" "$t/package.sh"
    tar -C "$BUILD" -czf "$DIST/$SRCNAME.tar.gz" "$SRCNAME"
    log "  -> dist/$SRCNAME.tar.gz"
}

build_deb() {
    have python3 || skip "python3 not found, cannot build the deb"
    log "deb"
    local root=$BUILD/deb/${NAME}_$VERSION
    stage "$root"
    mkdir -p "$root/DEBIAN"
    local size
    size=$(du -sk "$root" | cut -f1)
    cat > "$root/DEBIAN/control" <<CTL
Package: $NAME
Version: $PKGVER
Section: admin
Priority: optional
Architecture: all
Installed-Size: $size
Depends: python3 (>= 3.8)
Suggests: coturn, age, gnupg, postgresql-client, mariadb-client | mysql-client
Maintainer: $MAINTAINER
Homepage: $URL
Description: $DESC
 Proctor captures Prosody configuration, user data, certificates and SQL
 storage, verifies every archive with per-file checksums, and restores or
 migrates the installation across Linux distributions and BSDs. It can also
 provision missing Prosody, nginx/Apache and certbot components.
CTL
    mkdir -p "$DIST"
    python3 "$ROOT/packaging/mkdeb.py" "$root" "$DIST/${NAME}_${PKGVER}_all.deb"
    log "  -> dist/${NAME}_${PKGVER}_all.deb"
}

recipe_rpm() {
    mkdir -p "$DIST/recipes"
    cat > "$DIST/recipes/$NAME.spec" <<SPEC
Name:           $NAME
Version:        $PKGVER
Release:        1%{?dist}
Summary:        $DESC
License:        MIT
URL:            $URL
Source0:        $SRCNAME.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.8
Suggests:       coturn, age, gnupg, postgresql, mariadb

%description
Proctor backs up, restores and migrates Prosody XMPP servers. It captures
configuration, user data, certificates and SQL storage, verifies every archive,
and can provision Prosody, nginx/Apache and certbot on a fresh host.

%prep
%autosetup -n $SRCNAME

%install
DESTDIR=%{buildroot} PREFIX=/usr ./install.sh

%files
%license LICENSE
%doc README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md docs
/usr/bin/proctor
/usr/lib/proctor
/usr/share/man/man1/proctor.1*
/usr/share/bash-completion/completions/proctor
/usr/share/zsh/site-functions/_proctor
/usr/share/fish/vendor_completions.d/proctor.fish

%changelog
* $DATESTAMP $MAINTAINER - $PKGVER-1
- Packaged Proctor-XMPP $VERSION
SPEC
}

build_rpm() {
    recipe_rpm
    have rpmbuild || skip "rpmbuild not found (dnf/apt install rpm-build); spec written to dist/recipes/$NAME.spec"
    log "rpm"
    build_tar
    local top=$BUILD/rpm
    rm -rf "$top"
    mkdir -p "$top"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}
    cp "$DIST/$SRCNAME.tar.gz" "$top/SOURCES/"
    cp "$DIST/recipes/$NAME.spec" "$top/SPECS/"
    if ! rpmbuild --define "_topdir $top" -bb "$top/SPECS/$NAME.spec" >"$top/rpmbuild.log" 2>&1; then
        tail -n 40 "$top/rpmbuild.log" >&2
        echo "rpmbuild failed (full log: $top/rpmbuild.log)" >&2
        exit 1
    fi
    cp "$top"/RPMS/*/*.rpm "$DIST/"
    log "  -> dist/$(cd "$DIST" && ls -- *"$PKGVER"*.rpm | head -1)"
}

recipe_arch() {
    mkdir -p "$DIST/recipes/arch"
    cat > "$DIST/recipes/arch/PKGBUILD" <<PKG
# Maintainer: $MAINTAINER
pkgname=$NAME
pkgver=$VERSION
pkgrel=1
pkgdesc="$DESC"
arch=('any')
url="$URL"
license=('MIT')
depends=('python')
optdepends=(
    'coturn: STUN/TURN for audio/video calls'
    'age: encrypted backups'
    'gnupg: encrypted backups'
    'postgresql: PostgreSQL storage'
    'mariadb: MySQL/MariaDB storage'
)
source=("\$pkgname-\$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
    cd "\$srcdir/\$pkgname-\$pkgver"
    DESTDIR="\$pkgdir" PREFIX=/usr ./install.sh
    install -Dm644 LICENSE "\$pkgdir/usr/share/licenses/\$pkgname/LICENSE"
    install -Dm644 README.md "\$pkgdir/usr/share/doc/\$pkgname/README.md"
    install -Dm644 CHANGELOG.md "\$pkgdir/usr/share/doc/\$pkgname/CHANGELOG.md"
}
PKG
}

build_arch() {
    recipe_arch
    have python3 || skip "python3 not found, cannot build the Arch package"
    log "arch"
    local root=$BUILD/arch/${NAME}-$VERSION
    stage "$root"
    mkdir -p "$BUILD/arch" "$DIST"
    python3 "$ROOT/packaging/mkarch.py" "$root" "$DIST/$NAME-$VERSION-1-any.pkg.tar.xz" \
        --name "$NAME" --version "$VERSION" --desc "$DESC" --url "$URL" \
        --depend python \
        --optdepend 'coturn: STUN/TURN for audio/video calls' \
        --optdepend 'age: encrypted backups' \
        --optdepend 'gnupg: encrypted backups' \
        --optdepend 'postgresql: PostgreSQL storage' \
        --optdepend 'mariadb: MySQL/MariaDB storage' >/dev/null
    log "  -> dist/$NAME-$VERSION-1-any.pkg.tar.xz   (install: sudo pacman -U ...)"
}

recipe_void() {
    mkdir -p "$DIST/recipes/void"
    cat > "$DIST/recipes/void/template" <<TPL
# Template file for '$NAME' (copy to srcpkgs/$NAME/template in void-packages)
pkgname=$NAME
version=$VERSION
revision=1
depends="python3"
short_desc="$DESC"
maintainer="$MAINTAINER"
license="MIT"
homepage="$URL"
distfiles="\${homepage}/archive/refs/tags/v\${version}.tar.gz"
checksum=SKIP
build_style=gnu-makefile
do_build() { :; }
do_install() {
	DESTDIR=\${DESTDIR} PREFIX=/usr ./install.sh
	vlicense LICENSE
}
TPL
}

build_void() {
    recipe_void
    have xbps-create || skip "xbps-create not found (Void's xbps); template written to dist/recipes/void/template"
    log "void"
    local root=$BUILD/void/${NAME}-$VERSION
    stage "$root"
    mkdir -p "$DIST"
    (cd "$DIST" && xbps-create -A noarch -n "${NAME}-${VERSION}_1" -s "$DESC" \
        -D "python3>=3.8" -H "$URL" -l MIT -m "$MAINTAINER" -t "net" "$root" >/dev/null)
    log "  -> dist/${NAME}-${VERSION}_1.noarch.xbps"
}

recipe_alpine() {
    mkdir -p "$DIST/recipes/alpine"
    cat > "$DIST/recipes/alpine/APKBUILD" <<APK
# Maintainer: $MAINTAINER
pkgname=$NAME
pkgver=$VERSION
pkgrel=0
pkgdesc="$DESC"
url="$URL"
arch="noarch"
license="MIT"
depends="python3"
source="\$pkgname-\$pkgver.tar.gz"
builddir="\$srcdir/\$pkgname-\$pkgver"
options="!check"

package() {
	DESTDIR="\$pkgdir" PREFIX=/usr ./install.sh
	install -Dm644 LICENSE "\$pkgdir"/usr/share/licenses/\$pkgname/LICENSE
	install -Dm644 README.md "\$pkgdir"/usr/share/doc/\$pkgname/README.md
}
APK
}

build_alpine() {
    recipe_alpine
    skip "an Alpine .apk can only be built with abuild on Alpine; APKBUILD written to dist/recipes/alpine/"
}

recipe_openbsd() {
    mkdir -p "$DIST/recipes/openbsd/pkg"
    cat > "$DIST/recipes/openbsd/Makefile" <<MK
COMMENT =	$DESC
DISTNAME =	$NAME-$VERSION
CATEGORIES =	net
HOMEPAGE =	$URL
MAINTAINER =	$MAINTAINER
# MIT
PERMIT_PACKAGE =	Yes
MODULES =	lang/python
MODPY_PYBUILD =	No
RUN_DEPENDS =	lang/python
NO_BUILD =	Yes
NO_TEST =	Yes
SUBST_VARS +=	LOCALBASE

do-install:
	cd \${WRKSRC} && DESTDIR=\${DESTDIR} PREFIX=\${PREFIX} ./install.sh
	\${INSTALL_DATA_DIR} \${DESTDIR}\${PREFIX}/share/doc/$NAME
	\${INSTALL_DATA} \${WRKSRC}/README.md \${DESTDIR}\${PREFIX}/share/doc/$NAME/README.md

.include <bsd.port.mk>
MK
    echo "$DESC" > "$DIST/recipes/openbsd/pkg/DESCR"
}

build_openbsd() {
    recipe_openbsd
    skip "an OpenBSD package is built from a port; the port was written to dist/recipes/openbsd/"
}

build_recipes() {
    recipe_rpm
    recipe_arch
    recipe_void
    recipe_alpine
    recipe_openbsd
    log "recipes -> dist/recipes/"
}

show_help() {
    cat <<HELP
Proctor-XMPP Multi-Platform Packaging Tool (version $VERSION)
Usage: ./package.sh [options]

Options:
  --all         Build every package this machine can build (the default)
  --tar         Portable source tarball (.tar.gz) - unpack and run: sudo ./install.sh
  --deb         Debian / Ubuntu / Mint package (.deb)
  --rpm         Fedora / RHEL / openSUSE package (.rpm) - needs rpmbuild (or --container)
  --arch        Arch Linux package (.pkg.tar.xz) and PKGBUILD - needs only python3
  --void        Void Linux package (.xbps) and template - needs xbps-create
  --alpine      Alpine APKBUILD (build it with abuild on Alpine)
  --openbsd     OpenBSD port recipe
  --recipes     Recipes only (spec, PKGBUILD, template, APKBUILD, port)
  --container   Build the rpm inside a Fedora container (podman/docker)
  --version X.Y.Z   Set the version everywhere first
  --clean       Remove dist/ and build/
  --help, -h    Show this message

Plain target names also work (./package.sh deb arch). Every build writes
dist/SHA256SUMS. A missing build tool skips that target; the summary reports it.
The tarball is the portable route for any distribution/BSD.
Output: $DIST/
HELP
}

[ "${SHOW_HELP:-0}" -eq 1 ] && { show_help; exit 0; }
[ ${#targets[@]} -eq 0 ] && targets=(all)

checksums() {
    local files=() f name
    local seen=" "
    for f in "$DIST"/*"$VERSION"* "$DIST"/*"$PKGVER"*; do
        case "$f" in
            *.tar.gz|*.deb|*.rpm|*.pkg.tar.*|*.xbps)
                [ -f "$f" ] || continue
                name=$(basename "$f")
                case "$seen" in *" $name "*) continue ;; esac
                seen="$seen$name "
                files+=("$name")
                ;;
        esac
    done
    [ ${#files[@]} -gt 0 ] || return 0
    if have sha256sum; then
        (cd "$DIST" && sha256sum "${files[@]}") > "$DIST/SHA256SUMS.tmp"
    elif have shasum; then
        (cd "$DIST" && shasum -a 256 "${files[@]}") > "$DIST/SHA256SUMS.tmp"
    elif have sha256; then
        (cd "$DIST" && sha256 -r "${files[@]}" | awk '{print $1 "  " $2}') > "$DIST/SHA256SUMS.tmp"
    else
        warn "no sha256sum, shasum or sha256: dist/SHA256SUMS not written"
        return 0
    fi
    mv "$DIST/SHA256SUMS.tmp" "$DIST/SHA256SUMS"
    log "checksums -> dist/SHA256SUMS (${#files[@]} files)"
}

summary() {
    checksums
    local f name
    local seen=" "
    echo
    ok "Packaging complete! Artifacts in $DIST/:"
    for f in "$DIST"/*"$VERSION"* "$DIST"/*"$PKGVER"* "$DIST/SHA256SUMS"; do
        [ -f "$f" ] || continue
        name=$(basename "$f")
        case "$seen" in *" $name "*) continue ;; esac
        seen="$seen$name "
        printf '    %s\n' "$name"
    done
    [ -d "$DIST/recipes" ] && printf '    recipes/  (%s)\n' "$(cd "$DIST/recipes" && ls | tr '\n' ' ' | sed 's/ $//')"
    return 0
}

if [ "$container" -eq 1 ]; then
    engine=$(command -v podman || command -v docker || true)
    [ -n "$engine" ] || { echo "--container needs podman or docker" >&2; exit 1; }
    image=${PKG_IMAGE:-registry.fedoraproject.org/fedora:latest}
    [ "${targets[*]}" = all ] && targets=(rpm)
    log "building [${targets[*]}] in $image with $(basename "$engine")"
    if ! "$engine" run --rm -v "$ROOT:/src:Z" -w /src "$image" bash -c \
        "set -e
         dnf -y -q install rpm-build python3 tar gzip xz findutils sed >/dev/null
         cp -r /src /tmp/build-src
         cd /tmp/build-src
         rm -rf dist build
         ./package.sh ${targets[*]}
         mkdir -p /src/dist
         cp dist/*.rpm dist/*.deb dist/*.tar.gz /src/dist/ 2>/dev/null || true"; then
        echo "container build failed" >&2
        exit 1
    fi
    summary
    exit 0
fi

if [ "${targets[0]}" = all ]; then
    built=(); skipped=(); failed=()
    build_tar || { warn "tarball failed"; failed+=(tar); }
    export PKG_TAR_DONE=1 PKG_CHILD=1
    for target in deb rpm arch void alpine openbsd recipes; do
        set +e
        bash "$ROOT/package.sh" "$target"
        rc=$?
        set -e
        case $rc in
            0) built+=("$target") ;;
            3) skipped+=("$target") ;;
            *) failed+=("$target") ;;
        esac
    done
    log "built:   tar ${built[*]:-}"
    [ ${#skipped[@]} -eq 0 ] || log "skipped: ${skipped[*]}  (recipes for these are in dist/recipes/)"
    if [ ${#failed[@]} -gt 0 ]; then
        echo "FAILED:  ${failed[*]}" >&2
        exit 1
    fi
    summary
    exit 0
fi

for target in "${targets[@]}"; do
    case "$target" in
        tar) build_tar ;;
        deb) build_deb ;;
        rpm) build_rpm ;;
        arch) build_arch ;;
        void) build_void ;;
        alpine) build_alpine ;;
        openbsd) build_openbsd ;;
        recipes) build_recipes ;;
        clean) rm -rf "$DIST" "$BUILD"; log "removed dist/ and build/" ;;
        *) echo "unknown target: $target (see ./package.sh --help)" >&2; exit 2 ;;
    esac
done
if [ -z "${PKG_CHILD:-}" ] && [ "${targets[*]}" != clean ]; then summary; fi
