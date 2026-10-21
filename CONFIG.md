# Configuration

Every setting in `config.yaml`. `config.example.yaml` has only the ones needed to start. The config path is the first argument to `main.py`, else `$BRIDGE_CONFIG`, else `./config.yaml`.

## `bridge`

| Setting | Default | Meaning |
|---|---|---|
| `domain` | required | The domain Nostr handles live under: an ID named `alice` is `alice@<domain>`. Its web server must proxy `/.well-known/nostr.json` to the bridge (see INSTALL.md). It doesn't have to be the homeserver's `server_name`, just as in the sibling bridges. |
| `listen_host` | `127.0.0.1` | Address the bridge's HTTP server binds to. |
| `listen_port` | `8092` | Port it binds to. The siblings default to 8090 (ActivityPub) and 8091 (ATProto), so all three can run on one machine. |
| `internal_base_url` | `http://<listen_host>:<listen_port>` | Where the homeserver pushes AppService transactions; goes into the registration file. Only set it if the homeserver can't reach the default. |
| `admins` | `[]` | Matrix user IDs that are bridge admins. |
| `use_synapse_admin_api` | `false` | Also count Synapse server admins as bridge admins, by asking Synapse's Admin API. **Synapse only**, and the only Synapse-specific thing the bridge does; everything else works on any homeserver. Needs `homeserver.synapse_admin_token`. |
| `space_name` | `Nostr` | Name of the space the bridge keeps for each user, holding their Profile Rooms and notifications room. |
| `space_avatar_mxc` | the bot's avatar | Avatar (`mxc://`) of that space. |
| `notifications_room_name` | `Nostr Notifications` | Name of each user's notifications room. |
| `notifications_room_avatar_mxc` | the bot's avatar | Its avatar. |
| `local_profile_room_join_rule` | `knock` | Join rule of new Profile Rooms: `knock` (the owner's knock is let straight in; anyone else's is left to the people inside), `invite` or `public`. |
| `max_ids_per_user` | `5` | How many Nostr IDs one Matrix user may hold. Bridge admins are exempt. |
| `id_min_length` / `id_max_length` | `1` / `32` | Allowed length of an ID's name. |
| `reserved_ids` | `[]` | Names nobody may take, on top of the built-in list (`admin`, `support`, `nostr`, ...). Exact names or shell-style globs (`staff*`). |
| `id_allowed_users` | `[]` | Who may have Nostr IDs: MXID globs (`@*:example.org`). Empty means every local user. |
| `renamed_id_grace_days` | `30` | After a rename, how long the old handle keeps resolving to the ID. |
| `rename_cooldown_days` | `7` | Minimum time between two renames of the same ID. |

## `homeserver`

| Setting | Default | Meaning |
|---|---|---|
| `base_url` | required | The Client-Server API base URL, as reachable from the bridge. |
| `server_name` | required | The homeserver's `server_name` (the part after the colon in `@user:server.name`). Only its users can use the bridge. |
| `synapse_admin_token` | unset | Access token of a Synapse server admin. Only used, and only required, with `bridge.use_synapse_admin_api` on. |

## `appservice`

| Setting | Default | Meaning |
|---|---|---|
| `registration_path` | required | Where `python -m bridge.appservice` writes the registration file by default. |
| `id` | required | The AppService's id, as in the registration file. |
| `hs_token` | required | Secret the homeserver presents to the bridge. |
| `as_token` | required | Secret the bridge presents to the homeserver. |
| `user_prefix` | `nostr_` | Localpart prefix of ghost users for remote Nostr accounts (`@nostr_<hex key>`). Decide before going live. |
| `bot_localpart` | `nostrbot` | The bot's localpart. Must not start with `user_prefix`. |
| `bot_display_name` | unset | The bot's display name, applied on startup if set. |
| `bot_avatar_mxc` | unset | The bot's avatar (an `mxc://` URI), applied on startup if set. |
| `command_prefix` | `[` | What commands start with. Must differ from other bots in the same rooms (matrix-appservice-activitypub uses `;`, matrix-appservice-atproto `]`). |

## `storage`

| Setting | Default | Meaning |
|---|---|---|
| `backend` | `sqlite` | `sqlite` (a single file, `bridge.db`, under `data_dir`) or `postgresql`. |
| `data_dir` | `./data` | Where the SQLite file lives. |
| `postgres.dsn` | unset | A full connection string. Wins over the fields below. |
| `postgres.host`, `.port`, `.database`, `.user`, `.password` | `localhost`, `5432`, `matrix_appservice_nostr`, `matrix_appservice_nostr`, unset | The Postgres connection, field by field. |

To move an existing SQLite database to Postgres: stop the bridge, run `python -m bridge.storage migrate config.yaml`, set `backend: postgresql`, and start it again. The SQLite file is only read, so it remains as a backup.

## `nostr`

| Setting | Default | Meaning |
|---|---|---|
| `relay_url` | required | The bridge's own relay, as clients reach it (`wss://nostr.example.org`). Its NIP-11 document and media are served from the same host. Listed as every identity's relay in NIP-05 answers. |
| `key_encryption_secret` | required | Secret (at least 32 characters) that every Nostr secret key in the database is encrypted with. **Back it up with the database**: without it, the keys can't be decrypted. Changing it makes existing keys unreadable. |
| `publish_relays` | Damus, nos.lol, Primal, poster.place (both addresses) | Public relays local identities' events are also sent to, for reach. |
| `discovery_relays` | purplepag.es, Damus | Relays asked for other accounts' relay lists and profiles. |
| `fallback_relays` | Damus, nos.lol, Primal | Relays read for accounts that publish no relay list. |
| `max_media_mb` | `50` | Largest file published from a Profile Room (images are also capped at 5 MB after cleaning). |
| `refused_relay_retry_hours` | `24` | A relay that refuses an ID by policy (`blocked:`/`restricted:`) isn't tried again for that ID for this long. |
| `allow_private_addresses` | `false` | Let the bridge connect to private, loopback and link-local addresses: relays, media, NIP-05 lookups. **Local development only.** Off, every such address is refused, because these URLs are chosen by strangers. |

About the default relays: poster.place is a web-of-trust relay. It only accepts posts from accounts inside its web of trust, so expect it to refuse new identities at first. It also requires authentication even to read, which is why it isn't a fallback relay.

## `logging`

| Setting | Default | Meaning |
|---|---|---|
| `level` | `INFO` | Python log level. |
