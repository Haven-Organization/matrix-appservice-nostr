# Commands

A message is a command when it starts with the command prefix (`[` by default, `appservice.command_prefix`) followed straight away by a command, or when it tags the bot (`@nostrbot:example.org status`). Only the first line counts, and case doesn't matter. A `[` followed by anything else (`[citation needed]`) is an ordinary message.

Only users of the bridge's own homeserver can use commands.

The same sections are shown by `[help`. Keep this file and `_HELP_SECTIONS` in `bridge/commands.py` in sync.

## Contents

- [Profile](#profile)
  - [`[create profile`](#create-profile)
  - [`[create profile <id>`](#create-profile-id)
  - [`[profiles`](#profiles)
  - [`[switch <number or id>`](#switch-number-or-id)
  - [`[rename <number or id> <new id>`](#rename-number-or-id-new-id)
- [General](#general)
  - [`[help [section]`](#help-section)
  - [`[status`](#status)
- [IDs, and which one is used](#ids-and-which-one-is-used)

## Profile

### `[create profile`

Creates a Nostr ID named after your Matrix username: `@alice:example.org` gets `alice@<bridge.domain>`. The ID gets:

- a new key,
- a **Profile Room**, whose name, topic and avatar (and banner, MSC4221) are the ID's Nostr profile. Change them and the profile on Nostr follows.
- a NIP-05 handle that Nostr apps can look up.

You're invited to the Profile Room, your space and your notifications room. Your first ID becomes your active one.

If your Matrix username can't be a Nostr ID (Nostr IDs can only contain lowercase letters, digits, dashes, underscores and dots), or someone else already has that name, the bot says why and offers a close alternative (`q+test` → `q-test`, or `bob` → `bob-2`). Reply **confirm** to that message to take it, or choose any name with `[create profile <id>`.

Run it again for an ID you already have, and you're re-invited to its Profile Room and your other bridge rooms. That's the way back in if you left one.

### `[create profile <id>`

The same, with a name you choose: `[create profile qart` creates `qart@<bridge.domain>`. The name must:

- use only lowercase letters, digits, dashes, underscores and dots, starting and ending with a letter or digit,
- be between `bridge.id_min_length` and `bridge.id_max_length` characters (1 to 32 by default),
- not be reserved (`admin`, `support`, `nostr`, ... and `bridge.reserved_ids`),
- not be taken by another ID, or retired from someone else's,
- not be another user's Matrix username: `bob` is kept for `@bob`.

Each user can have up to `bridge.max_ids_per_user` IDs (5 by default; bridge admins aren't limited). An ID after your first isn't made active; use `[switch` for that. Its Profile Room is named after the ID rather than after you, since a second identity is usually meant to look different.

### `[profiles`

Lists your IDs, numbered in the order you created them, and marks the active one:

```
Your Nostr IDs:
1. q@example.org  ← active
2. qbot@example.org
```

The numbers are what `[switch` and `[rename` take.

### `[switch <number or id>`

Makes one of your IDs the active one: `[switch 2`, `[switch qbot` or `[switch qbot@example.org`.

### `[rename <number or id> <new id>`

Gives an ID a new name and keeps its key, so its followers, posts and Profile Room all stay. The bot says what will change. Reply **confirm** to that message to go ahead.

- The new name follows the same rules as `[create profile <id>`.
- The old handle keeps working for `bridge.renamed_id_grace_days` (30 by default). After that, it stays reserved for you: nobody else can take it, and you can take it back.
- An ID can be renamed once every `bridge.rename_cooldown_days` (7 by default).

## General

### `[help [section]`

Lists every command, or only the commands in one section (`[help profile`).

### `[status`

Shows:

- your Nostr IDs, numbered, with their `npub`s, and which is active,
- the handle domain, and whether the bridge's NIP-05 self-check passes (`https://<domain>/.well-known/nostr.json` reaching the bridge),
- the bridge's relay URL.

Bridge admins also see each outgoing relay connection and whether it is connected.

## IDs, and which one is used

When you do something on Nostr, the bridge uses:

1. **Inside one of your Profile Rooms:** that room's ID, always.
2. **Inside a DM room** (coming later): the ID that conversation belongs to.
3. **Anywhere else:** your active ID.

Undoing something (removing a reaction, say) always uses the ID that did it in the first place.
