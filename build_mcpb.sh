#!/usr/bin/env bash
#
# Package the server as a .mcpb extension for the Claude desktop app.
#
# Run this ON THE MACHINE YOU WILL INSTALL ON, not elsewhere: dependencies are
# installed compiled for the platform the script runs on (pydantic, which the
# MCP SDK depends on, ships a native extension). A bundle built on Linux will
# not start on macOS.
#
#   ./build_mcpb.sh
#
# Produces: zendesk-admin.mcpb (git-ignored: it is machine-specific)
#
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
BUNDLE="zendesk-admin.mcpb"
STAGE="dist"

echo "==> Python: $($PYTHON --version)"

rm -rf "$STAGE" "$BUNDLE"
mkdir -p "$STAGE/server/lib"

# The manifest declares server/main.py as the entry point.
cp server.py          "$STAGE/server/main.py"
cp zendesk_client.py  "$STAGE/server/"
cp manifest.json      "$STAGE/"
cp README.md          "$STAGE/"

# The source manifest says command: "python3". That does not work once
# installed: macOS apps launch with a minimal PATH where "python3" is the Xcode
# stub, not the python.org interpreter. And since the dependencies installed
# below are compiled against THIS interpreter, they have to run under it or
# pydantic's native extension will not load.
# So the bundle's manifest is rewritten with the absolute path of the python
# that ran this build. That makes the bundle specific to this machine, which is
# exactly what we want here - and why the bundle is never committed.
PYTHON_ABS="$("$PYTHON" -c 'import sys; print(sys.executable)')"
echo "==> Pinning interpreter: $PYTHON_ABS"
"$PYTHON" - "$STAGE/manifest.json" "$PYTHON_ABS" <<'EOF'
import json, sys
path, interpreter = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    manifest = json.load(fh)
manifest["server"]["mcp_config"]["command"] = interpreter
with open(path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
EOF

echo "==> Installing dependencies for $(uname -m) (takes a couple of minutes)"
"$PYTHON" -m pip install --quiet --upgrade --target "$STAGE/server/lib" \
    "mcp>=2.0,<3" "httpx>=0.27"

# Junk that adds nothing and only inflates the bundle.
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true
find "$STAGE/server/lib" -maxdepth 1 -name '*.dist-info' -type d \
    -exec rm -rf {}/RECORD \; 2>/dev/null || true

# Sanity check: the server must import using only what ended up in the bundle.
echo "==> Verifying the bundle is self-contained"
( cd "$STAGE/server" && PYTHONPATH="lib" "$PYTHON" -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('bundled_server', 'main.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print('    tools registered: ok')
" )

echo "==> Packaging"
( cd "$STAGE" && zip -qr "../$BUNDLE" . )

SIZE=$(du -h "$BUNDLE" | cut -f1)
echo ""
echo "Done: $BUNDLE ($SIZE)"
echo ""
echo "Two ways to install it, under Settings > Extensions > Advanced settings:"
echo ""
echo "  Install extension...          -> pick the file $BUNDLE"
echo "  Install unpacked extension... -> pick the FOLDER $(pwd)/$STAGE"
echo ""
echo "Note: for the unpacked option you must point at $STAGE/, not the project"
echo "folder. Only $STAGE/ has the manifest with the correct interpreter and the"
echo "server/ directory with the dependencies inside."
echo ""
echo "The API token is stored in the OS keychain, not in the .env."
