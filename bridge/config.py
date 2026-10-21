"""Loader for the bridge's ``config.yaml``.

Parses the YAML file described in ``config.example.yaml`` into typed,
validated dataclasses so the rest of the codebase never touches raw dicts.
Every setting is documented in CONFIG.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"
CONFIG_PATH_ENV_VAR = "BRIDGE_CONFIG"

DEFAULT_PUBLISH_RELAYS = (
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://poster.place/relay",
    "wss://relay.poster.place/",
)
DEFAULT_DISCOVERY_RELAYS = ("wss://purplepag.es", "wss://relay.damus.io")
# For reading accounts that publish no relay list. Not poster.place: it
# requires authentication even to read.
DEFAULT_FALLBACK_RELAYS = ("wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net")

# Smallest secret accepted for encrypting keys at rest (see bridge.key_vault).
_MIN_KEY_SECRET_LENGTH = 32


class ConfigError(Exception):
    """Raised when ``config.yaml`` is missing required fields or malformed."""


@dataclass(frozen=True)
class BridgeSection:
    # The domain NIP-05 handles live under: an ID named "q" is
    # q@<domain>. Doesn't have to be the homeserver's server_name, but its
    # web server must proxy /.well-known/nostr.json to this bridge.
    domain: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8092
    # Address the homeserver pushes AppService transactions to. Defaults
    # to http://{listen_host}:{listen_port}.
    internal_base_url: str | None = None
    # MXIDs treated as bridge admins.
    admins: list[str] = field(default_factory=list)
    # Also count Synapse server admins as bridge admins, by asking
    # Synapse's Admin API. Synapse-only, so off by default; everything
    # else works on any homeserver. Needs homeserver.synapse_admin_token.
    use_synapse_admin_api: bool = False
    # Name and avatar (mxc://) of the space the bridge keeps for each user,
    # and of their notifications room. Without an avatar, the bot's is used.
    space_name: str = "Nostr"
    space_avatar_mxc: str | None = None
    notifications_room_name: str = "Nostr Notifications"
    notifications_room_avatar_mxc: str | None = None
    # Where a ghost's real Nostr handle is recorded (MSC4503): "profile"
    # (an MSC4133 profile field; needs MSC4133 on the homeserver), "events"
    # (on every event the ghost sends), "both", or "off". Same setting as
    # the sibling bridges. Takes effect once ghosts exist (following).
    msc4503_external_handle: str = "off"
    # Join rule of a Remote User Room: "knock" (default: anyone may ask, and
    # is let straight in), "invite" or "public".
    ghost_room_join_rule: str = "knock"
    # Remote User Rooms readable without joining (history_visibility:
    # world_readable). Off by default.
    world_readable_remote_rooms: bool = False
    # Let in knocks from users on other homeservers too. Off by default.
    accept_federated_knocks: bool = False
    # MSC4501 (same settings and defaults as the sibling bridges).
    set_msc4501_relates_to: bool = True
    use_msc4501_content_inline: bool = True
    use_msc4501_post_event_type: bool = False
    set_msc4501_profile_room: bool = False
    msc4144_per_message_profiles: bool = True
    # Whether a quoted post that isn't in Matrix is imported: "known" (only
    # if its author already has a room here), "always" or "never".
    quote_import_policy: str = "known"
    # Create DM rooms as Matrix direct messages (is_direct). Off by
    # default; only turn on where DMs aren't end-to-end encrypted by default.
    dm_rooms_as_direct: bool = False
    # Addresses of reverse proxies whose X-Forwarded-For is trusted for the
    # relay's per-IP limits and bans.
    trusted_proxies: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    # Join rule of a Profile Room: "knock" (only its owner's knock is let
    # in), "invite" or "public".
    local_profile_room_join_rule: str = "knock"
    # -- Nostr IDs (see DESIGN.md, "Nostr IDs") --
    # How many IDs one Matrix user may hold. Bridge admins are exempt.
    max_ids_per_user: int = 5
    id_min_length: int = 1
    id_max_length: int = 32
    # Names nobody may take, on top of the built-in list. Exact names or
    # shell-style globs ("admin*").
    reserved_ids: list[str] = field(default_factory=list)
    # Who may have IDs: MXID globs ("@*:example.org"). Empty: every local user.
    id_allowed_users: list[str] = field(default_factory=list)
    # After a rename, how long the old name still resolves to the ID.
    renamed_id_grace_days: int = 30
    # Minimum time between two renames of the same ID.
    rename_cooldown_days: int = 7

    def resolved_internal_base_url(self) -> str:
        return self.internal_base_url or f"http://{self.listen_host}:{self.listen_port}"


@dataclass(frozen=True)
class HomeserverSection:
    # Client-Server API base URL, as reachable from the bridge process.
    base_url: str
    # The homeserver's server_name (the part after the colon in
    # @user:server.name). Only its users can have Nostr IDs.
    server_name: str
    # Access token of a Synapse server admin. Only used, and only
    # required, with bridge.use_synapse_admin_api on.
    synapse_admin_token: str | None = None


@dataclass(frozen=True)
class AppserviceSection:
    registration_path: str
    id: str
    hs_token: str
    as_token: str
    # Localpart prefix of ghosts of remote Nostr accounts: @nostr_<hex>.
    user_prefix: str = "nostr_"
    # The bridge's bot: who users talk to, and who creates rooms.
    bot_localpart: str = "nostrbot"
    bot_display_name: str | None = None
    bot_avatar_mxc: str | None = None
    # What a command starts with when the bot isn't tagged: "[help".
    # matrix-appservice-activitypub uses ";", matrix-appservice-atproto "]".
    command_prefix: str = "["


@dataclass(frozen=True)
class PostgresSection:
    dsn: str | None = None
    host: str = "localhost"
    port: int = 5432
    database: str = "matrix_appservice_nostr"
    user: str = "matrix_appservice_nostr"
    password: str | None = None

    def resolved_dsn(self) -> str:
        if self.dsn:
            return self.dsn

        def quoted(value: object) -> str:
            escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
            return f"'{escaped}'"

        parts = {"host": self.host, "port": self.port, "dbname": self.database, "user": self.user}
        if self.password:
            parts["password"] = self.password
        return " ".join(f"{key}={quoted(value)}" for key, value in parts.items())


@dataclass(frozen=True)
class StorageSection:
    backend: str = "sqlite"
    data_dir: str = "./data"
    postgres: PostgresSection = field(default_factory=PostgresSection)


@dataclass(frozen=True)
class NostrSection:
    # The bridge's own relay, as clients reach it: wss://nostr.example.org.
    # Also where its NIP-11 document and media are served.
    relay_url: str
    # Secret the Nostr secret keys in the database are encrypted with
    # (bridge.key_vault). Losing it loses every key; changing it needs a
    # re-encryption, so treat it like the database itself.
    key_encryption_secret: str
    # Public relays local IDs' events are copied to, for reach.
    publish_relays: list[str] = field(default_factory=lambda: list(DEFAULT_PUBLISH_RELAYS))
    # Relays asked for accounts' relay lists (kind 10002) and profiles.
    discovery_relays: list[str] = field(default_factory=lambda: list(DEFAULT_DISCOVERY_RELAYS))
    # Relays read for accounts that publish no relay list.
    fallback_relays: list[str] = field(default_factory=lambda: list(DEFAULT_FALLBACK_RELAYS))
    # How many of an account's latest posts are mirrored when it's followed.
    backfill_on_follow: int = 20
    # Let local IDs' own Nostr apps read their followed accounts' posts
    # through the bridge's relay (after NIP-42 auth). Off by default: it
    # costs homeserver load.
    serve_followed_to_local_clients: bool = False
    # Largest REQ result and most open subscriptions per relay connection.
    relay_max_limit: int = 500
    relay_max_subscriptions: int = 20
    # Accounts whose public mute lists (kind 10000) and reports (kind 1984)
    # the relay honours: anyone on them is refused (unless someone here
    # follows them). npub or hex.
    shared_block_lists: list[str] = field(default_factory=list)
    # Web page a Nostr profile or event can be opened on, followed by its
    # NIP-19 identifier (npub1..., nevent1...).
    web_viewer_url: str = "https://ditto.pub/"
    # Largest file (image, video, audio, document) published from Matrix,
    # and later the largest copied into Matrix, in MB.
    max_media_mb: int = 50
    # A relay that refuses an ID by policy isn't tried again for that ID
    # for this long.
    refused_relay_retry_hours: int = 24
    # Let the bridge connect to private, loopback and link-local addresses
    # (relays, media, NIP-05 lookups). Only for local development.
    allow_private_addresses: bool = False

    @property
    def relay_http_base(self) -> str:
        """``relay_url`` as the https:// URL its HTTP side is served at,
        without a trailing slash."""
        parts = urlsplit(self.relay_url)
        scheme = "https" if parts.scheme == "wss" else "http"
        return f"{scheme}://{parts.netloc}{parts.path}".rstrip("/")


@dataclass(frozen=True)
class LoggingSection:
    level: str = "INFO"


@dataclass(frozen=True)
class BridgeConfig:
    bridge: BridgeSection
    homeserver: HomeserverSection
    appservice: AppserviceSection
    storage: StorageSection
    nostr: NostrSection
    logging: LoggingSection
    # The relay's spam rules (bridge.spam.SpamConfig); see CONFIG.md.
    spam: Any = None


def _require(section: dict[str, Any], key: str, section_name: str) -> Any:
    if key not in section or section[key] in (None, ""):
        raise ConfigError(f"Missing required config field: {section_name}.{key}")
    return section[key]


def _section(raw: dict[str, Any], name: str, *, required: bool = False) -> dict[str, Any]:
    if name not in raw:
        if required:
            raise ConfigError(f"Missing required top-level config section: {name}")
        return {}
    value = raw[name] or {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _pubkeys(section: dict[str, Any], key: str) -> list[str]:
    from bridge.nostr import nip19

    out = []
    for value in _string_list(section, key, "nostr"):
        value = value.strip()
        try:
            out.append(nip19.npub_decode(value) if value.startswith("npub1") else bytes.fromhex(value).hex())
        except (nip19.Nip19Error, ValueError) as exc:
            raise ConfigError(f"nostr.{key}: {value!r} isn't an npub or hex key") from exc
    return out


def _choice(section: dict[str, Any], key: str, choices: tuple[str, ...], default: str) -> str:
    value = str(section.get(key, default))
    if value not in choices:
        raise ConfigError(f"bridge.{key} must be one of {', '.join(choices)}; got {value!r}")
    return value


def _string_list(section: dict[str, Any], key: str, section_name: str) -> list[str]:
    value = section.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{section_name}.{key} must be a list of strings")
    return list(value)


def _relay_url(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a relay URL (wss://...)")
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("wss", "ws") or not parts.hostname:
        raise ConfigError(f"{where} must be a ws:// or wss:// URL, got {value!r}")
    return url


def _relay_list(section: dict[str, Any], key: str, default: tuple[str, ...]) -> list[str]:
    value = section.get(key)
    if value is None:
        return list(default)
    if not isinstance(value, list):
        raise ConfigError(f"nostr.{key} must be a list of relay URLs")
    return list(dict.fromkeys(_relay_url(item, f"nostr.{key}") for item in value))


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


def load_config(path: str | os.PathLike[str] | None = None) -> BridgeConfig:
    """Load and validate ``config.yaml``.

    Resolution order for the path: explicit ``path`` argument, then the
    ``BRIDGE_CONFIG`` environment variable, then ``./config.yaml``.
    """
    resolved = Path(path or os.environ.get(CONFIG_PATH_ENV_VAR, DEFAULT_CONFIG_PATH))
    if not resolved.is_file():
        raise ConfigError(
            f"Config file not found at {resolved}. Copy config.example.yaml to "
            f"{DEFAULT_CONFIG_PATH} and fill in your values."
        )
    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{resolved} must contain a YAML mapping at the top level")
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> BridgeConfig:
    bridge_raw = _section(raw, "bridge", required=True)
    homeserver_raw = _section(raw, "homeserver", required=True)
    appservice_raw = _section(raw, "appservice", required=True)
    nostr_raw = _section(raw, "nostr", required=True)

    domain = str(_require(bridge_raw, "domain", "bridge")).strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(domain):
        raise ConfigError(f"bridge.domain must be a domain name like example.org, got {domain!r}")
    local_profile_room_join_rule = str(bridge_raw.get("local_profile_room_join_rule", "knock"))
    if local_profile_room_join_rule not in ("public", "invite", "knock"):
        raise ConfigError(
            "bridge.local_profile_room_join_rule must be 'public', 'invite' or 'knock', "
            f"got {local_profile_room_join_rule!r}"
        )
    msc4503_external_handle = str(bridge_raw.get("msc4503_external_handle", "off"))
    if msc4503_external_handle not in ("off", "profile", "events", "both"):
        raise ConfigError(
            "bridge.msc4503_external_handle must be one of 'off', 'profile', 'events', 'both', "
            f"got {msc4503_external_handle!r}"
        )
    id_min_length = int(bridge_raw.get("id_min_length", 1))
    id_max_length = int(bridge_raw.get("id_max_length", 32))
    if not 1 <= id_min_length <= id_max_length <= 64:
        raise ConfigError("bridge.id_min_length and id_max_length must satisfy 1 <= min <= max <= 64")
    bridge_section = BridgeSection(
        domain=domain,
        listen_host=str(bridge_raw.get("listen_host", "127.0.0.1")),
        listen_port=int(bridge_raw.get("listen_port", 8092)),
        internal_base_url=(str(bridge_raw.get("internal_base_url") or "")).rstrip("/") or None,
        admins=[str(mxid) for mxid in bridge_raw.get("admins") or []],
        use_synapse_admin_api=bool(bridge_raw.get("use_synapse_admin_api", False)),
        space_name=str(bridge_raw.get("space_name") or "Nostr"),
        space_avatar_mxc=bridge_raw.get("space_avatar_mxc") or None,
        notifications_room_name=str(bridge_raw.get("notifications_room_name") or "Nostr Notifications"),
        notifications_room_avatar_mxc=bridge_raw.get("notifications_room_avatar_mxc") or None,
        local_profile_room_join_rule=local_profile_room_join_rule,
        msc4503_external_handle=msc4503_external_handle,
        ghost_room_join_rule=_choice(bridge_raw, "ghost_room_join_rule", ("public", "invite", "knock"), "knock"),
        world_readable_remote_rooms=bool(bridge_raw.get("world_readable_remote_rooms", False)),
        accept_federated_knocks=bool(bridge_raw.get("accept_federated_knocks", False)),
        set_msc4501_relates_to=bool(bridge_raw.get("set_msc4501_relates_to", True)),
        use_msc4501_content_inline=bool(bridge_raw.get("use_msc4501_content_inline", True)),
        use_msc4501_post_event_type=bool(bridge_raw.get("use_msc4501_post_event_type", False)),
        set_msc4501_profile_room=bool(bridge_raw.get("set_msc4501_profile_room", False)),
        msc4144_per_message_profiles=bool(bridge_raw.get("msc4144_per_message_profiles", True)),
        quote_import_policy=_choice(bridge_raw, "quote_import_policy", ("always", "known", "never"), "known"),
        dm_rooms_as_direct=bool(bridge_raw.get("dm_rooms_as_direct", False)),
        trusted_proxies=_string_list(bridge_raw, "trusted_proxies", "bridge") or ["127.0.0.1", "::1"],
        max_ids_per_user=int(bridge_raw.get("max_ids_per_user", 5)),
        id_min_length=id_min_length,
        id_max_length=id_max_length,
        reserved_ids=_string_list(bridge_raw, "reserved_ids", "bridge"),
        id_allowed_users=_string_list(bridge_raw, "id_allowed_users", "bridge"),
        renamed_id_grace_days=int(bridge_raw.get("renamed_id_grace_days", 30)),
        rename_cooldown_days=int(bridge_raw.get("rename_cooldown_days", 7)),
    )

    homeserver_section = HomeserverSection(
        base_url=str(_require(homeserver_raw, "base_url", "homeserver")).rstrip("/"),
        server_name=str(_require(homeserver_raw, "server_name", "homeserver")),
        synapse_admin_token=homeserver_raw.get("synapse_admin_token") or None,
    )
    if bridge_section.use_synapse_admin_api and not homeserver_section.synapse_admin_token:
        raise ConfigError(
            "homeserver.synapse_admin_token is required when bridge.use_synapse_admin_api is on. "
            "Set it, or turn bridge.use_synapse_admin_api off (the default) and list admins in bridge.admins."
        )

    command_prefix = str(appservice_raw.get("command_prefix", "[")).strip()
    if not command_prefix:
        raise ConfigError("appservice.command_prefix must not be empty")
    user_prefix = str(appservice_raw.get("user_prefix", "nostr_"))
    bot_localpart = str(appservice_raw.get("bot_localpart", "nostrbot"))
    if bot_localpart.startswith(user_prefix):
        raise ConfigError("appservice.bot_localpart must not start with appservice.user_prefix")
    appservice_section = AppserviceSection(
        registration_path=str(_require(appservice_raw, "registration_path", "appservice")),
        id=str(_require(appservice_raw, "id", "appservice")),
        hs_token=str(_require(appservice_raw, "hs_token", "appservice")),
        as_token=str(_require(appservice_raw, "as_token", "appservice")),
        user_prefix=user_prefix,
        bot_localpart=bot_localpart,
        bot_display_name=appservice_raw.get("bot_display_name") or None,
        bot_avatar_mxc=appservice_raw.get("bot_avatar_mxc") or None,
        command_prefix=command_prefix,
    )

    storage_raw = _section(raw, "storage")
    backend = str(storage_raw.get("backend", "sqlite"))
    if backend not in ("sqlite", "postgresql"):
        raise ConfigError(f"storage.backend must be 'sqlite' or 'postgresql', got {backend!r}")
    postgres_raw = storage_raw.get("postgres", {}) or {}
    postgres_section = PostgresSection(
        dsn=postgres_raw.get("dsn") or None,
        host=str(postgres_raw.get("host", "localhost")),
        port=int(postgres_raw.get("port", 5432)),
        database=str(postgres_raw.get("database", "matrix_appservice_nostr")),
        user=str(postgres_raw.get("user", "matrix_appservice_nostr")),
        password=str(postgres_raw["password"]) if postgres_raw.get("password") not in (None, "") else None,
    )
    storage_section = StorageSection(
        backend=backend, data_dir=str(storage_raw.get("data_dir", "./data")), postgres=postgres_section
    )

    key_secret = str(_require(nostr_raw, "key_encryption_secret", "nostr"))
    if len(key_secret) < _MIN_KEY_SECRET_LENGTH:
        raise ConfigError(
            f"nostr.key_encryption_secret must be at least {_MIN_KEY_SECRET_LENGTH} characters; "
            'generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    nostr_section = NostrSection(
        relay_url=_relay_url(_require(nostr_raw, "relay_url", "nostr"), "nostr.relay_url"),
        key_encryption_secret=key_secret,
        publish_relays=_relay_list(nostr_raw, "publish_relays", DEFAULT_PUBLISH_RELAYS),
        discovery_relays=_relay_list(nostr_raw, "discovery_relays", DEFAULT_DISCOVERY_RELAYS),
        fallback_relays=_relay_list(nostr_raw, "fallback_relays", DEFAULT_FALLBACK_RELAYS),
        web_viewer_url=str(nostr_raw.get("web_viewer_url") or "https://ditto.pub/").rstrip("/") + "/",
        backfill_on_follow=int(nostr_raw.get("backfill_on_follow", 20)),
        shared_block_lists=_pubkeys(nostr_raw, "shared_block_lists"),
        serve_followed_to_local_clients=bool(nostr_raw.get("serve_followed_to_local_clients", False)),
        relay_max_limit=int(nostr_raw.get("relay_max_limit", 500)),
        relay_max_subscriptions=int(nostr_raw.get("relay_max_subscriptions", 20)),
        max_media_mb=int(nostr_raw.get("max_media_mb", 50)),
        refused_relay_retry_hours=int(nostr_raw.get("refused_relay_retry_hours", 24)),
        allow_private_addresses=bool(nostr_raw.get("allow_private_addresses", False)),
    )

    logging_raw = _section(raw, "logging")
    from bridge.spam import SpamConfig

    try:
        spam_section = SpamConfig.from_dict(_section(raw, "spam"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return BridgeConfig(
        spam=spam_section,
        bridge=bridge_section,
        homeserver=homeserver_section,
        appservice=appservice_section,
        storage=storage_section,
        nostr=nostr_section,
        logging=LoggingSection(level=str(logging_raw.get("level", "INFO"))),
    )
