# Installing matrix-appservice-nostr

Setting the bridge up, natively or in Docker. For what the bridge does, see [README.md](README.md).

## Contents

- [Requirements](#requirements)
- [1. DNS and TLS for the relay](#1-dns-and-tls-for-the-relay)
- [2. nginx](#2-nginx)
- [3. Install and configure](#3-install-and-configure)
  - [Using PostgreSQL](#using-postgresql)
- [4. Register the AppService with your homeserver](#4-register-the-appservice-with-your-homeserver)
- [5. Run the bridge](#5-run-the-bridge)
- [6. Verify](#6-verify)
- [Things to know before going live](#things-to-know-before-going-live)

## Requirements

- A Matrix homeserver that supports Application Services. Any spec-compliant one works (Synapse, Dendrite, conduwuit/continuwuity, ...): the bridge uses only the Client-Server and Application Service APIs. Synapse's Admin API is used only if you turn on `bridge.use_synapse_admin_api`, to count Synapse server admins as bridge admins; otherwise list admins in `bridge.admins`.
- A **handle domain** (`bridge.domain`) whose web server you control: handles are `name@<domain>`, and that domain must answer `/.well-known/nostr.json` by proxying it to the bridge. This is often the homeserver's own domain.
- A **relay hostname** (`nostr.relay_url`), e.g. `nostr.example.org`, with its own TLS certificate.
- A reverse proxy (nginx in the examples) in front of the bridge.
- Python 3.12 or newer (developed on 3.14), or Docker.
- Optionally, PostgreSQL. SQLite works out of the box and is fine for a small server.

## 1. DNS and TLS for the relay

Point the relay hostname (an `A`/`AAAA` record) at the server your reverse proxy runs on, and get a certificate for it, e.g. `certbot certonly --nginx -d nostr.example.org`.

The handle domain needs nothing new beyond a web server you can add a `location` to.

## 2. nginx

`deploy/nginx.conf.example` has both parts:

1. A `location = /.well-known/nostr.json` block for the server that serves your handle domain (`bridge.domain`). It must **proxy**, not redirect: NIP-05 forbids redirects, and clients ignore them.
2. A server block for the relay hostname, which passes everything to the bridge:
   - the relay itself: a WebSocket on `/`, so the `Upgrade`/`Connection` headers must be passed on, with long read and send timeouts (relay connections stay open),
   - its NIP-11 document: `GET /` with `Accept: application/nostr+json`,
   - NIP-86 relay management: `POST /`,
   - published media: `GET`/`HEAD /<sha256>.<ext>`,
   - uploads from users' own Nostr apps: `PUT /upload`, as large as `nostr.max_media_mb`, so `client_max_body_size` must be at least that (`50m` by default),
   - the room widget: `/widget`.

   Don't limit the methods allowed. Pass `X-Forwarded-For`: the relay's rate limits and bans are per client IP, and it believes that header only from `bridge.trusted_proxies` (localhost by default). If nginx runs on another machine or in another container, add its address there. The example also hides `/_matrix/` on the relay host, since only the homeserver needs the AppService API, and reaches it directly.

**Games (optional, `webxdc.enabled`).** Games run on a hostname of their own, `webxdc.sandbox_domain` (`xdc.<your domain>` by default). Give it a DNS record and a certificate (a wildcard certificate for the domain covers it), and proxy it to the bridge exactly like the relay host, passing the `Host` header through: the bridge recognises the sandbox by it, and serves only the game loader there. The example has a server block for it.

Don't add CORS headers yourself, on either host: the bridge sets them, and browsers reject a doubled header.

## 3. Install and configure

**Natively:**

```
./scripts/setup.sh
```

creates a virtualenv, installs the dependencies, and writes `config.yaml` from `config.example.yaml` with fresh random AppService tokens and a fresh `nostr.key_encryption_secret`. Then edit `config.yaml`. At minimum, set:

- `bridge.domain`: the handle domain
- `homeserver.base_url` and `homeserver.server_name`
- `nostr.relay_url`

**Docker:** build the image from the `Dockerfile`, and mount your `config.yaml` at `/config/config.yaml` and a volume at `/data`. In that config, set `storage.data_dir: /data` and `bridge.listen_host: 0.0.0.0`. Generate the secrets yourself:

```
python3 -c "import secrets; print(secrets.token_hex(32))"
```

once each for `hs_token`, `as_token` and `key_encryption_secret`.

### Using PostgreSQL

Create a database and a user for the bridge, for example:

```
sudo -u postgres createuser -P matrix_appservice_nostr
sudo -u postgres createdb -O matrix_appservice_nostr matrix_appservice_nostr
```

Then set `storage.backend: postgresql` and fill in `storage.postgres` (or give a full `dsn`). The bridge creates its tables on first start. `psycopg2` is in `requirements.txt`.

To move an existing SQLite database to Postgres: stop the bridge, run `.venv/bin/python -m bridge.storage migrate config.yaml`, set `backend: postgresql`, and start it again. The SQLite file is only read, so it remains as a backup.

### Back up

**Back up `config.yaml` with the database.** Without `nostr.key_encryption_secret`, the keys in the database can't be decrypted, and every identity they belong to is lost.

## 4. Register the AppService with your homeserver

Generate the registration file:

```
.venv/bin/python -m bridge.appservice config.yaml appservice-registration.yaml
```

Then register it. How depends on the homeserver:

- **Synapse:** add the file's path to `app_service_config_files` in `homeserver.yaml` and restart Synapse.
- **Dendrite:** add it to `app_service_api.config_files` in `dendrite.yaml` and restart Dendrite.
- **conduwuit / continuwuity:** in the admin room, send `!admin appservices register` followed by the file's contents in a code block.
- **Others:** see your homeserver's documentation for registering an Application Service.

The registration's `url` is where the homeserver pushes events: `http://<listen_host>:<listen_port>` by default. If the homeserver can't reach that address (it runs in another container, say), set `bridge.internal_base_url` and generate the file again.

## 5. Run the bridge

```
.venv/bin/python main.py
```

or install `deploy/matrix-appservice-nostr.service` to run it under systemd.

On startup the bridge waits for the homeserver, registers its bot user, and creates its own Nostr identity, `_@<domain>`, on first start. It then runs two checks and logs the results:

- **AppService ping:** whether the homeserver can reach the bridge. Homeservers without the ping endpoint (spec v1.7) are skipped.
- **NIP-05 self-check:** whether `https://<domain>/.well-known/nostr.json?name=_` reaches this bridge, with CORS.

## 6. Verify

- `curl -H 'Accept: application/nostr+json' https://nostr.example.org/` returns the relay's NIP-11 document.
- `curl 'https://<domain>/.well-known/nostr.json?name=_'` returns `{"names": {"_": "<hex key>"}, "relays": {...}}`, with `access-control-allow-origin: *` among the headers.
- Start a direct chat with the bot (`@nostrbot:<server_name>`) and say `[status`. It shows whether the handle check passes. Bridge admins also see the outgoing relay connections.

## Things to know before going live

- **Pick `appservice.user_prefix` and `appservice.bot_localpart` now.** Changing them later orphans the existing users and needs a new registration.
- **The handle domain can move later.** Followers follow the key, not the handle, so nobody is lost. But handles under the old domain stop working unless that domain keeps proxying `/.well-known/nostr.json` to the bridge.
- **Encrypted rooms:** the bridge doesn't support end-to-end encryption, so it can't read encrypted rooms. Start chats with the bot with encryption off. For the same reason `bridge.dm_rooms_as_direct` is off by default: many clients encrypt direct chats.
- **Admins:** list bridge admins in `bridge.admins`. They can ban from the relay (`[ban`, or NIP-86 tools signed by one of their IDs), and they're told in their notifications rooms when the relay raises its shields against a bot wave.
- **Spam rules:** the defaults let strangers reply to and mention your users freely, within rate limits. To make strangers show proof of work, set `spam.inbound_policy: pow`; to follow other people's block lists, set `nostr.shared_block_lists`. See [CONFIG.md](CONFIG.md).
- **Serving followed accounts' posts** to users' own Nostr apps (`nostr.serve_followed_to_local_clients`) is off by default, because every such read is fetched from the homeserver.
