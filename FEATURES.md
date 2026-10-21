# Features

How each thing is bridged so far. For the commands, see [COMMANDS.md](COMMANDS.md); for what's still to come, [DESIGN.md](DESIGN.md#build-order).

## Nostr IDs

Each Matrix user can hold several Nostr IDs (`[create profile`, `[profiles`, `[switch`, `[rename`). Each ID has its own key, its own NIP-05 handle (`name@<bridge.domain>`), and its own **Profile Room**.

## Your Profile Room

**Its name, topic, avatar and banner are the ID's Nostr profile.** Change any of them and a new profile (kind 0) is published. The avatar and banner are cleaned first: rotation applied, EXIF metadata (which often holds GPS coordinates) removed, and re-encoded if needed. The cleaned copy is stored back in Matrix and served at `https://<relay host>/<sha256>.<ext>`.

**What you post there is published on Nostr**, as that room's ID, whichever ID is active:

- **Text and emotes** become notes (kind 1). Nostr has no length limit, so a long message stays one note. An emote (`/me waves`) is published as `*waves*`.
- **Images** become notes with the image's URL and an `imeta` tag (type, hash, size, dimensions). They're cleaned the same way as avatars, and are at most 5 MB after cleaning.
- **Video, audio and other files** are published as they are, up to `nostr.max_media_mb` (50 MB by default). Their metadata isn't stripped, so a video can still carry its location.
- **Captions** become the note's text, with the file's URL on the next line.
- **Mentions:** a pill of a local user who has a Nostr ID becomes a `nostr:npub…` reference and a `p` tag, pointing at the ID that user had active at the time.
- **Spoilers** add a `content-warning` tag, with the spoiler's reason if it has one.
- **Replies and threads:** a rich reply or thread message answering one of the ID's own posts becomes a NIP-10 reply to it, so threads stay threads on Nostr. A reply to anything else is an ordinary post.
- **Redacting** a published message publishes a deletion (kind 5) for it, with the redaction's reason as the deletion's text.

**Not published:**

- edits (Nostr notes can't be edited)
- commands, and anything else addressed to the bot
- notices
- empty messages
- encrypted files (the bridge can't read encrypted rooms)
- files over the size limit (the bot tells you why)

**Other people in your Profile Room:** a local user who replies to one of your posts publishes that reply from *their own* active ID, as a reply to your post. Anything else they say there is conversation and stays in Matrix.

## Publishing, and when it doesn't work

A post is published once the bridge has it: its own relay (`nostr.relay_url`) serves it to anyone who looks your ID up. Copies also go to `nostr.publish_relays`, for reach:

- **No public relay took it:** the bot replies to your message saying so. The bridge keeps retrying (after 1 minute, 5 minutes, 30 minutes, then every 6 hours), and the retries survive a restart.
- **A relay refuses an ID by policy** (`blocked:` or `restricted:`, like a web-of-trust relay that doesn't know the ID yet): you're told once. That relay is skipped for that ID for `nostr.refused_relay_retry_hours` (24 by default), rather than being asked again on every post.

The bridge stores no post content of its own. When a post has to be sent again, it is rebuilt from the Matrix message, and must hash to the same Nostr event ID.

## Handles

Every ID's handle is answered at `https://<bridge.domain>/.well-known/nostr.json?name=<name>`, along with the bridge's relay as the place to find its events. After a rename, the old handle keeps answering for `bridge.renamed_id_grace_days`.
