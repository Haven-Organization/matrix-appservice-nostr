"""Sending the bridge's own events to public relays, and telling people
when that doesn't work. See DESIGN.md, "Telling the user whether it
worked".

An event is *published* as soon as it is in the bridge's index: from then
on the bridge's own relay serves it to anyone who looks the ID up. Copies
to ``nostr.publish_relays`` are for reach, and are sent in the background:

- A relay that accepts is done with.
- A relay that refuses for a passing reason (``rate-limited:``, ``error:``,
  unreachable) is tried again later. What to retry is kept in
  ``pending_publishes`` (event ids and relay URLs only: the event itself is
  rebuilt from the index each time), so retries survive a restart.
- A relay that refuses by policy (``blocked:``, ``restricted:``, a
  web-of-trust relay that doesn't know the ID yet) isn't tried again for
  that ID for ``nostr.refused_relay_retry_hours``. The user is told once
  per relay, not on every post.
- If no relay at all takes the event on the first try, the user is told,
  and retries carry on.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

from bridge.event_sources import rebuild
from bridge.messages import notice
from bridge.nostr.event import Event, parse
from bridge.nostr.relay_pool import PublishResult, normalize_relay_url
from bridge.repository import PendingPublish, RelayRefusal
from bridge.tasks import run_in_background

logger = logging.getLogger(__name__)

# Delay before retry n (in seconds): 1 min, 5 min, 30 min, then every 6 h.
_RETRY_DELAYS = (60, 300, 1800, 6 * 3600)
_MAX_ATTEMPTS = 12
RETRY_LOOP_INTERVAL = 30.0

_POLICY_PREFIXES = ("blocked:", "restricted:", "pow:", "mute:")


@dataclass(frozen=True)
class NotifyTarget:
    """Where to tell the user about trouble: a reply to the message the
    event was made from."""

    room_id: str
    event_id: str
    label: str  # "this post", "your profile", ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_at(attempts: int) -> str:
    delay = _RETRY_DELAYS[min(attempts - 1, len(_RETRY_DELAYS) - 1)]
    return (_now() + timedelta(seconds=delay)).isoformat()


async def _relays_to_try(app: FastAPI, pubkey: str, relays: list[str]) -> list[str]:
    """``relays`` minus those that refused this ID by policy recently."""
    repository = app.state.repository
    now = _now().isoformat()
    wanted = []
    for relay in dict.fromkeys(normalize_relay_url(url) for url in relays):
        refusal = await repository.get_relay_refusal(relay, pubkey)
        if refusal is None or refusal.until <= now:
            wanted.append(relay)
    return wanted


async def _reply_notice(app: FastAPI, target: NotifyTarget, plain: str, rich: str) -> None:
    await notice(
        app,
        target.room_id,
        plain,
        html_message=rich,
        extra={"m.relates_to": {"m.in_reply_to": {"event_id": target.event_id}}},
    )


async def _record_policy_refusals(
    app: FastAPI, pubkey: str, refused: list[PublishResult], notify: NotifyTarget | None
) -> None:
    repository = app.state.repository
    hours = app.state.config.nostr.refused_relay_retry_hours
    until = (_now() + timedelta(hours=hours)).isoformat()
    newly: list[PublishResult] = []
    for result in refused:
        previous = await repository.get_relay_refusal(result.relay, pubkey)
        await repository.put_relay_refusal(RelayRefusal(result.relay, pubkey, until, result.message))
        if previous is None:
            newly.append(result)
    if newly and notify is not None:
        lines = [f"{r.relay}: {r.message}" for r in newly]
        intro = f"Some relays won't take posts from this ID yet. I'll try them again in {hours} hours:"
        await _reply_notice(
            app,
            notify,
            intro + "\n" + "\n".join(lines),
            f"<p>{html.escape(intro)}</p><ul>" + "".join(f"<li>{html.escape(line)}</li>" for line in lines) + "</ul>",
        )


async def _attempt(app: FastAPI, event: Event, relays: list[str], attempts: int, notify: NotifyTarget | None) -> None:
    """One round of sending ``event`` to ``relays``, and the bookkeeping
    that follows from the answers."""
    repository = app.state.repository
    results: list[PublishResult] = await app.state.relay_pool.publish(event, relays) if relays else []
    accepted = [r for r in results if r.accepted]
    retry = [r.relay for r in results if r.retryable]
    policy = [r for r in results if not r.accepted and r.message.startswith(_POLICY_PREFIXES)]
    other = [r for r in results if not r.accepted and not r.retryable and r not in policy]
    for result in accepted:
        logger.info("%s accepted %s", result.relay, event.id)
    for result in [*policy, *other]:
        logger.info("%s refused %s: %s", result.relay, event.id, result.message)
    for result in results:
        if result.retryable:
            logger.info("%s didn't take %s (trying again later): %s", result.relay, event.id, result.message or "no answer")
    if policy:
        await _record_policy_refusals(app, event.pubkey, policy, notify)
    if retry and attempts < _MAX_ATTEMPTS:
        await repository.put_pending_publish(PendingPublish(event.id, tuple(retry), attempts, _retry_at(attempts)))
    else:
        await repository.delete_pending_publish(event.id)
    if attempts == 1 and not accepted and notify is not None and relays:
        explanation = (
            f"Couldn't get {notify.label} onto any public Nostr relay, so for now it can only be seen by people "
            "who look this ID up directly."
        )
        follow_up = " I'll keep retrying." if retry else ""
        details = [f"{r.relay}: {r.message}" for r in results if not r.accepted]
        await _reply_notice(
            app,
            notify,
            explanation + follow_up + ("\n" + "\n".join(details) if details else ""),
            f"<p>{html.escape(explanation + follow_up)}</p>"
            + ("<ul>" + "".join(f"<li>{html.escape(d)}</li>" for d in details) + "</ul>" if details else ""),
        )


async def publish(
    app: FastAPI,
    event: Event,
    *,
    notify: NotifyTarget | None = None,
    relays: list[str] | None = None,
    extra_relays: list[str] | None = None,
) -> None:
    """Send ``event`` (already in the index) to the publish relays (or
    ``relays``), plus ``extra_relays`` (the inboxes of whoever it's aimed
    at), now. Never raises."""
    try:
        wanted = [*(relays or app.state.config.nostr.publish_relays), *(extra_relays or [])]
        targets = await _relays_to_try(app, event.pubkey, wanted)
        await _attempt(app, event, targets, 1, notify)
    except Exception:
        logger.exception("Publishing %s failed", event.id)


def publish_in_background(
    app: FastAPI,
    event: Event,
    *,
    notify: NotifyTarget | None = None,
    relays: list[str] | None = None,
    extra_relays: list[str] | None = None,
    scope: str = "public",
) -> None:
    """Hand the event to live subscribers of the bridge's own relay, and
    send it out to other relays in the background."""
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        hub.broadcast(event.to_dict(), scope)
    run_in_background(
        app, publish(app, event, notify=notify, relays=relays, extra_relays=extra_relays), name=f"publish {event.id[:8]}"
    )


async def retry_due(app: FastAPI) -> int:
    """Retry every pending publish that is due. Returns how many were tried."""
    repository = app.state.repository
    due = await repository.list_due_publishes(_now().isoformat())
    for pending in due:
        row = await repository.get_event(pending.event_id)
        if row is None:
            await repository.delete_pending_publish(pending.event_id)
            continue
        rebuilt = await rebuild(app, row)
        if rebuilt.status in ("gone", "mismatch"):
            await repository.delete_pending_publish(pending.event_id)
            continue
        if rebuilt.status != "ok" or rebuilt.event is None:
            # The homeserver couldn't be asked: try again next round.
            continue
        event = parse(rebuilt.event)
        relays = await _relays_to_try(app, event.pubkey, list(pending.relays))
        await _attempt(app, event, relays, pending.attempts + 1, None)
    return len(due)


async def retry_loop(app: FastAPI) -> None:
    while True:
        try:
            await retry_due(app)
        except Exception:
            logger.exception("Retrying pending publishes failed")
        await asyncio.sleep(RETRY_LOOP_INTERVAL)
