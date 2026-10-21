"""Matrix AppService registration file generator.

The homeserver needs a registration YAML describing this bridge: its
tokens, the users it owns, and the URL to push events to. This module
builds that file from ``config.yaml`` so the two never drift apart. How
the homeserver loads it differs by implementation (Synapse:
``app_service_config_files`` in homeserver.yaml); see INSTALL.md.

Usage::

    python -m bridge.appservice config.yaml appservice-registration.yaml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from bridge.config import BridgeConfig, load_config


def build_registration(config: BridgeConfig) -> dict[str, Any]:
    """The registration document as a plain dict, ready to dump as YAML."""
    section = config.appservice
    return {
        "id": section.id,
        "url": config.bridge.resolved_internal_base_url(),
        "as_token": section.as_token,
        "hs_token": section.hs_token,
        "sender_localpart": section.bot_localpart,
        "rate_limited": False,
        "namespaces": {
            # Homeservers match these from the start of the MXID. The bot's
            # pattern ends at the colon so it can't also claim real users
            # whose names merely begin with the bot's (@nostrbotfan).
            "users": [
                {"exclusive": True, "regex": f"@{re.escape(section.user_prefix)}.*"},
                {"exclusive": True, "regex": f"@{re.escape(section.bot_localpart)}:.*"},
            ],
            "aliases": [],
            "rooms": [],
        },
    }


def write_registration(config: BridgeConfig, output_path: str | Path) -> Path:
    path = Path(output_path)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(build_registration(config), fh, default_flow_style=False, sort_keys=False)
    return path


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print("Usage: python -m bridge.appservice [config.yaml] [output_path]", file=sys.stderr)
        return 2
    config = load_config(argv[0] if argv else None)
    written = write_registration(config, argv[1] if len(argv) == 2 else config.appservice.registration_path)
    print(f"Wrote AppService registration to {written}")
    print("Register it with your homeserver (see INSTALL.md), then restart the homeserver.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
