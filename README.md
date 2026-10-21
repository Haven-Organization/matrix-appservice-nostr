# matrix-appservice-nostr

Turns your Matrix server into a Nostr server. Every Matrix user can have one or more Nostr identities, with handles on your own domain (`alice@example.org`), and post, follow, reply, react and DM across Nostr from ordinary Matrix rooms and clients. The bridge is also a Nostr relay: the home relay for your users, where the rest of the network finds their posts.

It is a sibling of [matrix-appservice-activitypub](https://github.com/Haven-Organization/matrix-appservice-activitypub) and [matrix-appservice-atproto](https://github.com/Haven-Organization/matrix-appservice-atproto). It uses the same room model, command structure and storage rules, and all three can run side by side on one homeserver.

> **Status: in development.** Working: Nostr IDs (several per user, with chosen names, switching and renaming), each with a Profile Room whose name, topic, avatar and banner are its Nostr profile; posting from Profile Rooms (text, media, mentions, quotes, polls, threads, pins, deletions); the bridge's own relay; following Nostr accounts, each in its own room; replies, reactions, reposts and quotes both ways; NIP-17 DMs; polls both ways; notifications; bringing your own key (`nsec`, `ncryptsec` or a NIP-46 remote signer) through a widget; Blossom media serving and uploads; and layered spam protection with NIP-86 relay management. Webxdc games are an optional last phase, not built. See [FEATURES.md](FEATURES.md) for how things work, and [DESIGN.md](DESIGN.md) for the design.

## How it works

- **Any homeserver.** The bridge talks to Matrix only through the Client-Server API and the Application Service API, never a homeserver's database, so any spec-compliant homeserver works. The Synapse Admin API is optional and off by default (`bridge.use_synapse_admin_api`).
- **No content outside Matrix.** Posts and media live in Matrix rooms and the Matrix media repository, and nowhere else. The bridge's own database (SQLite or Postgres) holds only bookkeeping: identities and their keys, follow relationships, bans, and maps between Nostr events and Matrix events. The relay serves events by rebuilding them from Matrix, or by reading the original signed event carried inside the Matrix event. Redact the Matrix event, and the Nostr event is gone from the relay too. See [DESIGN.md, "The storage rule"](DESIGN.md#the-storage-rule).
- **Keys encrypted at rest, and never in chat.** Nostr keys can't be rotated without losing the identity, so every secret key in the database is encrypted with a secret that lives in the config file, not the database. Keys are imported and exported only through the bridge's widget, over HTTPS; an `nsec` pasted into a room is redacted. Or keep your key in a NIP-46 remote signer, and the bridge never holds it.
- **A relay of its own.** The bridge is your users' home relay (`nostr.relay_url`). It serves their posts, and takes events aimed at them: replies, reactions, mentions, DMs. Everything else is refused, and strangers go through rate limits, content checks and trust tiers first.
- **NIP-05 handles on your domain.** Your domain's web server proxies `/.well-known/nostr.json` to the bridge, and every identity gets `name@your-domain`.

## Controlling the bridge

Tag the bot or type a `[`-prefixed command: `[create profile` to get a Nostr ID, `[follow <account>`, `[dm <account>`, `[block <account>`, `[widget`, `[status`, `[help`. See [COMMANDS.md](COMMANDS.md).

## Setup

See [INSTALL.md](INSTALL.md). In short: `./scripts/setup.sh`, edit `config.yaml`, set up nginx for the relay hostname (WebSocket, `POST /`, `PUT /upload`) and for `/.well-known/nostr.json` on your handle domain (`deploy/nginx.conf.example`), register the AppService with your homeserver, then run `python main.py`. Every setting is described in [CONFIG.md](CONFIG.md).

## Development

```
pip install -r requirements-dev.txt
python -m pytest
```

Signing and verification are pure Python (`bridge/nostr/secp256k1.py`, tested against the BIP340 vectors), so the bridge needs no native build. If [coincurve](https://pypi.org/project/coincurve/) is installed, it is used instead and is much faster.

## License

GPLv3. See [LICENSE](LICENSE).
