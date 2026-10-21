"""NIP-05: ``name@domain`` handles.

The syntax rules shared by the bridge's own endpoint (``bridge.nip05_routes``)
and, later, by ID creation and the lookup of remote accounts' handles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# NIP-05: the local part may only use a-z, 0-9, '-', '_' and '.'.
NAME_RE = re.compile(r"^[a-z0-9._-]+$")
# What a name is checked against: generous (NIP-05 sets no limit), but
# bounded so a request can't make the bridge look up megabytes of name.
MAX_NAME_LENGTH = 64
# "_@domain" is the domain's own identity, shown as just "domain".
ROOT_NAME = "_"


def is_valid_name(name: str) -> bool:
    return 0 < len(name) <= MAX_NAME_LENGTH and NAME_RE.match(name) is not None


@dataclass(frozen=True)
class Identifier:
    name: str
    domain: str

    def __str__(self) -> str:
        return self.domain if self.name == ROOT_NAME else f"{self.name}@{self.domain}"


_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


def parse_identifier(text: str) -> Identifier | None:
    """``name@domain`` (or a bare ``domain``, meaning ``_@domain``), or
    ``None`` if it isn't one. Case-insensitive, as handles are typed by
    people; NIP-05 names are lowercase."""
    text = text.strip().lower()
    if text.startswith("@"):
        text = text[1:]
    name, at, domain = text.rpartition("@")
    if not at:
        name, domain = ROOT_NAME, text
    domain = domain.rstrip(".")
    if not is_valid_name(name) or not _DOMAIN_RE.match(domain):
        return None
    return Identifier(name, domain)
