# Configuration

Every setting in `config.yaml`. `config.example.yaml` has the ones needed to start, and the rest commented out with their defaults. The config path is the first argument to `main.py`, else `$BRIDGE_CONFIG`, else `./config.yaml`.

**Changing settings from the widget.** Bridge admins can also change most settings in the widget (Admin, Settings), which lists every setting with its description. A change is stored in the database, over this file's value, and applies straight away; "Reset" goes back to the file's value. Secrets, privileges (`bridge.admins`, `bridge.use_synapse_admin_api`, `bridge.allow_db_setting_overrides`) and what's set once at deployment (domain, URLs, ports, storage, registration, `nostr.relay_url`, `nostr.allow_private_addresses`, `webxdc.sandbox_domain`, `bridge.trusted_proxies`, `logging.level`) can only be set here.

## `bridge`

| Setting | Default | Meaning |
|---|---|---|
| `domain` | required | The domain Nostr handles live under: an ID named `alice` is `alice@<domain>`. Its web server must proxy `/.well-known/nostr.json` to the bridge (see INSTALL.md). It doesn't have to be the homeserver's `server_name`, just as in the sibling bridges. |
| `listen_host` | `127.0.0.1` | Address the bridge's HTTP server binds to. |
| `listen_port` | `8092` | Port it binds to. The siblings default to 8090 (ActivityPub) and 8091 (ATProto), so all three can run on one machine. |
| `internal_base_url` | `http://<listen_host>:<listen_port>` | Where the homeserver pushes AppService transactions; goes into the registration file. Only set it if the homeserver can't reach the default. |
| `admins` | `[]` | Matrix user IDs that are bridge admins: they can use the admin commands, manage the relay over NIP-86, aren't limited by `max_ids_per_user`, and are told when the relay's shields go up. |
| `use_synapse_admin_api` | `false` | Also count Synapse server admins as bridge admins, by asking Synapse's Admin API. **Synapse only**, and the only Synapse-specific thing the bridge does; everything else works on any homeserver. Needs `homeserver.synapse_admin_token`. |
| `allow_db_setting_overrides` | `true` | Let bridge admins change settings from the widget (see above). Off, changes stored earlier are ignored but kept, the widget's settings editor is read-only, and changes are refused. Always read from this file. |
| `third_party_access` | `true` | Master switch for users of **other** homeservers. Who among them may use the bridge, and whether they get `full` or `shadow` access, is set per user, homeserver or room with the [`[allow`](COMMANDS.md#allow-mxidhomeserverroom-value-fullshadow) command; this only says whether those grants count right now. With no grants, only this homeserver's users can use the bridge whether it's on or off. Turning it off deletes nothing: anyone already allowed just has everything they send to Nostr refused with a notice until it's back on. |
| `space_name` | `Nostr` | Name of the space the bridge keeps for each user, holding their Profile Rooms, notifications room and the rooms of accounts they follow. |
| `space_avatar_mxc` | the bot's avatar | Avatar (`mxc://`) of that space. |
| `notifications_room_name` | `Nostr Notifications` | Name of each user's notifications room. |
| `notifications_room_avatar_mxc` | the bot's avatar | Its avatar. |
| `ghost_room_join_rule` | `knock` | Join rule of Remote User Rooms (one per followed account): `knock` (a local user's knock is let straight in), `invite` or `public`. |
| `world_readable_remote_rooms` | `false` | Make Remote User Rooms readable without joining (`history_visibility: world_readable`). |
| `accept_federated_knocks` | `false` | Also let in knocks on Remote User Rooms from users of other homeservers. |
| `backstage_space_enabled` | `false` | Keep one hidden "backstage" space that every ghost joins once, and set the join rule of rooms ghosts are added to (Remote User Rooms, NIP-29 group rooms, Concord channels, Git repo rooms) to `restricted`/`knock_restricted` on that space, so a ghost joins with one membership event instead of an invite plus a join. Rooms are converted the first time a ghost needs in; a room whose join rule the bridge can't change keeps using invites. DM and notification rooms are never changed. `adhoc_backfill_backstage_space.py` converts existing rooms up front. Uses the Synapse Admin API for the space join only if `use_synapse_admin_api` is on. |
| `dm_rooms_as_direct` | `false` | Create DM rooms as Matrix direct chats (`is_direct`). Only turn on where clients don't end-to-end encrypt direct chats by default: the bridge can't read encrypted rooms. |
| `shadow_id_dms` | `false` | Whether shadow IDs (no Profile Room of their own: made automatically for a reply, reaction or `[join`, including for anyone an [`[allow room`](COMMANDS.md#allow-mxidhomeserverroom-value-fullshadow) grant lets in) take Nostr DMs. Off, DMs to them are refused and no DM room is made; once their user runs `[create profile`, the same ID takes DMs. |
| `shadow_id_notice` | `true` | Whether the bot says so in the room when it makes someone a shadow ID ("I've created the Nostr identity … for you"). Off, shadow IDs are made silently. |
| `trusted_proxies` | `["127.0.0.1", "::1"]` | Addresses of reverse proxies whose `X-Forwarded-For` is believed. The relay's per-IP rate limits and bans use the address it gives. |
| `set_msc4501_relates_to` | `true` | Mark reposts, quotes and reply copies with an MSC4501 relation to the original post. A quote or reply copy carries the original post's content inside the relation, and its own words alone as `social.body`/`social.formatted_body`, without the "⤵️ Reply to" or "🔁 reposted" line. A plain repost's `body` is only the link to the post, with the post's words as `social.body`. Same setting as the sibling bridges. |
| `use_msc4501_content_inline` | `true` | On a plain repost's MSC4501 relation, say the repost carries the post's content inline instead of repeating it inside the relation. Never used for quotes or replies. |
| `use_msc4501_post_event_type` | `false` | Send followed accounts' posts as `org.matrix.msc4501.social.post` events instead of `m.room.message`. Most clients can't show those yet. |
| `set_msc4501_profile_room` | `false` | Point each ghost's Matrix profile at its Remote User Room (MSC4501, an MSC4133 profile field; needs MSC4133 on the homeserver). |
| `msc4144_per_message_profiles` | `true` | Posts made from your other Nostr apps are mirrored into the Profile Room by the bot; with this on, they're shown under the ID's name (MSC4144). |
| `use_msc4589_formatted_displayname` | `false` | Set `org.matrix.msc4589.formatted_displayname` (still a draft MSC) on a ghost whose profile name has a `:shortcode:` custom emoji with a matching image, so a compliant client can render it inline instead of the literal text. The plain displayname keeps that literal text either way. The same setting gives the ghost's Remote User Room the matching formatted room name (`org.matrix.msc4591.formatted_name` in `m.room.name`, draft MSC4591), so the two are formatted together or not at all. Off by default since no client supports these MSCs yet. |
| `quote_import_policy` | `known` | Whether a quoted post that isn't in Matrix yet is brought in: `known` (only if its author already has a room here), `always`, or `never`. Otherwise the quote links to it on `nostr.web_viewer_url`. |
| `local_profile_room_join_rule` | `knock` | Join rule of new Profile Rooms: `knock` (the owner's knock is let straight in; anyone else's is left to the people inside), `invite` or `public`. |
| `msc4503_external_handle` | `off` | Where a ghost's real Nostr handle (its verified NIP-05, else its `npub`) is recorded (MSC4503): `profile` (an MSC4133 profile field; needs MSC4133 on the homeserver), `events` (on every event it sends), `both`, or `off`. Same setting as the sibling bridges. The protocol icon is `appservice.bot_avatar_mxc`. |
| `msc4027_custom_reactions` | `false` | Show custom-emoji reactions from Nostr (NIP-30, `:name:` with an image) as MSC4027 image reactions: the image's `mxc://` as the reaction key and the `:name:` in a `shortcode` field, as Element does it. Off by default, as in matrix-appservice-activitypub: clients that don't support MSC4027 show the `mxc://` text instead of the emoji. Off, the reaction is the `:name:` text. |
| `client_name` | `matrix-appservice-nostr` | Put in a NIP-89 `client` tag on public events the bridge signs (notes, replies, reposts, reactions, polls, comments), which clients like Ditto show next to the time. The tag points at the bridge's NIP-89 app announcement (kind 31990, signed by `_@<domain>` and published at startup), where apps such as Radar Nostr find the icon and link below. Empty for no tag. Never added to DMs, gift wraps or group/Concord events. |
| `client_picture` | Haven's anchor logo | Icon URL in the app announcement. Empty to leave it out. |
| `client_website` | `https://github.com/Haven-Organization/matrix-appservice-nostr` | Link in the app announcement. Empty to leave it out. |
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
| `other_command_prefixes` | `[]` | Command prefixes of other bots that share rooms with this one, such as the sibling bridges (`[";", "]"]`). A message whose first line starts with one of them, straight away followed by a word and then a space or the end of the line (`]follow alice.bsky.social`, `;help`), is that bot's command: it's never published or answered here. `[Update] …` isn't matched, since the word is followed by `]`. Can't include `command_prefix`. |
| `other_bot_users` | `[]` | MXIDs of other bots that share rooms with this one. A message that mentions one (a pill, or the MXID typed in its first line) is meant for that bot and is never published here. Messages these bots send are never acted on. |
| `other_bridge_users` | `[]` | Regexes over the whole MXID matching other bridges' ghosts (`"@bsky_.*:example\\.org"`, `"@fedi_.*:example\\.org"`). Their messages, reactions, votes and redactions are never acted on: they can't run commands, create IDs or be published. Room state they set (a name or avatar change) still updates the profile. Copy the patterns from the other bridges' registration files. |

## `storage`

| Setting | Default | Meaning |
|---|---|---|
| `backend` | `sqlite` | `sqlite` (a single file, `bridge.db`, under `data_dir`) or `postgresql` (needs `psycopg2`, in `requirements.txt`). Postgres is the better choice for a busy server. |
| `data_dir` | `./data` | Where the SQLite file lives. |
| `postgres.dsn` | unset | A full connection string. Wins over the fields below. |
| `postgres.host`, `.port`, `.database`, `.user`, `.password` | `localhost`, `5432`, `matrix_appservice_nostr`, `matrix_appservice_nostr`, unset | The Postgres connection, field by field. |

To move an existing SQLite database to Postgres: stop the bridge, run `python -m bridge.storage migrate config.yaml`, set `backend: postgresql`, and start it again. The SQLite file is only read, so it remains as a backup.

## `nostr`

| Setting | Default | Meaning |
|---|---|---|
| `relay_url` | required | The bridge's own relay, as clients reach it (`wss://nostr.example.org`). Its NIP-11 document, NIP-86 management, media, uploads and the widget are served from the same host. Listed as every identity's relay in NIP-05 answers and relay lists. |
| `key_encryption_secret` | required | Secret (at least 32 characters) that every Nostr secret key in the database is encrypted with. **Back it up with the database**: without it, the keys can't be decrypted. Changing it makes existing keys unreadable. |
| `publish_relays` | Damus, nos.lol, Primal, poster.place (both addresses) | Public relays local identities' events are also sent to, for reach. |
| `discovery_relays` | purplepag.es, Damus | Relays asked for other accounts' relay lists and profiles. |
| `fallback_relays` | Damus, nos.lol, Primal | Relays read for accounts that publish no relay list. With `publish_relays`, also what the widget's Global feed reads. |
| `search_relays` | relay.nostr.band, search.nos.today | Relays the widget's search box asks for accounts and posts by text (NIP-50). An npub, `name@domain` or post link is looked up directly instead. |
| `backfill_on_follow` | `20` | How many of an account's latest posts are brought in when it's first followed. `0` turns it off. |
| `serve_followed_to_local_clients` | `false` | Let a local ID's own Nostr apps read the posts of the accounts it follows through the bridge's relay (after NIP-42 login). Off by default: every such read is fetched from the homeserver, and a timeline can be hundreds of events. Off, those apps read the accounts' own relays, as usual. |
| `relay_max_limit` | `500` | Most events one `REQ` filter can return (NIP-11 `max_limit`). |
| `relay_max_subscriptions` | `100` | Most open subscriptions per relay connection. poster.place opens up to 100 on one. |
| `shared_block_lists` | `[]` | Accounts (`npub` or hex) whose public mute lists (kind 10000) and reports (kind 1984) the relay honours: anyone on them is refused, unless someone here follows them. Read from `discovery_relays` and `fallback_relays` at startup and every 6 hours. |
| `web_viewer_url` | `https://ditto.pub/` | Web page a Nostr profile or event can be opened on, followed by its `npub`/`nevent`/`naddr`. Used for the `external_url` of bridged posts, links to posts that aren't in Matrix, long-form articles, and MSC4503 handles. |
| `max_media_mb` | `50` | Largest file published from Matrix (Profile Rooms and DMs; images are also capped at 5 MB after cleaning), and largest Blossom upload (`PUT /upload`). Keep the reverse proxy's body size limit at least this big. |
| `refused_relay_retry_hours` | `24` | A relay that refuses an ID by policy (`blocked:`/`restricted:`) isn't tried again for that ID for this long. Only refusals from `publish_relays` are reported to the user; the inboxes of whoever a post is aimed at are theirs to choose. |
| `skip_relays` | nostr.land, nostr.wine, Primal Premium and other paid relays | Relays never sent to, even as the inbox of someone a post is aimed at. A relay that refuses asking for payment is skipped for every ID for 30 days without a notice. |
| `allow_private_addresses` | `false` | Let the bridge connect to private, loopback and link-local addresses: relays, media, NIP-05 lookups. **Local development only.** Off, every such address is refused, because these URLs are chosen by strangers. |

About the default relays: poster.place is a web-of-trust relay. It only accepts posts from accounts inside its web of trust, so expect it to refuse new identities at first. It also requires authentication even to read, which is why it isn't a fallback relay.

## `spam`

The relay's spam rules (see [FEATURES.md](FEATURES.md#spam-protection)). Every setting is optional; an unknown one is an error. They apply to strangers' events, whether written to the bridge's relay or picked up from other relays; local IDs are only held to the size and clock checks.

A rate limit is a token bucket: `{burst: <most at once>, tokens: <added back>, per_seconds: <every so many seconds>}`.

| Setting | Default | Meaning |
|---|---|---|
| `connections` | `{burst: 100, tokens: 1, per_seconds: 6}` | New relay connections per IP: 100 at once, then one every 6 seconds (600 an hour). Web clients reconnect often, so keep this well above what one person opens. |
| `reqs` | `{burst: 300, tokens: 120, per_seconds: 60}` | Subscriptions (`REQ`) per IP. Web clients re-subscribe often, and some (poster.place) stop using a relay for hours after one "rate-limited" answer. |
| `events_per_ip` | `{burst: 10, tokens: 2, per_seconds: 180}` | Events per IP. |
| `events_per_pubkey` | `{burst: 10, tokens: 5, per_seconds: 3600}` | Events per key. |
| `events_per_pubkey_target` | `{burst: 5, tokens: 2, per_seconds: 3600}` | Events per key aimed at one local ID. |
| `trusted_multiplier` | `5.0` | Accounts someone here follows get per-key buckets this many times larger. |
| `max_event_bytes` | `65536` | Largest event, as JSON. |
| `future_skew_seconds` | `900` | How far in the future `created_at` may be. |
| `max_interaction_age_seconds` | `259200` (3 days) | How old a reply, reaction, repost or zap written to the relay may be. Events the bridge fetches itself (catching up) aren't held to this. |
| `max_p_tags` | `20` | Most accounts a post, reply or reaction may mention ("hellthreads"). Lists aren't counted. |
| `duplicate_window_seconds` | `600` | Window for spotting the same text from several accounts. |
| `duplicate_min_pubkeys` | `3` | That many different accounts sending the same text within the window is a wave, and refused. |
| `duplicate_min_length` | `12` | Shorter texts ("gm") are too common to judge. |
| `spam_patterns` | `[]` | Regular expressions; content matching any is refused. |
| `stranger_fast_reply_seconds` | `60` | A reply to someone here from an account nobody here follows, written less than this many seconds after the post, is refused: bots watch for new posts and answer at once, people take longer. `0` turns it off. |
| `stranger_scam_patterns` | crypto-giveaway wording | Regular expressions; a post from an account nobody here follows that has a link and matches any of them is refused. The default list catches wallet-drainer bait ("airdrop", "verify your wallet", "ETH giveaway", "2,000 USDC waiting"); see `DEFAULT_SCAM_PATTERNS` in `bridge/spam.py`. Set your own list to replace it, or `[]` to turn it off. |
| `enabled` | `true` | `false` turns every spam check off, except bans an admin set by hand, size and timestamp sanity (`max_event_bytes`, `future_skew_seconds`, `max_interaction_age_seconds`) and the `connections`/`reqs` limits, which protect the relay itself. No strikes are counted and automatic bans already made stop counting. |
| `inbound_policy` | `open` | What strangers (not followed here, and not followed by anyone followed here) need: `open` (nothing), `pow` (NIP-13 proof of work, with a committed target) or `web_of_trust` (refused). |
| `stranger_pow_bits` | `20` | Proof of work asked of strangers under `pow`. |
| `strikes_to_ban` | `10` | Refusals (strikes) within `strike_window_seconds` that ban a key or IP automatically. |
| `strike_window_seconds` | `600` | Window strikes are counted in. |
| `ban_durations_seconds` | `[3600, 86400, 604800]` | Length of a first, second, third (and later) automatic ban. |
| `escalation_memory_days` | `30` | A new ban within this long of the last one is the next step up. |
| `wave_threshold` | `30` | That many never-seen strangers reaching for someone here (tagging a local user, or referring to an event the relay holds) within `wave_window_seconds` raises the shields. |
| `wave_window_seconds` | `300` | Window for counting them. |
| `wave_policy` | `pow` | Policy while the shields are up (`open`, `pow` or `web_of_trust`). Only ever tightens `inbound_policy`. |
| `wave_cooldown_seconds` | `3600` | The shields come down after this long without a wave. |

## `webxdc`

Games: webxdc mini apps over Nostr (NIP-DC). See [FEATURES.md](FEATURES.md#games).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn games on: game posts get a "🎮 … reply `[play`" line, `[play` works, and the relay takes moves for games it knows. |
| `sandbox_domain` | `xdc.` + `bridge.domain` | The hostname games run on. It must be its own hostname, never the relay's or the homeserver's, so an app can't reach either's storage. Route it to the bridge like the relay host (see INSTALL.md); a wildcard certificate for the domain covers it. |
| `active_days` | `14` | How long after a game was last opened the bridge keeps following its moves on other relays. |
| `max_update_kb` | `128` | The largest move accepted, in KB (the webxdc default for `sendUpdateMaxSize`). |
| `avatar_mxc` | the bot's avatar | Avatar (`mxc://`) of Games rooms and the Nostr Games widget. An existing Games room and its widget pick it up the next time a game is opened there. |
| `max_app_mb` | `100` | The largest game the bridge downloads for a player whose browser can't download it from where it was posted (a host without CORS). It's passed through, never stored. |

## `encrypted_media`

Files forwarded from end-to-end encrypted rooms: posting one needs a decrypted copy, made once the sender agrees. See [DESIGN.md](DESIGN.md#encrypted-media).

| Key | Default | Meaning |
|---|---|---|
| `bridges` | `[]` | The bot MXIDs of this server's bridges that share one decrypted copy (this one, matrix-appservice-activitypub, matrix-appservice-atproto), in the order they ask. Include this bridge. Empty: this bridge asks on its own. Every bridge listed should have the same list. |
| `wait_seconds` | `5` | How long each bridge waits after the one before it, before asking itself. |

## `logging`

| Setting | Default | Meaning |
|---|---|---|
| `level` | `INFO` | Python log level. |
