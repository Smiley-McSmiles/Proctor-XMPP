#!/bin/sh
# Install Proctor without pip. Requires Python 3.8+.
set -eu

PREFIX="${PREFIX:-/usr/local}"
DESTDIR="${DESTDIR:-}"
LIBDIR="$PREFIX/lib/proctor"
BINDIR="$PREFIX/bin"
MANDIR="$PREFIX/share/man/man1"
SRCDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGE_LIBDIR="$DESTDIR$LIBDIR"
STAGE_BINDIR="$DESTDIR$BINDIR"
STAGE_MANDIR="$DESTDIR$MANDIR"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "error: Python 3.8 or newer is required" >&2
    exit 1
fi

umask 022
install -d "$STAGE_LIBDIR" "$STAGE_BINDIR"
rm -rf "$STAGE_LIBDIR/proctor"
cp -R "$SRCDIR/proctor" "$STAGE_LIBDIR/proctor"
find "$STAGE_LIBDIR/proctor" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

cat > "$STAGE_BINDIR/proctor" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec "$PYTHON" -m proctor "\$@"
EOF
chmod 0755 "$STAGE_BINDIR/proctor"

if [ -f "$SRCDIR/docs/proctor.1" ]; then
    install -d "$STAGE_MANDIR"
    install -m 0644 "$SRCDIR/docs/proctor.1" "$STAGE_MANDIR/proctor.1"
fi

if [ -f "$SRCDIR/completions/proctor.bash" ]; then
    install -d "$DESTDIR$PREFIX/share/bash-completion/completions"
    install -m 0644 "$SRCDIR/completions/proctor.bash" \
        "$DESTDIR$PREFIX/share/bash-completion/completions/proctor"
fi
if [ -f "$SRCDIR/completions/proctor.zsh" ]; then
    install -d "$DESTDIR$PREFIX/share/zsh/site-functions"
    install -m 0644 "$SRCDIR/completions/proctor.zsh" \
        "$DESTDIR$PREFIX/share/zsh/site-functions/_proctor"
fi
if [ -f "$SRCDIR/completions/proctor.fish" ]; then
    install -d "$DESTDIR$PREFIX/share/fish/vendor_completions.d"
    install -m 0644 "$SRCDIR/completions/proctor.fish" \
        "$DESTDIR$PREFIX/share/fish/vendor_completions.d/proctor.fish"
fi

echo "Installed proctor to $STAGE_BINDIR/proctor"
echo "Library: $STAGE_LIBDIR/proctor"
echo
echo "Next steps:"
echo "  sudo proctor init"
echo "  sudo proctor doctor"
echo "  sudo proctor backup --live"
