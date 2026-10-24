# Commands

A message is a command when it starts with the command prefix (`[` by default, `appservice.command_prefix`) followed straight away by a command, or when it tags the bot (`@nostrbot:example.org status`). Only the first line counts, and case doesn't matter. A `[` followed by anything else (`[citation needed]`) is an ordinary message. A command sent in a Profile Room is never published.

Only users of the bridge's own homeserver can use commands.

The same sections are shown by `[help`, laid out like matrix-appservice-activitypub's. Keep this file and `_help_sections` in `bridge/commands.py` in sync.

Where a command takes an `<account>`, any of these work: `npub1…`, `nprofile1…`, a 64-character hex key, a handle (`name@domain`, or a bare `domain` for `_@domain`), or a link to a profile on a Nostr website. In a Nostr account's room, or a DM room with one, the account can be left out: `[follow`, `[unfollow`, `[block`, `[unblock` and `[dm` then mean that account.

## Contents

- [Profile](#profile)
  - [`[create profile`](#create-profile)
  - [`[create profile <id>`](#create-profile-id)
  - [`[id [<number|id>]`](#id-numberid)
  - [`[create room <number or id>`](#create-room-number-or-id)
- [General](#general)
  - [`[help [section]`](#help-section)
  - [`[follow <account>`](#follow-account)
  - [`[unfollow <account>`](#unfollow-account)
  - [`[following`](#following)
  - [`[dm <account>`](#dm-account)
  - [`[join <invite link>`](#join-invite-link)
  - [`[play [<link>]`](#play-link)
  - [`[import <link|nevent|note|naddr>`](#import-linkneventnotenaddr)
- [Advanced/Maintenance](#advancedmaintenance)
  - [`[backfill [N]`](#backfill-n)
  - [`[block <account>`](#block-account)
  - [`[unblock <account>`](#unblock-account)
  - [`[widget`](#widget)
  - [`[import key`](#import-key)
  - [`[export key`](#export-key)
  - [`[status`](#status)
- [Admin](#admin)
  - [`[ban <npub, handle or IP> [duration] [purge] [reason]`](#ban-npub-handle-or-ip-duration-purge-reason)
  - [`[unban <npub, handle or IP>`](#unban-npub-handle-or-ip)
  - [`[bans`](#bans)
- [⚠️ Danger Zone](#️-danger-zone)
  - [`[link <number or id> [keep]`](#link-number-or-id-keep)
  - [`[unlink <number or id>`](#unlink-number-or-id)
  - [`[rename <number or id> <new id>`](#rename-number-or-id-new-id)
  - [`[delete profile [<number|id>]`](#delete-profile-numberid)
- [IDs, and which one is used](#ids-and-which-one-is-used)

## Profile

Setting up and maintaining your own Nostr identities.

### `[create profile`

Creates a Nostr ID named after your Matrix username: `@alice:example.org` gets `alice@<bridge.domain>`. The ID gets:

- a new key,
- a **Profile Room**, whose name, topic and avatar (and banner, MSC4221) are the ID's Nostr profile. Change them and the profile on Nostr follows.
- a NIP-05 handle that Nostr apps can look up.

You're invited to the Profile Room, your space and your notifications room. Your first ID becomes your active one.

If your Matrix username can't be a Nostr ID (Nostr IDs can only contain lowercase letters, digits, dashes, underscores and dots), or someone else already has that name, the bot says why and offers a close alternative (`q+test` → `q-test`, or `bob` → `bob-2`). Reply **confirm** to that message to take it, or choose any name with `[create profile <id>`.

Run it again for one you already have, and you're re-invited to its Profile Room and your other bridge rooms. That's the way back in if you left one.

To make an ID from a key you already have, use the [widget](#widget) instead.

### `[create profile <id>`

The same, with a name you choose: `[create profile qart` creates `qart@<bridge.domain>`. The name must:

- use only lowercase letters, digits, dashes, underscores and dots, starting and ending with a letter or digit,
- be between `bridge.id_min_length` and `bridge.id_max_length` characters (1 to 32 by default),
- not be reserved (`admin`, `support`, `nostr`, ... and `bridge.reserved_ids`),
- not be taken by another ID, or retired from someone else's,
- not be another user's Matrix username: `bob` is kept for `@bob`.

Each user can have up to `bridge.max_ids_per_user` IDs (5 by default; bridge admins aren't limited). An ID after your first isn't made Active; use `[id` for that. Its Profile Room is named after the ID rather than after you, since a second identity is usually meant to look different.

### `[id [<number|id>]`

Lists your Nostr identities, numbered in the order you created them, with the Active one marked:

```
Your Nostr identities:
1. q@example.org (active)
2. qbot@example.org
Switch with "[id <number|id>".
```

`[id <number|id>` makes one of them Active: `[id 2`, `[id qbot` or `[id qbot@example.org`. `[id list` is the same as `[id`. The numbers are also what `[rename`, `[link`, `[unlink` and `[create room` take. (`[profiles` and `[switch` are older names for these.)

### `[create room <number or id>`

Makes a new Profile Room for an ID that has none (after `[unlink`), starting from the ID's current profile, and invites you to it.

## General

Following, messaging, and everyday commands. These act as your Active identity, or as the room's identity inside one of your Profile Rooms or DM rooms.

### `[help [section]`

Without a section, lists the sections with a line about each. `[help <section>` shows that section's commands, as a table: `[help profile`, `[help general`, `[help advanced`, `[help admin` (bridge admins only) or `[help danger`. `[help all` shows every section but Admin.

### `[follow <account>`

Follows a Nostr account: `[follow npub1…`, `[follow alice@example.com`. The bot answers with a link to the account's room, where its posts arrive, and invites you to it. Its latest posts (`nostr.backfill_on_follow`, 20 by default) are brought in. The ID's follow list is published with the account in it.

An ID on this server can't be followed this way: join its Profile Room instead.

### `[unfollow <account>`

Stops following the account, and publishes the ID's follow list without it. Leaving the account's room does the same, for each of your IDs that followed it.

### `[following`

Lists the accounts the ID follows, with links to their rooms.

### `[dm <account>`

Opens a DM room with the account, as the ID, and invites you to it. Run it again to be re-invited. Messages there are sent as NIP-17 private DMs. An ID on this server can't be DMed this way: message its owner in Matrix.


### `[join <invite link | group>`

Joins a Concord community (the encrypted, Discord-style communities of Armada, Vector and other apps) from its invite link (`https://armada.buzz/invite/naddr1…#…`, or any other site's), as your Active identity. The bridge makes a Space for the community and a room for each channel you can read, invites you, and brings in each channel's recent messages. What you say in those rooms goes to the community, encrypted for it.

Concord communities are end-to-end encrypted, but the bridge can't encrypt Matrix rooms, so their messages are stored readable on this homeserver. The bot says so when you join. Identities that sign through a remote signer (NIP-46) can't join yet.

It also joins NIP-29 groups (the relay-hosted groups of Flotilla, Chachi, 0xchat and others), by their `naddr1…` link (with `?invite=<code>` for a closed group) or as `relay.example'groupid`. The bridge sends the relay your join request, makes a room for the group in a Space for its relay, and brings in its recent messages. What you say there goes to the group as you.

### `[play [<link>]`

Send it as a reply to a game post (a webxdc mini app, marked 🎮) in any room, or give a link to a game post (on ditto.pub, say), or its `nevent`/`note`/`naddr`: the post is brought in as `[import` would, then opened. The game opens in your Games room, a room the bridge makes for you the first time and invites you back to if you left. The game is the room's widget, and the next `[play` replaces it. Games need a Matrix client with widgets, such as Element Web or Desktop. Moves are posted as your Active identity. Only when `webxdc.enabled` is on.

The first time a game is opened, it gets a card in the Games room (its cartridge picture, name and description), with the bot's 🎮 on it. Toggle your own 🎮 on the card to load that game again, without a new message.

### `[import <link|nevent|note|naddr>`

Brings one Nostr post into Matrix without following its author, like matrix-appservice-activitypub's `;import <url>`. Give a link to the post on any Nostr website (`https://njump.me/nevent1…`, `https://ditto.pub/naddr1…`), or the `nevent`, `note` or `naddr` itself (`nostr:` in front works too). An `naddr` is a long-form article: its newest version is brought in.

- A reply is threaded into its conversation: its ancestors are fetched up the thread (up to 10) until one is already here, or the root is reached and goes into its author's room. `[backfill` in that room brings in more.
- Anything else goes into its author's room, which is made if needed.
- A post that's already here isn't brought in twice.

You're invited to the room, and the bot replies with a link to the post. `[import key` is still about keys.

## Advanced/Maintenance

Keys, moderating your own feed, and other things almost nobody needs day to day. Keys never go through chat messages: a homeserver stores those unencrypted. Importing and exporting is done in the widget. A message containing an `nsec` is redacted by the bot as soon as it sees it.

### `[backfill [N]`

In a Nostr account's room: brings in its latest `N` posts (20 by default; at most 100, except for bridge admins). It runs in the background, and the bot says how many posts it found.

Inside a thread whose root is a Nostr post (send it in the thread, or as a reply to the post in clients without threads), it brings in that conversation's replies instead, like matrix-appservice-activitypub's `;backfill`: replies and comments (NIP-10 and NIP-22) from the root author's relays and the fallback relays, oldest first, each threaded under the reply it answers. This works in any room, your Profile Room included. Replies already here are skipped.

### `[block <account>`

Blocks an account for the ID: its replies, mentions, reactions, reposts and DMs stop reaching you. The account is added to the ID's public mute list (kind 10000).

### `[unblock <account>`

Unblocks it.

### `[widget`

Adds the bridge's widget to the room. The bot needs permission to change the room's settings. The widget lists your IDs and does what the commands do, plus importing and exporting keys and connecting a remote signer.

### `[import key`

Explains how to bring a key you already have (`nsec`, `ncryptsec`, or a `bunker://` remote signer): in the widget, never in chat.

### `[export key`

Explains how to get an ID's key out: also only in the widget.

### `[status`

Shows:

- your Nostr IDs, numbered, with their `npub`s, and which is active,
- the handle domain, and whether the bridge's NIP-05 self-check passes (`https://<domain>/.well-known/nostr.json` reaching the bridge),
- the bridge's relay URL.

Bridge admins also see each outgoing relay connection and whether it is connected.

## Admin

Only bridge admins (`bridge.admins`, or Synapse server admins with `bridge.use_synapse_admin_api` on) can use these. Anyone else is told so.

### `[ban <npub, handle or IP> [duration] [purge] [reason]`

Bans an account (any `<account>` form) or an IP address from the relay: whatever it sends is refused.

- `duration` is `1h`, `6h`, `1d`, `7d`, `30d` or `forever`. Without one, the ban lasts `7d`.
- `purge` also redacts everything the account has in the bridge's rooms, which takes it off the relay too.
- The rest of the line is the reason. Without one, it's "banned by" and your Matrix ID.

`[ban npub1… 1d spam`, `[ban 203.0.113.7 forever`, `[ban bot@example.com 30d purge`.

### `[unban <npub, handle or IP>`

Lifts a ban, including an automatic one.

### `[bans`

Lists the current bans, manual and automatic, with when each ends and why.

## ⚠️ Danger Zone

Commands that change what a room or an identity is, in ways that are hard to undo. Each says what will happen and waits for you to reply **confirm** to its message.

### `[link <number or id> [keep]`

Makes an existing room the ID's Profile Room. Invite the bot to the room first, then send the command there. Reply **confirm** to the bot's message to go ahead.

- You must be an admin of the room (power level 100). The bot needs a power level high enough to change the room's name, topic, avatar and the bridge's own state (50 in most rooms). If it doesn't have that, nothing is linked: the bot says which level it needs, what it can't do without it, and the exact command to send again once you've raised its level. That's checked again when you confirm.
- The room can't be encrypted, a space, your notifications room, or another ID's Profile Room.
- From then on, everything you post in the room is published on Nostr as that ID. So are replies to your posts there from other local users who have Nostr IDs. Nothing already in the room is published.
- By default the room takes on the ID's current profile (name, topic, avatar, banner, website and Lightning address), so the ID's Nostr profile doesn't change. With `keep`, the room keeps its own name, topic and avatar, and they become the ID's Nostr profile. The website and Lightning address, which a room has no place to show, still come along.
- If the ID already has a Profile Room, it moves here. The old room stays, with its history, and is no longer published from.

### `[unlink <number or id>`

Stops a room being the ID's Profile Room. Sent in the Profile Room itself, the ID can be left out. The bot says what will happen. Reply **confirm** to that message to go ahead.

- The room and its history stay. Nothing said there is published any more, and posts already published stay on Nostr.
- The ID keeps its key, handle, followers and Nostr profile.
- The ID has no Profile Room until you give it one with `[link` or `[create room`. None is made for it automatically.

### `[rename <number or id> <new id>`

Gives an ID a new name and keeps its key, so its followers, posts and Profile Room all stay. The bot says what will change. Reply **confirm** to that message to go ahead.

- The new name follows the same rules as `[create profile <id>`.
- The old handle keeps working for `bridge.renamed_id_grace_days` (30 by default). After that, it stays reserved for you: nobody else can take it, and you can take it back.
- An ID can be renamed once every `bridge.rename_cooldown_days` (7 by default).

### `[delete profile [<number|id>]`

Deletes an identity for good. Nostr keys can't be revoked, so this does everything the protocol allows. The bot lists exactly what will happen, and nothing changes until you reply **confirm** to that message within 10 minutes. With more than one identity, name the one to delete: without a name, the bot lists them instead of guessing.

- Every relay the bridge knows is sent a request to vanish (NIP-62, tagged `ALL_RELAYS`), asking it to delete everything the identity ever posted. Relays that don't support NIP-62 may keep copies, and people who saved posts still have them.
- A last profile marked `"deleted": true` and an empty follow list are published, so apps show the account as gone.
- The bridge destroys its copy of the key. If you brought the key yourself (or use a remote signer), the key still exists wherever you keep it.
- The handle stops answering, and is never given to anyone else.
- Its followers, follows, blocks and DM rooms are forgotten by the bridge, and if it was your Active identity, your next one becomes Active.
- Its Profile Room stays, with its history, but nothing said there is published any more.

## IDs, and which one is used

When you do something on Nostr, the bridge uses:

1. **Inside one of your Profile Rooms:** that room's ID, always.
2. **Inside a DM room:** the ID that conversation belongs to.
3. **Anywhere else:** your active ID.

Undoing something (removing a reaction, say) always uses the ID that did it in the first place.
