"""Bridge settings an admin changes from the widget (bridge.bridge_settings):
the registry, folding stored overrides over config.yaml, the kill switch,
the admin API, and settings read at startup taking effect straight away."""

from __future__ import annotations

import dataclasses

import pytest

from bridge import bridge_settings
from bridge.bridge_settings import SettingError
from bridge.config import BridgeConfig, parse_config
from bridge.sqlite_repository import SqliteBridgeRepository
from tests.conftest import make_client, raw_config, settle
from tests.test_keys import run, widget

ADMIN = "@admin:example.org"


def _fields(obj, prefix=""):
    """Every leaf setting of a config section, dotted."""
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if dataclasses.is_dataclass(value):
            yield from _fields(value, f"{prefix}{f.name}.")
        else:
            yield prefix + f.name


def test_every_config_field_is_registered(config) -> None:
    keys = {d.key for d in bridge_settings.known_settings()}
    for section in dataclasses.fields(BridgeConfig):
        value = getattr(config, section.name)
        for name in _fields(value):
            assert f"{section.name}.{name}" in keys, f"{section.name}.{name} isn't registered"
    for definition in bridge_settings.known_settings():
        bridge_settings.value_of(config, definition)  # every path resolves


def test_secrets_and_topology_are_config_only() -> None:
    by_key = {d.key: d for d in bridge_settings.known_settings()}
    for key in ("appservice.as_token", "appservice.hs_token", "homeserver.synapse_admin_token",
                "storage.postgres.password", "storage.postgres.dsn", "nostr.key_encryption_secret"):
        assert by_key[key].sensitive and not by_key[key].editable
    for key in ("bridge.domain", "bridge.admins", "bridge.use_synapse_admin_api", "bridge.allow_db_setting_overrides",
                "nostr.relay_url", "nostr.allow_private_addresses", "webxdc.sandbox_domain", "logging.level",
                "homeserver.base_url", "appservice.bot_localpart", "storage.backend", "bridge.listen_port"):
        assert not by_key[key].editable, key
    for key in ("spam.inbound_policy", "spam.connections.burst", "bridge.client_name", "appservice.bot_display_name",
                "nostr.publish_relays", "bridge.third_party_access"):
        assert by_key[key].editable, key


def test_overrides_fold_over_the_file(config) -> None:
    merged = bridge_settings.build_effective_config(config, {
        "bridge.third_party_access": "false",
        "spam.connections.burst": "7",
        "spam.inbound_policy": "pow",
        "nostr.publish_relays": '["wss://a.example"]',
        "bridge.domain": "evil.example",  # config.yaml only: ignored
        "no.such.key": "1",  # unknown: ignored
        "bridge.max_ids_per_user": "not a number",  # unreadable: ignored
    })
    assert merged.bridge.third_party_access is False
    assert merged.spam.connections.burst == 7 and merged.spam.connections.tokens == config.spam.connections.tokens
    assert merged.spam.inbound_policy == "pow"
    assert merged.nostr.publish_relays == ["wss://a.example"]
    assert merged.bridge.domain == "example.org"
    assert merged.bridge.max_ids_per_user == config.bridge.max_ids_per_user
    assert config.bridge.third_party_access is True  # the file's config never changes


def test_rules_across_settings_hold(config) -> None:
    merged = bridge_settings.build_effective_config(config, {"bridge.id_min_length": "40", "bridge.id_max_length": "10"})
    assert merged.bridge.id_min_length <= merged.bridge.id_max_length


def test_values_are_checked() -> None:
    by_key = {d.key: d for d in bridge_settings.known_settings()}
    with pytest.raises(SettingError):
        by_key["nostr.publish_relays"].parse(["https://not-a-relay.example"])
    with pytest.raises(SettingError):
        by_key["spam.spam_patterns"].parse(["(unclosed"])
    with pytest.raises(SettingError):
        by_key["spam.inbound_policy"].parse("nobody")
    with pytest.raises(SettingError):
        by_key["bridge.max_ids_per_user"].parse(0)
    with pytest.raises(SettingError):
        by_key["appservice.bot_avatar_mxc"].parse("https://example.org/a.png")
    npub_key = "a" * 64
    from bridge.nostr import nip19

    assert by_key["nostr.shared_block_lists"].parse([nip19.npub_encode(npub_key)]) == [npub_key]
    assert by_key["spam.ban_durations_seconds"].parse([60, "120"]) == (60, 120)
    assert by_key["nostr.web_viewer_url"].parse("https://viewer.example") == "https://viewer.example/"


def _repository_run(fn, *args, **kwargs):
    import asyncio

    return asyncio.run(fn(*args, **kwargs))


def test_store_read_and_clear(config) -> None:
    async def scenario():
        repository = SqliteBridgeRepository(":memory:")
        await bridge_settings.apply_override(repository, "bridge.shadow_id_dms", True, base_config=config)
        assert (await repository.get_state("setting:bridge.shadow_id_dms")) == "true"
        assert (await bridge_settings.effective_config(config, repository)).bridge.shadow_id_dms is True
        with pytest.raises(SettingError):
            await bridge_settings.apply_override(repository, "bridge.admins", ["@me:example.org"])
        with pytest.raises(SettingError):
            await bridge_settings.apply_override(repository, "bridge.id_min_length", 50, base_config=config)
        await bridge_settings.clear_override(repository, "bridge.shadow_id_dms")
        assert (await bridge_settings.effective_config(config, repository)).bridge.shadow_id_dms is False
        # The kill switch: stored rows are ignored, not deleted.
        await bridge_settings.apply_override(repository, "bridge.shadow_id_dms", True)
        locked = parse_config(raw_config(bridge={"allow_db_setting_overrides": False}))
        assert (await bridge_settings.effective_config(locked, repository)).bridge.shadow_id_dms is False
        assert (await repository.get_state("setting:bridge.shadow_id_dms")) == "true"
        await repository.close()

    _repository_run(scenario)


# -- the admin API ------------------------------------------------------------------------


def test_settings_api(client, homeserver) -> None:
    call = widget(client, homeserver, user=ADMIN)
    data = call("/widget/api/admin/settings").json()
    assert data["overrides_enabled"] is True
    by_key = {s["key"]: s for s in data["settings"]}
    assert by_key["appservice.as_token"]["value"] is None and by_key["appservice.as_token"]["sensitive"]
    assert by_key["nostr.key_encryption_secret"]["value"] is None
    assert by_key["bridge.domain"]["editable"] is False and by_key["bridge.domain"]["value"] == "example.org"
    assert by_key["spam.inbound_policy"] == {**by_key["spam.inbound_policy"], "source": "config", "value": "open", "editable": True}

    assert call("/widget/api/admin/settings/spam.inbound_policy", {"value": "pow"}).json()["value"] == "pow"
    assert client.app.state.spam.config.inbound_policy == "pow"  # the running guard took it
    assert client.app.state.base_config.spam.inbound_policy == "open"
    by_key = {s["key"]: s for s in call("/widget/api/admin/settings").json()["settings"]}
    assert by_key["spam.inbound_policy"]["source"] == "database"

    assert call("/widget/api/admin/settings/bridge.domain", {"value": "x.example"}).status_code == 400
    assert call("/widget/api/admin/settings/spam.inbound_policy", {"value": "maybe"}).status_code == 400
    assert call("/widget/api/admin/settings/nope", {"value": 1}).status_code == 400

    reset = call("/widget/api/admin/settings/spam.inbound_policy/reset", {}).json()
    assert reset == {"key": "spam.inbound_policy", "value": "open", "source": "config"}
    assert client.app.state.spam.config.inbound_policy == "open"


def test_settings_need_an_admin(client, homeserver) -> None:
    call = widget(client, homeserver)
    assert call("/widget/api/admin/settings").status_code == 403
    assert call("/widget/api/admin/settings/bridge.shadow_id_dms", {"value": True}).status_code == 403
    assert call("/widget/api/admin/settings/bridge.shadow_id_dms/reset", {}).status_code == 403


def test_startup_captures_are_reapplied(client, homeserver) -> None:
    call = widget(client, homeserver, user=ADMIN)
    assert call("/widget/api/admin/settings/bridge.client_name", {"value": "Renamed"}).status_code == 200
    assert client.app.state.signer.client_tag[1] == "Renamed"
    settle(client)
    assert call("/widget/api/admin/settings/appservice.bot_display_name", {"value": "Nostr Bot"}).status_code == 200
    assert homeserver.profiles["@nostrbot:example.org"]["displayname"] == "Nostr Bot"
    assert call("/widget/api/admin/settings/spam.connections.burst", {"value": 3}).status_code == 200
    assert client.app.state.spam._connections.spec.burst == 3


def test_the_kill_switch_locks_the_editor(repository, homeserver, relay_pool) -> None:
    config = parse_config(raw_config(bridge={"admins": [ADMIN], "allow_db_setting_overrides": False}))
    with make_client(config, repository, homeserver, relay_pool) as client:
        call = widget(client, homeserver, user=ADMIN)
        data = call("/widget/api/admin/settings").json()
        assert data["overrides_enabled"] is False
        assert not any(s["editable"] for s in data["settings"])
        assert call("/widget/api/admin/settings/bridge.shadow_id_dms", {"value": True}).status_code == 400
        assert call("/widget/api/admin/settings/bridge.shadow_id_dms/reset", {}).status_code == 400
        assert run(client, client.app.state.repository.list_state, "setting:") == []


def test_stored_settings_apply_at_startup(repository, homeserver, relay_pool) -> None:
    config = parse_config(raw_config())

    async def store():
        await bridge_settings.apply_override(repository, "spam.inbound_policy", "web_of_trust")

    import asyncio

    asyncio.run(store())
    with make_client(config, repository, homeserver, relay_pool) as client:
        assert client.app.state.config.spam.inbound_policy == "web_of_trust"
        assert client.app.state.spam.config.inbound_policy == "web_of_trust"
        assert client.app.state.base_config is config
