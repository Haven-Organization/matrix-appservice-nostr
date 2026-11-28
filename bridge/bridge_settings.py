"""Bridge-wide settings an admin can change from the widget (Admin,
Settings), as matrix-appservice-activitypub does.

Every field of ``bridge.config`` is registered here, including the ones
only ``config.yaml`` may set (``editable=False``) and secrets
(``sensitive=True``, whose values are never read or sent anywhere by this
module), so the Settings page also documents every setting.

Overrides are rows in the ``bridge_state`` table under ``setting:<key>``,
holding the value as text. ``effective_config`` folds them over the file's
config; ``app.state.config`` is that result and ``app.state.base_config``
the file's own, never changed. ``refresh`` rebuilds the effective config
after a change and re-applies what the bridge only reads once at startup
(the spam guard, the NIP-89 client tag and announcement, the bot's
profile, relay subscriptions), so every editable setting takes effect
straight away. Anything read once and not re-applied here is registered
``editable=False``.

``bridge.allow_db_setting_overrides`` switches all of this off. It is read
from the file's config only, so no stored row can turn the lock off. The
widget routes check it before calling ``apply_override`` or
``clear_override``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from bridge.config import BridgeConfig, ConfigError, _relay_url

logger = logging.getLogger(__name__)

STATE_PREFIX = "setting:"


class SettingError(Exception):
    """An unknown key, a key only config.yaml may set, or a value that
    doesn't parse. Callers answer with a 400."""


@dataclass(frozen=True)
class BridgeSettingDefinition:
    key: str  # dotted, e.g. "bridge.third_party_access": the stored key and the API id
    section: str  # attribute of BridgeConfig, e.g. "bridge", "nostr"
    field_name: str  # attribute within that section; dotted when nested ("postgres.dsn")
    label: str
    description: str  # 1-3 sentences, shown in the admin UI
    value_type: str  # "bool" | "int" | "float" | "str" | "enum" | "list_str"
    enum_choices: tuple[str, ...] | None
    editable: bool  # False: config.yaml only, still listed so it's documented
    sensitive: bool  # True: the value is never read or sent anywhere
    parse: Callable[[Any], Any]  # API JSON value -> checked native value; raises SettingError
    serialize: Callable[[Any], str]  # native value -> stored text
    deserialize: Callable[[str], Any]  # stored text -> native value


def _get_value(config: BridgeConfig, definition: BridgeSettingDefinition) -> Any:
    value: Any = getattr(config, definition.section)
    for part in definition.field_name.split("."):
        if value is None:
            return None
        value = getattr(value, part)
    return value


def _replace_path(obj: Any, path_parts: list[str], value: Any) -> Any:
    if len(path_parts) == 1:
        return dataclasses.replace(obj, **{path_parts[0]: value})
    child = getattr(obj, path_parts[0])
    return dataclasses.replace(obj, **{path_parts[0]: _replace_path(child, path_parts[1:], value)})


def _with_value(config: BridgeConfig, definition: BridgeSettingDefinition, value: Any) -> BridgeConfig:
    section_obj = getattr(config, definition.section)
    if section_obj is None:
        return config
    new_section_obj = _replace_path(section_obj, definition.field_name.split("."), value)
    return dataclasses.replace(config, **{definition.section: new_section_obj})


# -- field factories ------------------------------------------------------------------


def _bool_field(key: str, section: str, field_name: str, label: str, description: str,
                *, editable: bool = True, sensitive: bool = False) -> BridgeSettingDefinition:
    def _parse(value: Any) -> bool:
        if not isinstance(value, bool):
            raise SettingError(f'"{key}" must be true or false.')
        return value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="bool", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=lambda v: "true" if v else "false", deserialize=lambda raw: raw == "true",
    )


def _int_field(key: str, section: str, field_name: str, label: str, description: str,
               *, editable: bool = True, sensitive: bool = False, minimum: int | None = None,
               maximum: int | None = None) -> BridgeSettingDefinition:
    def _parse(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingError(f'"{key}" must be a whole number.')
        if minimum is not None and value < minimum:
            raise SettingError(f'"{key}" must be at least {minimum}.')
        if maximum is not None and value > maximum:
            raise SettingError(f'"{key}" must be at most {maximum}.')
        return value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="int", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=str, deserialize=int,
    )


def _float_field(key: str, section: str, field_name: str, label: str, description: str,
                 *, editable: bool = True, sensitive: bool = False, minimum: float | None = None,
                 maximum: float | None = None) -> BridgeSettingDefinition:
    def _parse(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingError(f'"{key}" must be a number.')
        parsed = float(value)
        if minimum is not None and parsed < minimum:
            raise SettingError(f'"{key}" must be at least {minimum}.')
        if maximum is not None and parsed > maximum:
            raise SettingError(f'"{key}" must be at most {maximum}.')
        return parsed

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="float", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=str, deserialize=float,
    )


def _str_field(key: str, section: str, field_name: str, label: str, description: str,
               *, editable: bool = True, sensitive: bool = False, nullable: bool = False, allow_empty: bool = True,
               check: Callable[[str], str] | None = None) -> BridgeSettingDefinition:
    def _parse(value: Any) -> str | None:
        if value is None:
            if nullable:
                return None
            raise SettingError(f'"{key}" can\'t be empty.')
        if not isinstance(value, str):
            raise SettingError(f'"{key}" must be text.')
        value = value.strip()
        if value == "":
            if nullable:
                return None
            if not allow_empty:
                raise SettingError(f'"{key}" can\'t be empty.')
            return value
        return check(value) if check is not None else value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="str", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse,
        serialize=lambda v: v or "",
        deserialize=(lambda raw: raw or None) if nullable else (lambda raw: raw),
    )


def _enum_field(key: str, section: str, field_name: str, label: str, description: str, choices: tuple[str, ...],
                *, editable: bool = True, sensitive: bool = False) -> BridgeSettingDefinition:
    def _parse(value: Any) -> str:
        if not isinstance(value, str) or value not in choices:
            raise SettingError(f'"{key}" must be one of: {", ".join(choices)}.')
        return value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="enum", enum_choices=choices, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=lambda v: v, deserialize=lambda raw: raw,
    )


def _list_str_field(key: str, section: str, field_name: str, label: str, description: str,
                    *, editable: bool = True, sensitive: bool = False, container: type = list,
                    item: Callable[[str], Any] | None = None) -> BridgeSettingDefinition:
    """A list. ``item`` checks (and may convert) each entry, raising
    ``SettingError``; duplicates are dropped, order kept."""

    def _parse(value: Any) -> Any:
        if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
            raise SettingError(f'"{key}" must be a list of text values.')
        entries = [entry.strip() for entry in value if entry.strip()]
        if item is not None:
            entries = [item(entry) for entry in entries]
        return container(dict.fromkeys(entries))

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="list_str", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse,
        serialize=lambda v: json.dumps(list(v)),
        deserialize=lambda raw: container(json.loads(raw)),
    )


# -- value checks ---------------------------------------------------------------------


def _relay(key: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        try:
            return _relay_url(value, key)
        except ConfigError as exc:
            raise SettingError(str(exc)) from exc

    return check


def _regex(key: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise SettingError(f'"{key}": {value!r} isn\'t a valid regular expression ({exc}).') from exc
        return value

    return check


def _mxid(key: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not re.fullmatch(r"@[^:\s]+:\S+", value):
            raise SettingError(f'"{key}": {value!r} isn\'t a Matrix user ID (@name:server).')
        return value

    return check


def _pubkey(key: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        from bridge.nostr import nip19

        try:
            hex_key = nip19.npub_decode(value) if value.startswith("npub1") else bytes.fromhex(value).hex()
        except (nip19.Nip19Error, ValueError) as exc:
            raise SettingError(f'"{key}": {value!r} isn\'t an npub or hex key.') from exc
        if len(hex_key) != 64:
            raise SettingError(f'"{key}": {value!r} isn\'t an npub or hex key.')
        return hex_key

    return check


def _seconds(key: str) -> Callable[[str], int]:
    def check(value: str) -> int:
        if not value.isdigit() or int(value) < 1:
            raise SettingError(f'"{key}": each entry must be a whole number of seconds, at least 1.')
        return int(value)

    return check


def _mxc(key: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not value.startswith("mxc://"):
            raise SettingError(f'"{key}" must be an mxc:// URI.')
        return value

    return check


def _http_url(key: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise SettingError(f'"{key}" must be an http(s):// URL.')
        return value

    return check


def _viewer_url(value: str) -> str:
    return _http_url("nostr.web_viewer_url")(value).rstrip("/") + "/"


def _client_name(value: str) -> str:
    return value[:100]


def _durations_field() -> BridgeSettingDefinition:
    key = "spam.ban_durations_seconds"
    base = _list_str_field(key, "spam", "ban_durations_seconds", "Ban durations (seconds)",
                           "How long automatic bans last, one entry per escalation step: a first ban, a second, and "
                           "so on. The last entry repeats.", container=tuple, item=_seconds(key))

    def _parse(value: Any) -> tuple[int, ...]:
        if isinstance(value, list):
            value = [str(v) if isinstance(v, int) and not isinstance(v, bool) else v for v in value]
        parsed = base.parse(value)
        if not parsed:
            raise SettingError(f'"{key}" needs at least one duration.')
        return parsed

    return dataclasses.replace(base, parse=_parse)


def _rate_fields(name: str, label: str, what: str) -> list[BridgeSettingDefinition]:
    return [
        _int_field(f"spam.{name}.burst", "spam", f"{name}.burst", f"{label}: burst",
                   f"Most {what} allowed at once before the rate limit applies.", minimum=1),
        _float_field(f"spam.{name}.tokens", "spam", f"{name}.tokens", f"{label}: refill",
                     f"How many {what} are allowed again every refill period.", minimum=0),
        _float_field(f"spam.{name}.per_seconds", "spam", f"{name}.per_seconds", f"{label}: refill period (seconds)",
                     f"The refill period of the {what} limit.", minimum=0.001),
    ]


_POLICIES = ("open", "pow", "web_of_trust")
_JOIN_RULES = ("knock", "invite", "public")
_RELAY_HOOK = "Changes apply straight away."

# In bridge/config.py's order, grouped by section.
_REGISTRY: dict[str, BridgeSettingDefinition] = {
    definition.key: definition
    for definition in (
        # -- bridge: set once at deployment --
        _str_field("bridge.domain", "bridge", "domain", "Domain", "The domain NIP-05 handles live under (name@domain). Set once at deployment.", editable=False),
        _str_field("bridge.listen_host", "bridge", "listen_host", "Listen host", "The local address the bridge's HTTP server binds to.", editable=False),
        _int_field("bridge.listen_port", "bridge", "listen_port", "Listen port", "The local port the bridge's HTTP server binds to.", editable=False),
        _str_field("bridge.internal_base_url", "bridge", "internal_base_url", "Internal base URL", "Where the homeserver pushes AppService transactions, if not http://listen_host:listen_port.", editable=False, nullable=True),
        # -- bridge: privilege, config.yaml only --
        _list_str_field("bridge.admins", "bridge", "admins", "Admins", "Matrix users who are bridge admins. Config.yaml only: changing the admin list through the feature it guards would be a way to take it over.", editable=False),
        _bool_field("bridge.use_synapse_admin_api", "bridge", "use_synapse_admin_api", "Use Synapse Admin API", "Also count Synapse server admins as bridge admins. Config.yaml only, for the same reason as the admin list.", editable=False),
        _bool_field("bridge.allow_db_setting_overrides", "bridge", "allow_db_setting_overrides", "Allow database setting overrides", "Whether settings changed here apply at all. Config.yaml only, and always read from the file, so a stored change can never turn the lock off.", editable=False),
        # -- bridge: policy and features --
        _bool_field("bridge.third_party_access", "bridge", "third_party_access", "Third-party access", "Master switch for users of other homeservers. Who among them may use the bridge is set by grants (Admin, Access)."),
        _str_field("bridge.space_name", "bridge", "space_name", "Space name", "Name of the space the bridge keeps for each user. Applies to spaces made from now on.", allow_empty=False),
        _str_field("bridge.space_avatar_mxc", "bridge", "space_avatar_mxc", "Space avatar", "An mxc:// avatar for those spaces; empty uses the bot's. Applies to spaces made from now on.", nullable=True, check=_mxc("bridge.space_avatar_mxc")),
        _str_field("bridge.notifications_room_name", "bridge", "notifications_room_name", "Notifications room name", "Name of each user's notifications room. Applies to rooms made from now on.", allow_empty=False),
        _str_field("bridge.notifications_room_avatar_mxc", "bridge", "notifications_room_avatar_mxc", "Notifications room avatar", "An mxc:// avatar for notifications rooms; empty uses the bot's.", nullable=True, check=_mxc("bridge.notifications_room_avatar_mxc")),
        _enum_field("bridge.msc4503_external_handle", "bridge", "msc4503_external_handle", "MSC4503 external handle", "Where a ghost's real Nostr handle is recorded: \"profile\" (needs MSC4133), \"events\", \"both\" or \"off\".", ("off", "profile", "events", "both")),
        _bool_field("bridge.msc4027_custom_reactions", "bridge", "msc4027_custom_reactions", "MSC4027 custom reactions", "Show custom-emoji reactions from Nostr as image reactions. Clients without MSC4027 show the mxc:// text instead."),
        _str_field("bridge.client_name", "bridge", "client_name", "Client name", "Name put in the NIP-89 \"client\" tag of public events, and of the bridge's app announcement. Empty: no tag.", check=_client_name),
        _str_field("bridge.client_picture", "bridge", "client_picture", "Client icon", "Icon URL in the bridge's NIP-89 app announcement. Empty: left out.", check=_http_url("bridge.client_picture")),
        _str_field("bridge.client_website", "bridge", "client_website", "Client website", "Website in the bridge's NIP-89 app announcement. Empty: left out.", check=_http_url("bridge.client_website")),
        _enum_field("bridge.ghost_room_join_rule", "bridge", "ghost_room_join_rule", "Remote User Room join rule", "Join rule of new Remote User Rooms.", _JOIN_RULES),
        _bool_field("bridge.world_readable_remote_rooms", "bridge", "world_readable_remote_rooms", "World-readable remote rooms", "Whether new Remote User Rooms can be read without joining."),
        _bool_field("bridge.accept_federated_knocks", "bridge", "accept_federated_knocks", "Accept federated knocks", "Let in knocks on Remote User Rooms from users of other homeservers too."),
        _bool_field("bridge.backstage_space_enabled", "bridge", "backstage_space_enabled", "Backstage space", "Keep one space every ghost joins, so ghosts can join bridge rooms themselves instead of being invited each time."),
        _bool_field("bridge.set_msc4501_relates_to", "bridge", "set_msc4501_relates_to", "Set MSC4501 relates_to", "Set the MSC4501 relates_to field on mirrored reposts and quotes."),
        _bool_field("bridge.use_msc4501_content_inline", "bridge", "use_msc4501_content_inline", "MSC4501 content_inline", "For a mirrored repost, mark the reposted content as inline instead of repeating it."),
        _bool_field("bridge.use_msc4501_post_event_type", "bridge", "use_msc4501_post_event_type", "MSC4501 post event type", "Send mirrored posts as the MSC4501 event type. Clients that don't know it show nothing."),
        _bool_field("bridge.set_msc4501_profile_room", "bridge", "set_msc4501_profile_room", "Set MSC4501 profile room", "Point each ghost's MSC4133 profile at its Remote User Room. Needs MSC4133 on the homeserver."),
        _bool_field("bridge.msc4144_per_message_profiles", "bridge", "msc4144_per_message_profiles", "MSC4144 per-message profiles", "Messages the bot sends on a user's behalf carry that user's name and avatar (MSC4144)."),
        _bool_field("bridge.use_msc4589_formatted_displayname", "bridge", "use_msc4589_formatted_displayname", "MSC4589 formatted display names", "Give ghosts with custom emoji in their names a formatted display name (draft MSC4589)."),
        _enum_field("bridge.quote_import_policy", "bridge", "quote_import_policy", "Quote import policy", "Whether a quoted post that isn't in Matrix is imported: \"known\" (its author has a room here), \"always\" or \"never\".", ("known", "always", "never")),
        _bool_field("bridge.dm_rooms_as_direct", "bridge", "dm_rooms_as_direct", "DM rooms as direct", "Create DM rooms as Matrix direct messages. Only where DMs aren't end-to-end encrypted by default."),
        _bool_field("bridge.shadow_id_dms", "bridge", "shadow_id_dms", "Shadow IDs take DMs", "Whether shadow IDs (made automatically, with no Profile Room) receive Nostr DMs."),
        _bool_field("bridge.shadow_id_notice", "bridge", "shadow_id_notice", "Shadow ID notice", "Tell someone in the room when a shadow ID was just made for them."),
        _list_str_field("bridge.trusted_proxies", "bridge", "trusted_proxies", "Trusted proxies", "Reverse proxies whose X-Forwarded-For is trusted for the relay's per-IP limits. Deployment topology, so config.yaml only.", editable=False),
        _enum_field("bridge.local_profile_room_join_rule", "bridge", "local_profile_room_join_rule", "Profile Room join rule", "Join rule of new Profile Rooms.", _JOIN_RULES),
        _int_field("bridge.max_ids_per_user", "bridge", "max_ids_per_user", "Max IDs per user", "How many Nostr IDs one Matrix user may hold. Bridge admins are exempt.", minimum=1),
        _int_field("bridge.id_min_length", "bridge", "id_min_length", "ID minimum length", "Shortest name a new ID may have.", minimum=1, maximum=64),
        _int_field("bridge.id_max_length", "bridge", "id_max_length", "ID maximum length", "Longest name a new ID may have.", minimum=1, maximum=64),
        _list_str_field("bridge.reserved_ids", "bridge", "reserved_ids", "Reserved IDs", "Names nobody may take, on top of the built-in list. Exact names or globs like admin*."),
        _list_str_field("bridge.id_allowed_users", "bridge", "id_allowed_users", "Who may have IDs", "Matrix ID globs like @*:example.org. Empty: every local user."),
        _int_field("bridge.renamed_id_grace_days", "bridge", "renamed_id_grace_days", "Rename grace (days)", "How long an ID's old name still resolves after a rename.", minimum=0),
        _int_field("bridge.rename_cooldown_days", "bridge", "rename_cooldown_days", "Rename cooldown (days)", "Minimum time between two renames of the same ID.", minimum=0),
        # -- homeserver --
        _str_field("homeserver.base_url", "homeserver", "base_url", "Homeserver URL", "The homeserver's Client-Server API URL. Set once at deployment.", editable=False),
        _str_field("homeserver.server_name", "homeserver", "server_name", "Server name", "The homeserver's server_name. Set once at deployment.", editable=False),
        _str_field("homeserver.synapse_admin_token", "homeserver", "synapse_admin_token", "Synapse admin token", "A Synapse admin's access token, for bridge.use_synapse_admin_api.", editable=False, sensitive=True, nullable=True),
        # -- appservice --
        _str_field("appservice.registration_path", "appservice", "registration_path", "Registration file", "Path of the AppService registration file. Set once at deployment.", editable=False),
        _str_field("appservice.id", "appservice", "id", "AppService id", "The registered AppService id. Set once at deployment.", editable=False),
        _str_field("appservice.hs_token", "appservice", "hs_token", "Homeserver token", "The registration's hs_token.", editable=False, sensitive=True),
        _str_field("appservice.as_token", "appservice", "as_token", "AppService token", "The registration's as_token.", editable=False, sensitive=True),
        _str_field("appservice.user_prefix", "appservice", "user_prefix", "Ghost prefix", "Localpart prefix of ghost users. Set once at deployment.", editable=False),
        _str_field("appservice.bot_localpart", "appservice", "bot_localpart", "Bot localpart", "The bot's localpart. Set once at deployment.", editable=False),
        _str_field("appservice.bot_display_name", "appservice", "bot_display_name", "Bot display name", "The bot's Matrix display name, applied straight away. Empty: left as it is.", nullable=True),
        _str_field("appservice.bot_avatar_mxc", "appservice", "bot_avatar_mxc", "Bot avatar", "An mxc:// avatar for the bot, applied straight away. Empty: left as it is.", nullable=True, check=_mxc("appservice.bot_avatar_mxc")),
        _str_field("appservice.command_prefix", "appservice", "command_prefix", "Command prefix", "What a command starts with when the bot isn't mentioned, e.g. \"[\".", allow_empty=False),
        _list_str_field("appservice.other_command_prefixes", "appservice", "other_command_prefixes", "Other bots' prefixes", "Command prefixes of other bots in the same rooms. Messages starting with them are never published or answered.", container=tuple),
        _list_str_field("appservice.other_bot_users", "appservice", "other_bot_users", "Other bots", "Matrix IDs of other bots in the same rooms.", container=tuple, item=_mxid("appservice.other_bot_users")),
        _list_str_field("appservice.other_bridge_users", "appservice", "other_bridge_users", "Other bridges' ghosts", "Regexes over whole Matrix IDs matching other bridges' ghosts. Their messages are never acted on.", container=tuple, item=_regex("appservice.other_bridge_users")),
        # -- storage --
        _str_field("storage.backend", "storage", "backend", "Storage backend", "\"sqlite\" or \"postgresql\". Switching needs a data migration.", editable=False),
        _str_field("storage.data_dir", "storage", "data_dir", "Data directory", "Where the sqlite database lives.", editable=False),
        _str_field("storage.postgres.dsn", "storage", "postgres.dsn", "Postgres DSN", "A full Postgres connection string, used instead of the fields below.", editable=False, sensitive=True, nullable=True),
        _str_field("storage.postgres.host", "storage", "postgres.host", "Postgres host", "Postgres host.", editable=False),
        _int_field("storage.postgres.port", "storage", "postgres.port", "Postgres port", "Postgres port.", editable=False),
        _str_field("storage.postgres.database", "storage", "postgres.database", "Postgres database", "Postgres database name.", editable=False),
        _str_field("storage.postgres.user", "storage", "postgres.user", "Postgres user", "Postgres user.", editable=False),
        _str_field("storage.postgres.password", "storage", "postgres.password", "Postgres password", "Postgres password.", editable=False, sensitive=True, nullable=True),
        # -- nostr --
        _str_field("nostr.relay_url", "nostr", "relay_url", "Relay URL", "The bridge's own relay as clients reach it. Set once at deployment.", editable=False),
        _str_field("nostr.key_encryption_secret", "nostr", "key_encryption_secret", "Key encryption secret", "What Nostr secret keys are encrypted with in the database.", editable=False, sensitive=True),
        _list_str_field("nostr.publish_relays", "nostr", "publish_relays", "Publish relays", "Public relays local IDs' events are copied to. " + _RELAY_HOOK, item=_relay("nostr.publish_relays")),
        _list_str_field("nostr.skip_relays", "nostr", "skip_relays", "Skipped relays", "Relays never sent to, even as someone's inbox (paid relays).", item=_relay("nostr.skip_relays")),
        _list_str_field("nostr.discovery_relays", "nostr", "discovery_relays", "Discovery relays", "Relays asked for accounts' relay lists and profiles.", item=_relay("nostr.discovery_relays")),
        _list_str_field("nostr.fallback_relays", "nostr", "fallback_relays", "Fallback relays", "Relays read for accounts with no relay list, and the widget's Global feed. " + _RELAY_HOOK, item=_relay("nostr.fallback_relays")),
        _list_str_field("nostr.search_relays", "nostr", "search_relays", "Search relays", "Relays the widget's search box asks for accounts and posts (NIP-50).", item=_relay("nostr.search_relays")),
        _int_field("nostr.backfill_on_follow", "nostr", "backfill_on_follow", "Backfill on follow", "How many of an account's latest posts are mirrored when it's followed.", minimum=0),
        _bool_field("nostr.serve_followed_to_local_clients", "nostr", "serve_followed_to_local_clients", "Serve followed posts to local clients", "Let local IDs' Nostr apps read their followed accounts' posts through the bridge's relay."),
        _int_field("nostr.relay_max_limit", "nostr", "relay_max_limit", "Relay max limit", "Largest result one REQ to the bridge's relay gets.", minimum=1),
        _int_field("nostr.relay_max_subscriptions", "nostr", "relay_max_subscriptions", "Relay max subscriptions", "Most open subscriptions per connection to the bridge's relay.", minimum=1),
        _list_str_field("nostr.shared_block_lists", "nostr", "shared_block_lists", "Shared block lists", "Accounts (npub or hex) whose public mute lists and reports the relay honours. " + _RELAY_HOOK, item=_pubkey("nostr.shared_block_lists")),
        _str_field("nostr.web_viewer_url", "nostr", "web_viewer_url", "Web viewer", "Web page a Nostr profile or event is opened on, followed by its npub or nevent.", allow_empty=False, check=_viewer_url),
        _int_field("nostr.max_media_mb", "nostr", "max_media_mb", "Max media (MB)", "Largest file published from Matrix or copied into it.", minimum=1),
        _int_field("nostr.refused_relay_retry_hours", "nostr", "refused_relay_retry_hours", "Refused relay retry (hours)", "How long a relay that refused an ID by policy is left alone for that ID.", minimum=0),
        _bool_field("nostr.allow_private_addresses", "nostr", "allow_private_addresses", "Allow private addresses", "Let the bridge connect to private and loopback addresses. For local development; read once at startup, so config.yaml only.", editable=False),
        # -- logging --
        _enum_field("logging.level", "logging", "level", "Log level", "The bridge's log verbosity. Set at startup, so config.yaml only.", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), editable=False),
        # -- webxdc --
        _bool_field("webxdc.enabled", "webxdc", "enabled", "Games", "Webxdc games over Nostr (the Games widget and \"[play\")."),
        _str_field("webxdc.sandbox_domain", "webxdc", "sandbox_domain", "Sandbox domain", "The hostname games run on. Needs its own DNS and TLS, so config.yaml only.", editable=False),
        _int_field("webxdc.active_days", "webxdc", "active_days", "Active days", "How long after a game was last opened its moves are followed.", minimum=1),
        _int_field("webxdc.max_update_kb", "webxdc", "max_update_kb", "Max move (KB)", "Largest game move accepted.", minimum=1),
        _int_field("webxdc.max_app_mb", "webxdc", "max_app_mb", "Max app (MB)", "Largest game the bridge fetches for a player whose browser can't.", minimum=1),
        _list_str_field("webxdc.catalog_relays", "webxdc", "catalog_relays", "Game catalog relays", "Where the Games widget finds games to browse, besides the bridge's own relays.", container=tuple, item=_relay("webxdc.catalog_relays")),
        _str_field("webxdc.avatar_mxc", "webxdc", "avatar_mxc", "Games avatar", "An mxc:// avatar for Games rooms and the Nostr Games widget; empty uses the bot's. An existing room picks it up the next time a game is opened there.", nullable=True, check=_mxc("webxdc.avatar_mxc")),
        # -- encrypted_media --
        _list_str_field("encrypted_media.bridges", "encrypted_media", "bridges", "Encrypted media bridges", "Bot Matrix IDs of this server's bridges that share one decrypted copy, in the order they ask.", container=tuple, item=_mxid("encrypted_media.bridges")),
        _float_field("encrypted_media.wait_seconds", "encrypted_media", "wait_seconds", "Encrypted media wait (seconds)", "How long to wait for an earlier bridge before asking on its own.", minimum=0, maximum=60),
        # -- spam (applied to the running spam guard straight away) --
        *_rate_fields("connections", "Connections per IP", "relay connections from one IP"),
        *_rate_fields("reqs", "Subscriptions per IP", "subscriptions (REQs) from one IP"),
        *_rate_fields("events_per_ip", "Events per IP", "events aimed at local IDs from one IP"),
        *_rate_fields("events_per_pubkey", "Events per account", "events aimed at local IDs from one account"),
        *_rate_fields("events_per_pubkey_target", "Events per account and ID", "events from one account to one local ID"),
        _float_field("spam.trusted_multiplier", "spam", "trusted_multiplier", "Trusted multiplier", "How many times larger the per-account limits are for accounts someone here follows.", minimum=0),
        _int_field("spam.max_event_bytes", "spam", "max_event_bytes", "Max event size (bytes)", "Largest event accepted from strangers.", minimum=1),
        _int_field("spam.future_skew_seconds", "spam", "future_skew_seconds", "Future skew (seconds)", "How far in the future an event's time may be.", minimum=0),
        _int_field("spam.max_interaction_age_seconds", "spam", "max_interaction_age_seconds", "Max interaction age (seconds)", "Oldest reply, mention or reaction accepted.", minimum=0),
        _int_field("spam.max_p_tags", "spam", "max_p_tags", "Max mentions", "Most accounts one event may mention.", minimum=1),
        _int_field("spam.duplicate_window_seconds", "spam", "duplicate_window_seconds", "Duplicate window (seconds)", "How long the same text from many accounts counts as a wave.", minimum=0),
        _int_field("spam.duplicate_min_pubkeys", "spam", "duplicate_min_pubkeys", "Duplicate accounts", "How many accounts sending the same text make a wave.", minimum=1),
        _int_field("spam.duplicate_min_length", "spam", "duplicate_min_length", "Duplicate min length", "Shorter texts are too common to judge.", minimum=0),
        _list_str_field("spam.spam_patterns", "spam", "spam_patterns", "Spam patterns", "Regexes; strangers' events matching any are refused.", container=tuple, item=_regex("spam.spam_patterns")),
        _enum_field("spam.inbound_policy", "spam", "inbound_policy", "Inbound policy", "What strangers must do: \"open\", \"pow\" (proof of work) or \"web_of_trust\".", _POLICIES),
        _int_field("spam.stranger_pow_bits", "spam", "stranger_pow_bits", "Stranger proof of work (bits)", "Proof of work strangers need under the \"pow\" policy.", minimum=0, maximum=256),
        _int_field("spam.strikes_to_ban", "spam", "strikes_to_ban", "Strikes to ban", "Rejections within the strike window that ban an account or IP.", minimum=1),
        _int_field("spam.strike_window_seconds", "spam", "strike_window_seconds", "Strike window (seconds)", "The window strikes are counted in.", minimum=0),
        _durations_field(),
        _int_field("spam.escalation_memory_days", "spam", "escalation_memory_days", "Escalation memory (days)", "How long an earlier ban makes the next one longer.", minimum=0),
        _int_field("spam.wave_threshold", "spam", "wave_threshold", "Wave threshold", "New accounts within the wave window that raise the shields.", minimum=1),
        _int_field("spam.wave_window_seconds", "spam", "wave_window_seconds", "Wave window (seconds)", "The window new accounts are counted in.", minimum=0),
        _enum_field("spam.wave_policy", "spam", "wave_policy", "Shields policy", "What strangers must do while the shields are up.", _POLICIES),
        _int_field("spam.wave_cooldown_seconds", "spam", "wave_cooldown_seconds", "Wave cooldown (seconds)", "How long things must be quiet before the shields go down.", minimum=0),
        _int_field("spam.stranger_fast_reply_seconds", "spam", "stranger_fast_reply_seconds", "Fast reply (seconds)", "A stranger's reply this soon after a post here is refused as a bot. 0 turns it off.", minimum=0),
        _list_str_field("spam.stranger_scam_patterns", "spam", "stranger_scam_patterns", "Scam patterns", "Regexes; strangers' posts with a link matching any are refused. Empty turns it off.", container=tuple, item=_regex("spam.stranger_scam_patterns")),
        _bool_field("spam.enabled", "spam", "enabled", "Spam checks", "Off: no spam checks, but bans set by hand, size and clock checks and connection limits still apply."),
    )
}


def known_settings() -> list[BridgeSettingDefinition]:
    """Every registered setting, in registry order."""
    return list(_REGISTRY.values())


def definition_of(key: str) -> BridgeSettingDefinition | None:
    return _REGISTRY.get(key)


def value_of(config: BridgeConfig, definition: BridgeSettingDefinition) -> Any:
    """``definition``'s value in ``config``. Callers must not send it
    anywhere for a ``sensitive`` definition."""
    value = _get_value(config, definition)
    return list(value) if isinstance(value, tuple) else value


def check(config: BridgeConfig) -> None:
    """Rules across settings, as config.yaml's loader applies them."""
    bridge, appservice = config.bridge, config.appservice
    if bridge.id_min_length > bridge.id_max_length:
        raise SettingError("bridge.id_min_length can't be more than bridge.id_max_length.")
    if appservice.command_prefix in appservice.other_command_prefixes:
        raise SettingError("appservice.other_command_prefixes can't include this bridge's own command_prefix.")


def build_effective_config(base_config: BridgeConfig, overrides: dict[str, str]) -> BridgeConfig:
    """``base_config`` with every editable override folded in. Rows for
    unknown or non-editable keys, unreadable ones, and a set that breaks
    the rules across settings are skipped with a warning."""
    config = base_config
    for key, raw in overrides.items():
        definition = _REGISTRY.get(key)
        if definition is None:
            logger.warning("Ignoring a stored setting for unknown key %r.", key)
            continue
        if not definition.editable:
            logger.warning("Ignoring a stored setting for %r, which only config.yaml may set.", key)
            continue
        try:
            value = definition.deserialize(raw)
        except (ValueError, TypeError):
            logger.warning("Ignoring an unreadable stored setting for %r: %r", key, raw)
            continue
        candidate = _with_value(config, definition, value)
        try:
            check(candidate)
        except SettingError as exc:
            logger.warning("Ignoring the stored setting for %r: %s", key, exc)
            continue
        config = candidate
    return config


async def stored_overrides(repository: Any) -> dict[str, str]:
    """The stored rows, by setting key."""
    return {key[len(STATE_PREFIX):]: value for key, value in await repository.list_state(STATE_PREFIX)}


async def effective_config(base_config: BridgeConfig, repository: Any) -> BridgeConfig:
    """``base_config`` with the stored overrides folded in, unless
    ``bridge.allow_db_setting_overrides`` is off in ``base_config`` (then
    it comes back as it is, and nothing is read)."""
    if not base_config.bridge.allow_db_setting_overrides:
        return base_config
    return build_effective_config(base_config, await stored_overrides(repository))


async def apply_override(repository: Any, key: str, raw_value: Any, *, base_config: BridgeConfig | None = None) -> Any:
    """Check and store one override; returns the native value. Raises
    ``SettingError`` for an unknown or config-only key, a bad value, or (with
    ``base_config``) a result that breaks the rules across settings. Does
    not check ``allow_db_setting_overrides``: callers do."""
    definition = _REGISTRY.get(key)
    if definition is None:
        raise SettingError(f'Unknown setting "{key}".')
    if not definition.editable:
        raise SettingError(f'"{key}" can only be changed in config.yaml.')
    value = definition.parse(raw_value)
    if base_config is not None:
        stored = await stored_overrides(repository)
        current = build_effective_config(base_config, stored)
        check(_with_value(current, definition, value))
    await repository.set_state(STATE_PREFIX + key, definition.serialize(value))
    return value


async def clear_override(repository: Any, key: str) -> None:
    """Remove one stored override, so ``key`` is config.yaml's again.
    Raises ``SettingError`` for an unknown or config-only key. Does not
    check ``allow_db_setting_overrides``: callers do."""
    definition = _REGISTRY.get(key)
    if definition is None:
        raise SettingError(f'Unknown setting "{key}".')
    if not definition.editable:
        raise SettingError(f'"{key}" can only be changed in config.yaml.')
    await repository.delete_state(STATE_PREFIX + key)


# -- after a change -------------------------------------------------------------------


async def refresh(app: Any) -> BridgeConfig:
    """Rebuild ``app.state.config`` after a change, and re-apply what the
    bridge otherwise reads only at startup."""
    old: BridgeConfig = app.state.config
    new = await effective_config(app.state.base_config, app.state.repository)
    app.state.config = new
    await reapply(app, old, new)
    return new


async def reapply(app: Any, old: BridgeConfig, new: BridgeConfig) -> None:
    from bridge.tasks import run_in_background

    state = app.state
    if new.spam is not None and new.spam != old.spam and getattr(state, "spam", None) is not None:
        state.spam.reconfigure(new.spam)
    if (old.bridge.client_name, old.bridge.client_picture, old.bridge.client_website) != (
            new.bridge.client_name, new.bridge.client_picture, new.bridge.client_website):
        from bridge.app_handler import client_tag, publish_handler

        if getattr(state, "bot_identity", None) is not None:
            state.signer.client_tag = client_tag(app)
            run_in_background(app, publish_handler(app), name="app announcement")
    if (old.appservice.bot_display_name, old.appservice.bot_avatar_mxc) != (
            new.appservice.bot_display_name, new.appservice.bot_avatar_mxc):
        from bridge.server import sync_bot_profile

        await sync_bot_profile(state.homeserver, new)
    feeds = getattr(state, "feeds", None)
    if feeds is not None:
        if (old.nostr.publish_relays, old.nostr.fallback_relays, old.webxdc.enabled, old.webxdc.catalog_relays) != (
                new.nostr.publish_relays, new.nostr.fallback_relays, new.webxdc.enabled, new.webxdc.catalog_relays):
            feeds.changed()
        if old.nostr.shared_block_lists != new.nostr.shared_block_lists:
            run_in_background(app, feeds.refresh_shared_blocks(), name="shared block lists")
    if old.nostr.publish_relays != new.nostr.publish_relays:
        # Each ID's relay list (kind 10002) names the publish relays.
        from bridge.identity_events import sync_all_identities

        run_in_background(app, sync_all_identities(app), name="identity sync")
