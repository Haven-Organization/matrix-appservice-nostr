#!/usr/bin/env bash
# Sets up the bridge to run natively (no containers): creates a virtualenv,
# installs dependencies, and generates an initial config.yaml with fresh
# random AppService tokens and key-encryption secret.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$PROJECT_ROOT/.venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "error: $PYTHON_BIN not found on PATH" >&2
    exit 1
fi

echo "==> Creating virtualenv at $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "==> Installing dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r requirements.txt

mkdir -p data

if [ -f config.yaml ]; then
    echo "==> config.yaml already exists, leaving it untouched"
else
    echo "==> Generating config.yaml from config.example.yaml"
    cp config.example.yaml config.yaml
    chmod 600 config.yaml

    random_hex() { "$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_hex(32))'; }
    AS_TOKEN="as_$(random_hex)"
    HS_TOKEN="hs_$(random_hex)"
    KEY_SECRET="$(random_hex)"

    # Portable in-place sed (GNU sed vs BSD/macOS sed take -i differently).
    sed_inplace() {
        if sed --version >/dev/null 2>&1; then
            sed -i "$1" "$2"
        else
            sed -i '' "$1" "$2"
        fi
    }
    sed_inplace "s|as_token: \"as_replace_me\"|as_token: \"${AS_TOKEN}\"|" config.yaml
    sed_inplace "s|hs_token: \"hs_replace_me\"|hs_token: \"${HS_TOKEN}\"|" config.yaml
    sed_inplace "s|key_encryption_secret: \"key_secret_replace_me\"|key_encryption_secret: \"${KEY_SECRET}\"|" config.yaml

    echo "==> Generated config.yaml with fresh AppService tokens and key-encryption secret."
    echo "    Back up config.yaml along with the database: the secret can't be recovered."
fi

cat <<EOF2

==> Done. Next steps (INSTALL.md has the details):
    1. Point DNS for your relay hostname (nostr.relay_url) at this server, and
       set up nginx for it and for /.well-known/nostr.json on bridge.domain
       (deploy/nginx.conf.example).
    2. Edit config.yaml: at minimum set
         bridge.domain, homeserver.base_url, homeserver.server_name, nostr.relay_url
    3. Generate the AppService registration file:
         $VENV_DIR/bin/python -m bridge.appservice config.yaml appservice-registration.yaml
       and register it with your homeserver (INSTALL.md), then restart the homeserver.
    4. Run the bridge:
         $VENV_DIR/bin/python main.py
       (or install deploy/matrix-appservice-nostr.service to run it under systemd)
EOF2
