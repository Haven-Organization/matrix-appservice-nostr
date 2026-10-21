"""NIP-19: the bech32 forms Nostr shows to people (``npub1…``, ``note1…``,
``nprofile1…``, ...).

These are for display and input only; the protocol itself always uses hex.
Bech32 here is BIP173's original checksum (not bech32m), without BIP173's
90-character limit, which the TLV forms routinely exceed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CHARSET_REV = {c: i for i, c in enumerate(_CHARSET)}
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
# Generous, but bounded: nothing legitimate comes close.
_MAX_LENGTH = 5000

# TLV types
_TLV_SPECIAL = 0
_TLV_RELAY = 1
_TLV_AUTHOR = 2
_TLV_KIND = 3


class Nip19Error(ValueError):
    """Not a valid NIP-19 string. The message is safe to show the user."""


def _polymod(values: list[int]) -> int:
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GENERATOR[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: bytes | list[int], from_bits: int, to_bits: int, pad: bool) -> list[int]:
    acc = 0
    bits = 0
    out = []
    maxv = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise Nip19Error("Invalid data")
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        raise Nip19Error("Invalid padding")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    values = _convert_bits(data, 8, 5, True)
    polymod = _polymod(_hrp_expand(hrp) + values + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[v] for v in values + checksum)


def bech32_decode(text: str) -> tuple[str, bytes]:
    if len(text) > _MAX_LENGTH:
        raise Nip19Error("Too long")
    if text.lower() != text and text.upper() != text:
        raise Nip19Error("Mixed case")
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text):
        raise Nip19Error("Not a bech32 string")
    hrp = text[:pos]
    try:
        values = [_CHARSET_REV[c] for c in text[pos + 1 :]]
    except KeyError:
        raise Nip19Error("Invalid character") from None
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise Nip19Error("Bad checksum")
    return hrp, bytes(_convert_bits(values[:-6], 5, 8, False))


# -- the plain forms -----------------------------------------------------


def _encode_hex32(hrp: str, value: str) -> str:
    raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise Nip19Error("Expected 32 bytes")
    return bech32_encode(hrp, raw)


def _decode_hex32(expected_hrp: str, text: str) -> str:
    hrp, data = bech32_decode(text)
    if hrp != expected_hrp:
        raise Nip19Error(f"Expected {expected_hrp}, got {hrp}")
    if len(data) != 32:
        raise Nip19Error("Expected 32 bytes")
    return data.hex()


def npub_encode(pubkey_hex: str) -> str:
    return _encode_hex32("npub", pubkey_hex)


def npub_decode(text: str) -> str:
    return _decode_hex32("npub", text)


def nsec_encode(secret_key: bytes) -> str:
    if len(secret_key) != 32:
        raise Nip19Error("Expected 32 bytes")
    return bech32_encode("nsec", secret_key)


def nsec_decode(text: str) -> bytes:
    return bytes.fromhex(_decode_hex32("nsec", text))


def note_encode(event_id_hex: str) -> str:
    return _encode_hex32("note", event_id_hex)


def note_decode(text: str) -> str:
    return _decode_hex32("note", text)


# -- the TLV forms -------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    pubkey: str
    relays: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventPointer:
    id: str
    relays: tuple[str, ...] = ()
    author: str | None = None
    kind: int | None = None


@dataclass(frozen=True)
class Address:
    identifier: str
    pubkey: str
    kind: int
    relays: tuple[str, ...] = field(default_factory=tuple)


def _tlv(entries: list[tuple[int, bytes]]) -> bytes:
    out = bytearray()
    for tlv_type, value in entries:
        if len(value) > 255:
            raise Nip19Error("TLV value too long")
        out += bytes((tlv_type, len(value))) + value
    return bytes(out)


def _parse_tlv(data: bytes) -> dict[int, list[bytes]]:
    result: dict[int, list[bytes]] = {}
    i = 0
    while i < len(data):
        if i + 2 > len(data):
            raise Nip19Error("Truncated TLV")
        tlv_type, length = data[i], data[i + 1]
        value = data[i + 2 : i + 2 + length]
        if len(value) != length:
            raise Nip19Error("Truncated TLV")
        result.setdefault(tlv_type, []).append(value)
        i += 2 + length
    return result


def _relays(values: list[bytes]) -> tuple[str, ...]:
    return tuple(v.decode("utf-8", errors="replace") for v in values)


def nprofile_encode(profile: Profile) -> str:
    entries = [(_TLV_SPECIAL, bytes.fromhex(profile.pubkey))]
    entries += [(_TLV_RELAY, relay.encode()) for relay in profile.relays]
    return bech32_encode("nprofile", _tlv(entries))


def nevent_encode(pointer: EventPointer) -> str:
    entries = [(_TLV_SPECIAL, bytes.fromhex(pointer.id))]
    entries += [(_TLV_RELAY, relay.encode()) for relay in pointer.relays]
    if pointer.author:
        entries.append((_TLV_AUTHOR, bytes.fromhex(pointer.author)))
    if pointer.kind is not None:
        entries.append((_TLV_KIND, pointer.kind.to_bytes(4, "big")))
    return bech32_encode("nevent", _tlv(entries))


def naddr_encode(address: Address) -> str:
    entries = [(_TLV_SPECIAL, address.identifier.encode())]
    entries += [(_TLV_RELAY, relay.encode()) for relay in address.relays]
    entries.append((_TLV_AUTHOR, bytes.fromhex(address.pubkey)))
    entries.append((_TLV_KIND, address.kind.to_bytes(4, "big")))
    return bech32_encode("naddr", _tlv(entries))


def _one32(tlv: dict[int, list[bytes]], tlv_type: int, what: str) -> str:
    values = tlv.get(tlv_type) or []
    if not values or len(values[0]) != 32:
        raise Nip19Error(f"Missing or invalid {what}")
    return values[0].hex()


def _kind(tlv: dict[int, list[bytes]]) -> int | None:
    values = tlv.get(_TLV_KIND) or []
    if not values:
        return None
    if len(values[0]) != 4:
        raise Nip19Error("Invalid kind")
    return int.from_bytes(values[0], "big")


def decode(text: str) -> str | bytes | Profile | EventPointer | Address:
    """Any NIP-19 string: ``npub``/``note`` give hex, ``nsec`` gives bytes,
    and the TLV forms give a ``Profile``/``EventPointer``/``Address``.
    Unknown TLV entries are ignored, as NIP-19 requires."""
    hrp, data = bech32_decode(text.strip())
    if hrp in ("npub", "note"):
        if len(data) != 32:
            raise Nip19Error("Expected 32 bytes")
        return data.hex()
    if hrp == "nsec":
        if len(data) != 32:
            raise Nip19Error("Expected 32 bytes")
        return data
    tlv = _parse_tlv(data)
    if hrp == "nprofile":
        return Profile(_one32(tlv, _TLV_SPECIAL, "public key"), _relays(tlv.get(_TLV_RELAY, [])))
    if hrp == "nevent":
        authors = tlv.get(_TLV_AUTHOR) or []
        author = authors[0].hex() if authors and len(authors[0]) == 32 else None
        return EventPointer(_one32(tlv, _TLV_SPECIAL, "event id"), _relays(tlv.get(_TLV_RELAY, [])), author, _kind(tlv))
    if hrp == "naddr":
        identifiers = tlv.get(_TLV_SPECIAL) or []
        if not identifiers:
            raise Nip19Error("Missing identifier")
        kind = _kind(tlv)
        if kind is None:
            raise Nip19Error("Missing kind")
        return Address(
            identifiers[0].decode("utf-8", errors="replace"),
            _one32(tlv, _TLV_AUTHOR, "author"),
            kind,
            _relays(tlv.get(_TLV_RELAY, [])),
        )
    raise Nip19Error(f"Unsupported NIP-19 type {hrp!r}")
