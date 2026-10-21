# Features

How each thing is bridged. For the commands, see [COMMANDS.md](COMMANDS.md); for the settings, [CONFIG.md](CONFIG.md); for how it's built, [DESIGN.md](DESIGN.md).

## Contents

- [Nostr IDs](#nostr-ids)
- [Your Profile Room](#your-profile-room)
- [Sharing a room with other bridges](#sharing-a-room-with-other-bridges)
- [Publishing, and when it doesn't work](#publishing-and-when-it-doesnt-work)
- [Handles](#handles)
- [Following Nostr accounts](#following-nostr-accounts)
- [What comes in from Nostr](#what-comes-in-from-nostr)
- [Acting on Nostr posts from Matrix](#acting-on-nostr-posts-from-matrix)
- [Direct messages](#direct-messages)
- [Polls](#polls)
- [Notifications](#notifications)
- [Blocking](#blocking)
- [Keys: bringing your own, taking it with you](#keys-bringing-your-own-taking-it-with-you)
- [The widget](#the-widget)
- [The bridge's relay](#the-bridges-relay)
- [Media server](#media-server)
- [Spam protection](#spam-protection)
- [Moderation for admins](#moderation-for-admins)
- [Not supported](#not-supported)

## Nostr IDs

Each Matrix user can hold several Nostr IDs (`[create profile`, `[profiles`, `[switch`, `[rename`). An ID's Profile Room can be moved to an existing room with `[link`, unlinked with `[unlink`, or replaced with a fresh one with `[create room`. Each ID has its own key, its own NIP-05 handle (`name@<bridge.domain>`), and its own **Profile Room**. One of them is your **active** ID: it's the one that follows, reacts and replies when you're not inside a room that belongs to a particular ID (see [COMMANDS.md](COMMANDS.md#ids-and-which-one-is-used)).

Each ID also publishes, and keeps up to date:

- its **profile** (kind 0), from the Profile Room,
- its **relay list** (kind 10002): the bridge's relay for reading and writing, plus `nostr.publish_relays` for writing,
- its **DM relay list** (kind 10050): the bridge's relay,
- its **media server list** (kind 10063): the bridge,
- its **pinned posts** (kind 10001), from the Profile Room's pins,
- its **follow list** (kind 3) and **mute list** (kind 10000), from `[follow` and `[block`.

## Your Profile Room

**Its name, topic, avatar and banner are the ID's Nostr profile.** Change any of them and a new profile (kind 0) is published. The avatar and banner are cleaned first: rotation applied, EXIF metadata (which often holds GPS coordinates) removed, and re-encoded if needed. The cleaned copy is stored back in Matrix and served at `https://<relay host>/<sha256>.<ext>`. An ID made from an imported key keeps the website and Lightning address its old profile had.

**Pinning** one of the ID's own posts in the room (Matrix's pinned messages) publishes it as a pinned note (NIP-51). Pins of anything else are left out.

**What you post there is published on Nostr**, as that room's ID, whichever ID is active:

- **Text and emotes** become notes (kind 1). Nostr has no length limit, so a long message stays one note. An emote (`/me waves`) is published as `*waves*`.
- **Images** become notes with the image's URL and an `imeta` tag (type, hash, size, dimensions). They're cleaned the same way as avatars, and are at most 5 MB after cleaning.
- **Video, audio and other files** are published as they are, up to `nostr.max_media_mb` (50 MB by default). Their metadata isn't stripped, so a video can still carry its location.
- **Captions** become the note's text, with the file's URL on the next line.
- **Mentions:** a pill of a local user who has a Nostr ID becomes a `nostr:npub…` reference and a `p` tag, pointing at the ID that user had active at the time. A pill of a Nostr account's ghost does the same for that account.
- **Quotes:** a post that links to a Nostr post (a `nostr:note…`/`nevent…` link, a link on any Nostr website, or a matrix.to link to a bridged post) quotes it (`q` tag), tagging its author.
- **Spoilers** add a `content-warning` tag, with the spoiler's reason if it has one.
- **Polls** become Nostr polls. See [Polls](#polls).
- **Replies and threads:** a rich reply or thread message answering a Nostr post becomes a reply to it, so threads stay threads on Nostr. A reply to anything else is an ordinary post.
- **Redacting** a published message publishes a deletion (kind 5) for it, with the redaction's reason as the deletion's text.

**Not published:**

- edits (Nostr notes can't be edited)
- commands, and anything else addressed to the bot
- notices
- empty messages
- encrypted files (the bridge can't read encrypted rooms)
- files over the size limit (the bot tells you why)

**Other people in your Profile Room:** a local user who replies to one of your posts publishes that reply from *their own* active ID, as a reply to your post. Anything else they say there is conversation and stays in Matrix.

**Posts from your other Nostr apps:** if you brought your own key, what you post from another app as that ID is shown in its Profile Room too, sent by the bot under your ID's name (MSC4144), and your deletions are applied.

## Sharing a room with other bridges

One room can be your Profile Room on several networks at once: link it with this bridge's `[link` and the sibling bridges' own link commands (matrix-appservice-activitypub, matrix-appservice-atproto). Its name, topic and avatar are then your profile on each network, and what you post there goes to each.

- **Commands:** give each bridge a different `command_prefix` (the defaults already differ: `[` Nostr, `;` ActivityPub, `]` Bluesky). List the other bridges' prefixes in `appservice.other_command_prefixes`, and their bots in `appservice.other_bot_users`, so a command for one bridge is never posted by another. Do the same in each sibling bridge's config.
- **Replies:** a reply to something that isn't on Nostr and isn't your own message, such as a reply mirrored from Bluesky or another bridge's "reply confirm" question, stays in Matrix. Only replies to Nostr posts, and to your own messages, are published.
- **Other bridges' users:** list their ghosts in `appservice.other_bridge_users` and their bots in `other_bot_users`. Nothing they say is acted on: a Bluesky reply mirrored into the room that happens to start with `[create profile` doesn't make the ghost a Nostr ID. Changes they make to the room's name or avatar still update your Nostr profile.
- **Power:** each bridge's bot needs enough power to set the room's profile state (50 in most rooms). The MSC4501 "whose profile this is" state is shared: once one bridge has set it, the others don't need the power to set it again, and unlinking one bridge leaves it in place for the rest.
- **Reposts:** reposting a fediverse or Bluesky post (through that bridge, which posts a "🔁 q reposted …" card in the room, or from an MSC4501 client like Haven) publishes a Nostr note too. Nostr reposts can only point at Nostr events, so the note credits the original author (`🔁 JoeBravo77:`), then has the original's text and media, and ends with its own link, which Nostr apps show as a preview card. A repost with your own words puts them first. Undoing the repost deletes the note. Reposting a post that's already on Nostr from Haven is left to its 🔁 reaction (a real Nostr repost); with a caption it becomes a quote.
- **Linking:** `[link` says which other bridges are already in the room. Without `keep`, the room takes on the Nostr ID's profile, which changes the profile on those networks too; `[link <id> keep` uses the room's own.

## Publishing, and when it doesn't work

A post is published once the bridge has it: its own relay (`nostr.relay_url`) serves it to anyone who looks your ID up. Copies also go to `nostr.publish_relays`, for reach, and replies, reactions and reposts also go to the inbox relays of the account they're aimed at:

- **No public relay took it:** the bot replies to your message saying so. The bridge keeps retrying (after 1 minute, 5 minutes, 30 minutes, then every 6 hours), and the retries survive a restart.
- **A relay refuses an ID by policy** (`blocked:` or `restricted:`, like a web-of-trust relay that doesn't know the ID yet): you're told once. That relay is skipped for that ID for `nostr.refused_relay_retry_hours` (24 by default), rather than being asked again on every post.
- **The key is with a remote signer that doesn't answer:** the bot replies saying the post wasn't published, and why.

The bridge stores no post content of its own. When a post has to be sent again, it is rebuilt from the Matrix message, and must hash to the same Nostr event ID.

## Handles

Every ID's handle is answered at `https://<bridge.domain>/.well-known/nostr.json?name=<name>`, along with the bridge's relay as the place to find its events. Only the name asked for is answered, never the full list. After a rename, the old handle keeps answering for `bridge.renamed_id_grace_days`.

The bridge itself is `_@<bridge.domain>`, shown by Nostr apps as just the domain.

## Following Nostr accounts

**`[follow` takes an account however you paste it:** `npub1…`, `nprofile1…`, a hex key, a handle (`name@domain`, or a bare `domain` for `_@domain`), or a profile link from a Nostr website. It follows as your active ID, and publishes that ID's new follow list.

**Each followed account gets one Matrix room**, its **Remote User Room**, shared by everyone here who follows it, and a ghost user (`@nostr_<hex key>`) who posts in it:

- The room's name, topic and avatar, and the ghost's display name and avatar, follow the account's profile.
- The account's NIP-05 handle is only shown once checked: the bridge fetches it and makes sure it points back to the same key.
- With `bridge.set_msc4501_profile_room`, the ghost's Matrix profile points at its room (MSC4501). With `bridge.msc4503_external_handle`, its handle (or `npub`) is recorded on its profile or its events (MSC4503). Both need MSC4133 profile fields on the homeserver, and are skipped quietly where it doesn't have them.
- You're invited to the room, and it's filed in your Nostr space. Other local users can knock and are let straight in (and users of other homeservers too, with `bridge.accept_federated_knocks`).
- **Backfill:** on following, the account's latest `nostr.backfill_on_follow` posts (20 by default) are brought in. `[backfill [N]` in its room brings in more.
- **Leaving the room unfollows** the account from each of your IDs that followed it.

New posts arrive through subscriptions on the relays the account says it writes to (its NIP-65 relay list, or `nostr.fallback_relays` if it has none). The bridge remembers how far it has read on each relay, so a restart catches up rather than starting over.

## What comes in from Nostr

**A followed account's posts** appear in its room, from its ghost, with the original's web link (`external_url`, on `nostr.web_viewer_url`, `https://ditto.pub/` by default):

- **Notes, pictures and videos** (kinds 1, 20, 21, 22). The first image or video becomes the Matrix message, copied into the media repository, with the text as its caption. Further attachments are listed as links.
- **Text:** URLs are linked; `nostr:npub…` becomes a pill when it's someone here, else a link; `nostr:note…`/`nevent…` becomes a link to the post in Matrix if it's here, else on the web viewer; custom `:emoji:` (NIP-30) are shown as inline images; a `content-warning` becomes a spoiler. Markdown isn't rendered, since Nostr notes are plain text.
- **Long-form articles** (kind 30023) are shown as 📰 the title, the summary (or the start of the text), the cover image, and a link to read it, rather than pages of markdown.
- **Replies** by a followed account are threaded under the post they answer, if that post is in Matrix (it's brought in first, if need be), with a copy in the replier's own room. Otherwise they're shown in its room as "⤵️ Reply to a post".
- **Reposts** (kinds 6 and 16) are shown as "🔁 reposted X's post", followed by the post.
- **Quotes** get a footer, "🔁 reposted X's post", with a preview of the quoted post. Whether a quoted post that isn't in Matrix yet is brought in follows `bridge.quote_import_policy`.
- **Polls** (NIP-88) become Matrix polls.
- **Deletions** (kind 5) redact the Matrix event, if it was the author's own.
- **Profile changes** (kind 0) update the ghost and the room.

**What's aimed at your IDs** comes in whether you follow its author or not, from the bridge's own relay and from the public relays it watches for anything tagging a local ID:

- **Replies** to your posts, including NIP-22 comments (kind 1111), are threaded under the post in your Profile Room, from the replier's ghost.
- **Reactions** (kind 7) to your posts become Matrix reactions from the reacter's ghost: `+` as 👍, custom emoji as images (MSC4027). Dislikes (`-`) aren't shown.
- **Reposts** of your posts become a 🔁 reaction on the post.
- **Mentions and quotes** in posts that have no other home here are shown in your notifications room, which then holds the post.
- **Zaps** (zap receipts) and **new followers** become notifications. A zap receipt only counts if it is signed by the Lightning provider of the ID's `lud16` address (its LNURL `nostrPubkey`), so an ID without a Lightning address gets no zap notifications.
- **DMs**: see [Direct messages](#direct-messages).

Everything that comes in is checked first: see [Spam protection](#spam-protection).

## Acting on Nostr posts from Matrix

In any room a Nostr post appears in (an account's room, your Profile Room, a notification):

- **React** to like it. 👍 (any skin tone) is sent as `+`; any other emoji as itself; a custom image reaction (MSC4027) as a custom emoji with its image.
- **React with 🔁** to repost it (kind 6, or kind 16 for anything but a note).
- **Reply** (rich reply or thread) to reply on Nostr: a reply (kind 1) to a note, a comment (kind 1111) to anything else.
- **Quote** it by linking to it from a post in your Profile Room.
- **Redact** your reaction, repost or reply to undo it: a deletion is published, signed by whichever ID did it.

Outside your Profile Rooms and DM rooms, these are done as your active ID.

## Direct messages

**`[dm <account>`** opens a DM room with a Nostr account, as your active ID: one room per (ID, account), with the bot, you and the account's ghost.

- **Sending:** your messages go out as NIP-17 private DMs (NIP-44 encrypted, gift wrapped), to the account's DM relays (kind 10050), with a copy for your own Nostr apps. Files are sent encrypted (kind 15), up to `nostr.max_media_mb`. Edits can't be sent.
- **Receiving:** NIP-17 DMs, and old-style NIP-04 DMs, arrive in the DM room from the ghost. A DM from someone new arrives as an invite to a new room, which you can just decline.
- On the bridge's relay, DMs are readable only by their recipient, after logging in (NIP-42).
- DMs between two IDs on this server aren't bridged: message each other in Matrix.
- DM rooms aren't flagged as Matrix direct chats unless `bridge.dm_rooms_as_direct` is on.

## Polls

- **A poll you start in your Profile Room** becomes a Nostr poll (NIP-88, kind 1068): the question, each answer as an option, single or multiple choice. Its votes are sent to the bridge's relay, so they come back as Matrix votes.
- **Voting** on any bridged poll from Matrix sends a vote (kind 1018) as your acting ID.
- **A followed account's poll** appears in its room as a Matrix poll from its ghost, and Nostr votes on any poll that's in Matrix appear as votes from the voters' ghosts.
- The results are Matrix's own tally of those votes.

## Notifications

Each Matrix user has one **notifications room**, a private room with the bot, filed in their Nostr space. It's kept apart from Profile Rooms, so other people in a Profile Room can't see who is interacting with its owner. With several IDs, each notification names the ID it's for.

A notification leads with an emoji for its type (👤 follow, 👍 like, 🔁 repost, 💬 reply, 💬 mention, 🔁 quote, ⚡ zap; a reaction leads with the reaction itself), then a pill of who did it, then what they did, with "your post" linking to the post in Matrix. Under that is a quote: their words for a reply, mention or quote; your post's for a like, reaction or repost. If the quoted post has an image or video, the notification shows it at preview size.

A local user replying to or reacting to another local user's post notifies them the same way.

## Blocking

**`[block <account>`** blocks an account for your active ID: its replies, mentions, reactions, reposts and DMs stop reaching you, and it doesn't show as a new follower. The account is added to the ID's public mute list (kind 10000), so your other Nostr apps honour it too. `[unblock` undoes it.

## Keys: bringing your own, taking it with you

**Keys never go through chat.** A room message is stored by the homeserver unencrypted. Importing and exporting keys is done in the [widget](#the-widget), over HTTPS. If someone pastes an `nsec` into a room the bot can see, the bot redacts the message straight away (where it has the power to), never publishes it, and says the key should be considered exposed.

Every key the bridge holds is encrypted at rest with `nostr.key_encryption_secret`.

**Bringing a key you already have**, as a new ID or in place of an existing ID's key:

- an `nsec`, or a password-protected `ncryptsec` (NIP-49),
- or a **remote signer** (NIP-46, `bunker://`): the key stays in your signer app, and the bridge asks it to sign each event. If the signer asks you to approve the connection on a web page, the widget shows the link. Old-style NIP-04 DMs can't be read for such an ID.

A new ID made from an existing key starts its Profile Room from the key's existing profile. Optionally, your existing follows and up to 100 recent posts are brought in too.

Replacing an existing ID's key keeps its name and Profile Room, but **not its followers**: they follow the key, not the handle. The widget asks you to type the ID's name to confirm.

**Exporting** gives an ID's key back as an `nsec`, or as an `ncryptsec` if you give a password. Key imports and exports are limited to 10 tries per 10 minutes per user.

## The widget

The bridge has a room widget, a small web page that widget-capable clients (Element, ...) show beside a room. It's added to every room the bridge creates, and `[widget` adds it to any other room.

It does what the commands do (creating and switching IDs, following, unfollowing, blocking, DMs, backfill), plus the key handling that must never go through chat. The page proves who you are with a Matrix OpenID token from your client, checked with your homeserver, and only works in rooms you're in.

## The bridge's relay

The bridge is a Nostr relay at `nostr.relay_url` (NIP-01, NIP-11 information document, NIP-42 login, `COUNT`). It's your IDs' home relay: their handles point there, and it's where the rest of the network finds their posts.

**It accepts** events that have a home in Matrix: anything signed by a local ID, and replies, comments, mentions, quotes, reactions, reposts, zap receipts and DMs aimed at a local ID, after the [spam checks](#spam-protection). Deletions of events it holds are applied. Follow lists that add or drop a local ID are noted (a "followed you" notification) but not stored. Anything else is refused with `restricted:`. Protected events (NIP-70) are only taken from their author, logged in.

**It serves**, rebuilding each event from Matrix on request:

- **public:** local IDs' events, and the replies, reactions and mentions aimed at them, to anyone,
- **DMs:** only to a connection logged in as their recipient,
- **followed accounts' posts:** only with `nostr.serve_followed_to_local_clients` on, and only to a connection logged in as a local ID that follows the account. That lets your own Nostr apps read your timeline through the bridge. It's off by default, since every such read is fetched from the homeserver.

**Hashtag and full-text search aren't supported.** A filter only `#t` could narrow, or any NIP-50 `search`, is closed with `CLOSED` and `error: hashtag and full-text search aren't supported by this relay`, so clients try another relay. A `#t` alongside something else (an author, say) works.

## Media server

Published media is served at `https://<relay host>/<sha256>.<ext>`, the Blossom (BUD-01) shape, so Blossom-aware clients can find files by hash. The files stay in the Matrix media repository; only the ones the bridge has published can be fetched, so it isn't an open proxy to your media.

Your own Nostr apps can also upload media through the bridge (BUD-02, `PUT /upload`), since each ID's media server list names it. The upload must be authorized by a kind 24242 event signed by a local ID's key, and is at most `nostr.max_media_mb`. The file is stored in the Matrix media repository like everything else.

## Spam protection

Everything strangers send the bridge, through its relay or through the relays it watches, goes through the spam guard before the bridge does anything with it. The layers, cheapest first:

- **Bans** of keys and IP addresses, kept across restarts.
- **Content checks:** event size, timestamps (not from the future, and replies and reactions not days old), mass mentions ("hellthreads"), media embedded as base64, the operator's own patterns, and the same text arriving from several accounts at once.
- **Rate limits** per IP address, per key, and per key and local ID. Accounts someone here follows get larger allowances.
- **Trust tiers:** local IDs; accounts someone here follows; the accounts those follow (the **web of trust**, refreshed every few hours); and strangers. Depending on `spam.inbound_policy`, strangers are let in (`open`), must show proof of work (`pow`, NIP-13), or are refused (`web_of_trust`).
- **Strikes:** every refusal by the content checks or rate limits is a strike against the key and IP. Enough strikes within a window ban them automatically, for longer each time. A reply redacted from a Profile Room counts as a strike against its author.
- **Shields up:** many never-seen accounts in a few minutes looks like a bot wave. The relay then tightens to `spam.wave_policy` until things have been quiet for an hour (by default), and bridge admins are told in their notifications rooms when the shields go up and down.
- **Shared block lists:** the relay can honour the public mute lists (kind 10000) and reports (kind 1984) of accounts you trust (`nostr.shared_block_lists`). Anyone on them is refused, unless someone here follows them.

A refusal reaches the sender as a standard `OK false` with a `blocked:`, `rate-limited:`, `pow:`, `restricted:` or `invalid:` reason. Every limit is a setting: see [CONFIG.md](CONFIG.md#spam).

## Moderation for admins

Bridge admins are listed in `bridge.admins` (or, optionally, are Synapse server admins: `bridge.use_synapse_admin_api`).

- **From Matrix:** `[ban` a key, handle or IP address (for a time or for good, optionally removing everything they have here), `[unban`, and `[bans`.
- **From Nostr tools:** the relay speaks NIP-86 relay management (`POST` to the relay's address with `Content-Type: application/nostr+json+rpc`). Each request needs a NIP-98 authorization (kind 27235) signed by an ID owned by a bridge admin. Methods: `supportedmethods`, `banpubkey`, `unbanpubkey`, `listbannedpubkeys`, `blockip`, `unblockip`, `listblockedips`, `banevent` (removes the event from Matrix and the relay), `listbannedevents`. Bans made this way are permanent until lifted.
- Redacting a stranger's event in Matrix takes it off the relay too.

## Not supported

- **Edits**, either way: Nostr notes can't be edited.
- **Hashtag and full-text search** on the bridge's relay.
- **Sending zaps.** Zaps you receive are shown.
- **Encrypted Matrix rooms:** the bridge doesn't do end-to-end encryption.
- **Webxdc games and apps** (NIP-DC): planned as an optional last phase.
