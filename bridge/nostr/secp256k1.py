"""BIP340 Schnorr signatures over secp256k1: what every Nostr event is
signed with.

Pure Python, so the bridge has no native dependency to build. coincurve
(libsecp256k1) is used instead when it is installed, since it is much
faster. It is optional because it has no build for every Python version
the bridge runs on.

The pure-Python path follows the BIP340 reference implementation and is
checked against its test vectors (``tests/vectors/bip340-test-vectors.csv``).
Two speed-ups keep it usable for a relay:

- Multiples of the generator use a precomputed table (64 four-bit windows),
  so ``k*G`` is 64 point additions and no doublings.
- Other points use width-5 NAF in Jacobian coordinates, so ``e*P`` needs no
  modular inversion until the very end.

Side channels: Python big-integer arithmetic isn't constant-time. Signing
happens on the bridge's own server with keys it already holds, so timing
leaks only to someone who can already measure that machine closely.
Install coincurve where that matters.
"""

from __future__ import annotations

import hashlib
import secrets
from functools import lru_cache

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

try:  # pragma: no cover - depends on the environment
    from coincurve import PrivateKey as _CCPrivateKey
    from coincurve import PublicKeyXOnly as _CCPublicKeyXOnly

    HAVE_COINCURVE = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_COINCURVE = False


class InvalidKey(ValueError):
    """A secret or public key that isn't a valid secp256k1 key."""


# -- field and point arithmetic ------------------------------------------
#
# Affine points are (x, y) tuples, Jacobian points (X, Y, Z) with
# x = X/Z^2, y = Y/Z^3. The point at infinity is None (affine) or Z == 0.

_INFINITY = (0, 1, 0)


def _jac_double(p: tuple[int, int, int]) -> tuple[int, int, int]:
    x, y, z = p
    if z == 0 or y == 0:
        return _INFINITY
    yy = y * y % P
    s = 4 * x * yy % P
    m = 3 * x * x % P
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * yy * yy) % P
    z3 = 2 * y * z % P
    return (x3, y3, z3)


def _jac_add(p: tuple[int, int, int], q: tuple[int, int, int]) -> tuple[int, int, int]:
    x1, y1, z1 = p
    x2, y2, z2 = q
    if z1 == 0:
        return q
    if z2 == 0:
        return p
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    if u1 == u2:
        return _jac_double(p) if s1 == s2 else _INFINITY
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hhh) % P
    z3 = h * z1 * z2 % P
    return (x3, y3, z3)


def _jac_add_affine(p: tuple[int, int, int], x2: int, y2: int) -> tuple[int, int, int]:
    """``p`` plus the affine point (x2, y2): cheaper than a full addition."""
    x1, y1, z1 = p
    if z1 == 0:
        return (x2, y2, 1)
    z1z1 = z1 * z1 % P
    u2 = x2 * z1z1 % P
    s2 = y2 * z1 * z1z1 % P
    if x1 == u2:
        return _jac_double(p) if y1 == s2 else _INFINITY
    h = (u2 - x1) % P
    r = (s2 - y1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = x1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - y1 * hhh) % P
    z3 = h * z1 % P
    return (x3, y3, z3)


def _to_affine(p: tuple[int, int, int]) -> tuple[int, int] | None:
    x, y, z = p
    if z == 0:
        return None
    zi = pow(z, -1, P)
    zi2 = zi * zi % P
    return (x * zi2 % P, y * zi2 * zi % P)


@lru_cache(maxsize=1)
def _generator_table() -> list[list[tuple[int, int] | None]]:
    """``table[i][j]`` = ``j * 16**i * G`` (affine), for the 64 nibbles of
    a 256-bit scalar. Built once, on first use (a few milliseconds)."""
    table: list[list[tuple[int, int] | None]] = []
    base = (_GX, _GY, 1)
    for _ in range(64):
        row_jac = [_INFINITY]
        for _ in range(15):
            row_jac.append(_jac_add(row_jac[-1], base))
        table.append([_to_affine(point) for point in row_jac])
        for _ in range(4):
            base = _jac_double(base)
    return table


def _mul_g(k: int) -> tuple[int, int, int]:
    table = _generator_table()
    acc = _INFINITY
    i = 0
    while k:
        nibble = k & 15
        if nibble:
            point = table[i][nibble]
            assert point is not None
            acc = _jac_add_affine(acc, point[0], point[1])
        k >>= 4
        i += 1
    return acc


def _wnaf(k: int, width: int = 5) -> list[int]:
    digits = []
    half = 1 << (width - 1)
    full = 1 << width
    while k:
        if k & 1:
            d = k % full
            if d >= half:
                d -= full
            k -= d
        else:
            d = 0
        digits.append(d)
        k >>= 1
    return digits


def _mul_point(k: int, x: int, y: int) -> tuple[int, int, int]:
    """``k * (x, y)`` for an arbitrary point."""
    base = (x, y, 1)
    double = _jac_double(base)
    odd = [base]  # 1P, 3P, ..., 15P
    for _ in range(7):
        odd.append(_jac_add(odd[-1], double))
    acc = _INFINITY
    for digit in reversed(_wnaf(k)):
        acc = _jac_double(acc)
        if digit > 0:
            acc = _jac_add(acc, odd[digit >> 1])
        elif digit < 0:
            px, py, pz = odd[(-digit) >> 1]
            acc = _jac_add(acc, (px, P - py, pz))
    return acc


def _lift_x(x: int) -> tuple[int, int] | None:
    """The point with x-coordinate ``x`` and an even y (BIP340 ``lift_x``)."""
    if x >= P:
        return None
    c = (pow(x, 3, P) + 7) % P
    y = pow(c, (P + 1) // 4, P)
    if y * y % P != c:
        return None
    return (x, y if y % 2 == 0 else P - y)


# -- BIP340 --------------------------------------------------------------


@lru_cache(maxsize=8)
def _tag_prefix(tag: str) -> bytes:
    digest = hashlib.sha256(tag.encode()).digest()
    return digest + digest


def tagged_hash(tag: str, message: bytes) -> bytes:
    return hashlib.sha256(_tag_prefix(tag) + message).digest()


def _secret_int(secret_key: bytes) -> int:
    if len(secret_key) != 32:
        raise InvalidKey("A secret key is 32 bytes")
    d = int.from_bytes(secret_key, "big")
    if not 1 <= d < N:
        raise InvalidKey("Secret key out of range")
    return d


def generate_secret_key() -> bytes:
    while True:
        candidate = secrets.token_bytes(32)
        if 1 <= int.from_bytes(candidate, "big") < N:
            return candidate


def public_key(secret_key: bytes) -> bytes:
    """The 32-byte x-only public key (what Nostr calls ``pubkey``)."""
    if HAVE_COINCURVE:  # pragma: no cover
        _secret_int(secret_key)
        return _CCPrivateKey(secret_key).public_key_xonly.format()
    point = _to_affine(_mul_g(_secret_int(secret_key)))
    assert point is not None
    return point[0].to_bytes(32, "big")


def is_valid_public_key(pubkey: bytes) -> bool:
    return len(pubkey) == 32 and _lift_x(int.from_bytes(pubkey, "big")) is not None


def sign(message: bytes, secret_key: bytes, aux_rand: bytes | None = None) -> bytes:
    """A 64-byte BIP340 signature of ``message``. ``aux_rand`` defaults to
    fresh randomness, as BIP340 recommends; pass bytes only for test
    vectors."""
    if aux_rand is None:
        aux_rand = secrets.token_bytes(32)
    if len(aux_rand) != 32:
        raise ValueError("aux_rand must be 32 bytes")
    if HAVE_COINCURVE and len(message) == 32:  # pragma: no cover
        _secret_int(secret_key)
        return _CCPrivateKey(secret_key).sign_schnorr(message, aux_rand)
    d0 = _secret_int(secret_key)
    point = _to_affine(_mul_g(d0))
    assert point is not None
    px, py = point
    d = d0 if py % 2 == 0 else N - d0
    t = (d ^ int.from_bytes(tagged_hash("BIP0340/aux", aux_rand), "big")).to_bytes(32, "big")
    pk = px.to_bytes(32, "big")
    k0 = int.from_bytes(tagged_hash("BIP0340/nonce", t + pk + message), "big") % N
    if k0 == 0:  # pragma: no cover - probability 2^-256
        raise ValueError("Signing failed; try again")
    r_point = _to_affine(_mul_g(k0))
    assert r_point is not None
    rx, ry = r_point
    k = k0 if ry % 2 == 0 else N - k0
    rb = rx.to_bytes(32, "big")
    e = int.from_bytes(tagged_hash("BIP0340/challenge", rb + pk + message), "big") % N
    signature = rb + ((k + e * d) % N).to_bytes(32, "big")
    if not verify(message, pk, signature):  # pragma: no cover - defensive, as in the reference
        raise ValueError("Signature failed to verify")
    return signature


def verify(message: bytes, pubkey: bytes, signature: bytes) -> bool:
    """Whether ``signature`` is a valid BIP340 signature of ``message`` by
    the x-only ``pubkey``. Never raises on malformed input."""
    if len(pubkey) != 32 or len(signature) != 64:
        return False
    if HAVE_COINCURVE and len(message) == 32:  # pragma: no cover
        try:
            return _CCPublicKeyXOnly(pubkey).verify(signature, message)
        except Exception:
            return False
    point = _lift_x(int.from_bytes(pubkey, "big"))
    if point is None:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(tagged_hash("BIP0340/challenge", signature[:32] + pubkey + message), "big") % N
    combined = _jac_add(_mul_g(s), _mul_point((N - e) % N, point[0], point[1]))
    result = _to_affine(combined)
    return result is not None and result[1] % 2 == 0 and result[0] == r


def ecdh_x(secret_key: bytes, pubkey: bytes) -> bytes:
    """The x-coordinate of ``secret * lift_x(pubkey)``: the shared secret
    NIP-44 builds its conversation key from."""
    point = _lift_x(int.from_bytes(pubkey, "big")) if len(pubkey) == 32 else None
    if point is None:
        raise InvalidKey("Not a valid public key")
    shared = _to_affine(_mul_point(_secret_int(secret_key), point[0], point[1]))
    if shared is None:  # pragma: no cover - impossible for a valid key
        raise InvalidKey("Degenerate shared point")
    return shared[0].to_bytes(32, "big")
