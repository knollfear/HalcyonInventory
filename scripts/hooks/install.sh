#!/usr/bin/env sh
# Point .git/hooks at the tracked hooks in this directory.
#
# A shim rather than a copy: .git/hooks is not version-controlled, so a copied
# hook silently goes stale the moment the tracked one changes, and a fresh
# clone has none at all. The shim is three lines that never need updating.
set -eu
ROOT="$(git rev-parse --show-toplevel)"
HOOK="$ROOT/.git/hooks/pre-push"
cat > "$HOOK" <<SHIM
#!/usr/bin/env sh
# Installed by scripts/hooks/install.sh — edit scripts/hooks/pre-push instead.
exec "\$(git rev-parse --show-toplevel)/scripts/hooks/pre-push" "\$@"
SHIM
chmod +x "$HOOK" "$ROOT/scripts/hooks/pre-push"
echo "installed: .git/hooks/pre-push -> scripts/hooks/pre-push"
