#!/bin/sh
# Install Proctor without pip. Requires Python 3.8+.
set -eu

PREFIX="${PREFIX:-/usr/local}"
LIBDIR="$PREFIX/lib/proctor"
BINDIR="$PREFIX/bin"
MANDIR="$PREFIX/share/man/man1"
SRCDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

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
install -d "$LIBDIR" "$BINDIR"
rm -rf "$LIBDIR/proctor"
cp -R "$SRCDIR/proctor" "$LIBDIR/proctor"
find "$LIBDIR/proctor" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

cat > "$BINDIR/proctor" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec "$PYTHON" -m proctor "\$@"
EOF
chmod 0755 "$BINDIR/proctor"

if [ -f "$SRCDIR/docs/proctor.1" ]; then
    install -d "$MANDIR"
    install -m 0644 "$SRCDIR/docs/proctor.1" "$MANDIR/proctor.1"
fi

if [ -f "$SRCDIR/completions/proctor.bash" ]; then
    install -d "$PREFIX/share/bash-completion/completions"
    install -m 0644 "$SRCDIR/completions/proctor.bash" \
        "$PREFIX/share/bash-completion/completions/proctor"
fi
if [ -f "$SRCDIR/completions/proctor.zsh" ]; then
    install -d "$PREFIX/share/zsh/site-functions"
    install -m 0644 "$SRCDIR/completions/proctor.zsh" \
        "$PREFIX/share/zsh/site-functions/_proctor"
fi
if [ -f "$SRCDIR/completions/proctor.fish" ]; then
    install -d "$PREFIX/share/fish/vendor_completions.d"
    install -m 0644 "$SRCDIR/completions/proctor.fish" \
        "$PREFIX/share/fish/vendor_completions.d/proctor.fish"
fi

echo "Installed proctor to $BINDIR/proctor"
echo "Library: $LIBDIR/proctor"
echo
echo "Next steps:"
echo "  sudo proctor init"
echo "  sudo proctor doctor"
echo "  sudo proctor backup --live"
