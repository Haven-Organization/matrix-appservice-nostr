"""One-off backfill: upgrade existing rooms + ghosts to the backstage space.

``bridge.backstage_space_enabled`` only changes behavior for a NEW ghost
room-access, a room reached, or a ghost seen, before the flag was flipped
on keeps working exactly as it always did until something gives it a
reason to change (see ``bridge.backstage_space.ensure_ghost_access``'s own
lazy, on-demand upgrade, wired into the bridge's one ghost-invite choke
point, ``bridge.remote.ensure_ghost_in_room``). That self-converts
gradually on its own, but this script does the same work up front in one
pass, for every room a ghost has ever been added to:

- Remote User Rooms (``bridge.remote.ensure_room``, discovered via
  ``repository.list_remote_accounts()``'s ``room_id`` field).
- NIP-29 group rooms, Concord channel rooms, and Git repository rooms,
  discovered via the generic key-value store (``repository.list_state``)
  using the exact same key prefixes those modules use internally
  (``nip29:room:``, ``concord:room:``, ``git:room:``).

DM rooms, the per-user Notifications room, and the Games room are never
reached via ``ensure_ghost_in_room`` at all, so they're out of scope here
too, same as everywhere else in ``bridge.backstage_space``.

Run natively, no containerization:

    python adhoc_backfill_backstage_space.py [--public-join] [path/to/config.yaml]

Requires ``bridge.backstage_space_enabled: true`` already set, refuses to
run otherwise, since without it ``maybe_upgrade_join_rule`` would silently
no-op on every room.

``--public-join`` trades a brief window of exposure for half the member
events: instead of joining every ghost to the backstage space one at a
time (an invite+join pair, or a single Admin API force-join when
``bridge.use_synapse_admin_api`` is on), it flips the space's own
``join_rule`` to ``public`` just long enough for every ghost to self-join
with one plain ``join`` event each, then locks the space back down to
whatever it was before. During that window, anyone on this (or a
federating) homeserver who learns the space's room ID could join it too,
the same consequence as being a backstage-space member generally (direct
join access to every room that trusts it). The window only spans the
ghost-joining phase, not the whole room sweep, and is always closed again
even if the run is interrupted partway through.
"""

from __future__ import annotations

import asyncio
import sys
import types

from bridge.backstage_space import (
    close_backstage_space,
    ensure_backstage_space,
    join_ghost_to_backstage_space,
    maybe_upgrade_join_rule,
    open_backstage_space_publicly,
)
from bridge.config import ConfigError, load_config
from bridge.homeserver_client import HomeserverClient, HomeserverError
from bridge.storage import open_repository
from bridge.users import bot_mxid

_ROOM_STATE_PREFIXES = {
    "NIP-29 group": "nip29:room:",
    "Concord channel": "concord:room:",
    "Git repository": "git:room:",
}


async def _in_scope_room_ids(repository) -> dict[str, str]:
    """Every room this feature applies to, as ``{room_id: label}``."""
    rooms: dict[str, str] = {}
    for remote in await repository.list_remote_accounts():
        if remote.room_id:
            rooms[remote.room_id] = f"Remote User Room ({remote.pubkey[:12]}...)"
    for label, prefix in _ROOM_STATE_PREFIXES.items():
        for key, value in await repository.list_state(prefix):
            room_id = key[len(prefix):]
            rooms[room_id] = f"{label} ({value})"
    return rooms


async def _ghosts_by_room(
    homeserver: HomeserverClient, config, bot_mxid_: str, rooms: dict[str, str]
) -> tuple[dict[str, list[str]], set[str]]:
    """``{room_id: [ghost_mxid, ...]}`` for every room whose members are
    readable, plus the flat set of every distinct ghost seen, gathered
    once up front so ``--public-join``'s exposure window can be scoped to
    exactly the ghost-joining phase, not the slower per-room upgrade sweep
    that follows it."""
    ghosts_by_room: dict[str, list[str]] = {}
    all_ghosts: set[str] = set()
    for room_id in rooms:
        try:
            members = await homeserver.get_joined_members(room_id, as_user_id=bot_mxid_)
        except HomeserverError as exc:
            print(f"  SKIP  {room_id} ({rooms[room_id]}): could not list members: {exc}")
            continue
        ghosts = [m for m in members if m.startswith(f"@{config.appservice.user_prefix}")]
        ghosts_by_room[room_id] = ghosts
        all_ghosts.update(ghosts)
    return ghosts_by_room, all_ghosts


async def run(config_path: str | None, *, public_join: bool = False) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if not config.bridge.backstage_space_enabled:
        print(
            "bridge.backstage_space_enabled is off, enable it in config.yaml and restart the bridge, "
            "then re-run this script.",
            file=sys.stderr,
        )
        return 1

    repository = open_repository(config)
    homeserver = HomeserverClient(config.homeserver.base_url, as_token=config.appservice.as_token)
    app = types.SimpleNamespace(state=types.SimpleNamespace(config=config, repository=repository, homeserver=homeserver))
    bot = bot_mxid(config)

    space_room_id = await ensure_backstage_space(app)
    if space_room_id is None:
        print("Could not create/find the backstage space, aborting.", file=sys.stderr)
        return 1
    print(f"Backstage space: {space_room_id}")

    rooms = await _in_scope_room_ids(repository)
    print(f"Found {len(rooms)} in-scope room(s).")

    ghosts_by_room, all_ghosts = await _ghosts_by_room(homeserver, config, bot, rooms)
    readable_rooms = set(ghosts_by_room)
    print(f"Found {len(all_ghosts)} distinct ghost(s) across {len(readable_rooms)} readable room(s).")

    joined_ghosts: set[str] = set()
    if public_join:
        previous_join_rule = await open_backstage_space_publicly(app, space_room_id)
        print(
            f"Backstage space temporarily set to public (was {previous_join_rule!r}), "
            f"self-joining {len(all_ghosts)} ghost(s) with one event each..."
        )
        try:
            for ghost_mxid in all_ghosts:
                try:
                    await homeserver.join_room(space_room_id, as_user_id=ghost_mxid)
                    joined_ghosts.add(ghost_mxid)
                except HomeserverError as exc:
                    print(f"    WARN: {ghost_mxid} could not self-join the backstage space: {exc}")
        finally:
            await close_backstage_space(app, space_room_id, previous_join_rule)
            print(f"Backstage space locked back down to {previous_join_rule!r}.")
    else:
        for ghost_mxid in all_ghosts:
            await join_ghost_to_backstage_space(app, ghost_mxid)
            joined_ghosts.add(ghost_mxid)

    upgraded, skipped = 0, len(rooms) - len(readable_rooms)
    for room_id in readable_rooms:
        label = rooms[room_id]
        if await maybe_upgrade_join_rule(app, room_id):
            print(f"  OK    {room_id} ({label}): restricted to the backstage space.")
            upgraded += 1
        else:
            print(f"  SKIP  {room_id} ({label}): no permission, already public, or unrecognized join_rules.")
            skipped += 1

    print(
        f"\nDone. {upgraded} room(s) upgraded, {skipped} skipped, "
        f"{len(joined_ghosts)} distinct ghost(s) joined to the backstage space."
    )
    await homeserver.aclose()
    return 0


def main(argv: list[str]) -> int:
    public_join = "--public-join" in argv
    positional = [a for a in argv if not a.startswith("--")]
    config_path = positional[0] if positional else None
    return asyncio.run(run(config_path, public_join=public_join))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
