"""BOSS (SpotPass) container encryption for 3DS content.

Layout, all big-endian (https://www.3dbrew.org/wiki/SpotPass#Content_Container):
  0x000 outer header (0x28, plaintext)
  0x028 content header (0x132)            -- encrypted from here on
  0x15A payload content header (0x13C)
  0x296 payload
"""
import hashlib
import os
import secrets
import struct
from pathlib import Path

try:
    from Cryptodome.Cipher import AES
except ImportError:
    from Crypto.Cipher import AES

_key_cache: bytes | None = None


def boss_key() -> bytes:
    """The 16-byte BOSS content AES key (3DS keyslot 0x38).

    It is deliberately not part of this repository. Provide it either as 32 hex
    digits in the NINTENDOVIDEO_BOSS_KEY environment variable, or in a file
    (default: `boss.key` next to this module, override with
    NINTENDOVIDEO_BOSS_KEY_FILE).
    """
    global _key_cache
    if _key_cache is None:
        text = os.environ.get("NINTENDOVIDEO_BOSS_KEY", "")
        if not text:
            path = Path(os.environ.get("NINTENDOVIDEO_BOSS_KEY_FILE") or Path(__file__).with_name("boss.key"))
            if path.is_file():
                text = path.read_text()
        try:
            key = bytes.fromhex("".join(text.split()))
        except ValueError:
            key = b""
        if len(key) != 16:
            raise RuntimeError("BOSS key missing: put 32 hex digits in boss.key or NINTENDOVIDEO_BOSS_KEY")
        _key_cache = key
    return _key_cache

OUTER_SIZE = 0x28
CONTENT_HEADER_SIZE = 0x132
PAYLOAD_HEADER_SIZE = 0x13C
HEADER_SIZE = OUTER_SIZE + CONTENT_HEADER_SIZE + PAYLOAD_HEADER_SIZE

# Value seen in retail Nintendo Video containers.
DATATYPE_NV = 0x00010001


def _ctr(iv12: bytes, data: bytes) -> bytes:
    iv = int.from_bytes(iv12 + b"\x00\x00\x00\x01", "big")
    return AES.new(boss_key(), AES.MODE_CTR, nonce=b"", initial_value=iv).encrypt(data)


def encrypt(payload: bytes, *, title_id: int, ns_data_id: int, version: int,
            timestamp: int, datatype: int = DATATYPE_NV) -> bytes:
    """Wrap an already-built NVSP payload in an encrypted BOSS container.

    `timestamp` (unix seconds) goes in the outer header's release field; BOSS
    compares it to decide whether a download is new content.
    """
    ch = bytearray(0x12)
    ch[0] = 0x80
    struct.pack_into(">H", ch, 0x10, 1)  # payload count
    content_header = bytes(ch) + hashlib.sha256(bytes(ch) + b"\x00\x00").digest() + bytes(0x100)

    pch = struct.pack(">QIIIII", title_id, 0, datatype, len(payload), ns_data_id, version)
    pch_hash = hashlib.sha256(pch + b"\x00\x00" + payload).digest()
    payload_header = pch + pch_hash + bytes(0x100)

    iv = secrets.token_bytes(12)
    body = content_header + payload_header + payload
    outer = struct.pack(">4sIIQHHHH12s", b"boss", 0x10001, OUTER_SIZE + len(body),
                        timestamp, 1, 0, 2, 2, iv)
    return outer + _ctr(iv, body)


def decrypt(data: bytes) -> bytes:
    """Return the decrypted body (content header + payload header + payload)."""
    if data[:4] != b"boss":
        raise ValueError("not a BOSS container")
    return _ctr(data[0x1C:0x28], data[OUTER_SIZE:])


def extract_payload(data: bytes) -> bytes:
    return decrypt(data)[CONTENT_HEADER_SIZE + PAYLOAD_HEADER_SIZE:]
