"""Nintendo Video payload (the plaintext inside a BOSS container). Little-endian.

  header   7*u32 + u32 per banner:
           0, hdr_end, mv_start, mv_end, thumbnail_len, 0, num_banners, banner_offsets...
  movie    u32 0x248, video_id[0x20], release[8], expiration[8],
           title utf16[0x78], selected_banner u8, 0xFF, age u8, pad[5],
           video_len u32, description utf16[0x190],
           banner_ids[0x20 * num_banners], moflex, align4
  thumb    JPEG (starts at mv_end), align4
  banners  u32 0x16C, id[0x20], pad[0x10], time_priority[8], url[0x100],
           rgba[4], text utf16[0x28], image_len u32, JPEG, align4

Verified against a retail-derived container (round-trips byte for byte apart
from strings that filled their field without a terminator).
"""
import struct
from dataclasses import dataclass, field
from datetime import datetime

MV_LEN = 0x248
BANNER_META_LEN = 0x16C


def _pad4(b: bytes) -> bytes:
    return b + bytes(-len(b) % 4)


def _str(s: str, size: int, enc: str) -> bytes:
    raw = s.encode(enc)
    unit = 2 if enc.startswith("utf-16") else 1
    raw = raw[: size - unit]  # always keep a terminator
    raw = raw[: len(raw) - len(raw) % unit]
    return raw.ljust(size, b"\x00")


def _read_str(data: bytes, off: int, size: int, enc: str) -> str:
    raw = data[off:off + size]
    if enc.startswith("utf-16"):
        end = next((i for i in range(0, size, 2) if raw[i:i + 2] == b"\x00\x00"), size)
    else:
        end = raw.find(b"\x00") if b"\x00" in raw else size
    return raw[:end].decode(enc, errors="replace")


def _dt(d: datetime) -> bytes:
    return struct.pack("<HBBBBBx", d.year, d.month, d.day, d.hour, d.minute, d.second)


def _read_dt(data: bytes, off: int) -> datetime:
    return datetime(*struct.unpack_from("<HBBBBB", data, off))


@dataclass
class Banner:
    banner_id: str
    image: bytes
    url: str = ""
    text: str = ""
    color: bytes = b"\xff\xff\xff\x00"
    time_priority: bytes = b"\x00\x01\x00\x00\x00\x00\x00\x00"


@dataclass
class Payload:
    video_id: str
    title: str
    description: str
    release: datetime
    expiration: datetime
    video: bytes
    thumbnail: bytes
    banners: list[Banner] = field(default_factory=list)
    age_restriction: int = 0


def build(p: Payload) -> bytes:
    n = len(p.banners)
    mv_start = 7 * 4 + 4 * n

    mv = struct.pack("<I", MV_LEN)
    mv += _str(p.video_id, 0x20, "ascii")
    mv += _dt(p.release) + _dt(p.expiration)
    mv += _str(p.title, 0x78, "utf-16-le")
    mv += bytes([n, 0xFF, p.age_restriction]) + bytes(5)
    mv += struct.pack("<I", len(p.video))
    mv += _str(p.description, 0x190, "utf-16-le")
    assert len(mv) == MV_LEN
    mv += b"".join(_str(b.banner_id, 0x20, "ascii") for b in p.banners)
    mv = _pad4(mv + p.video)  # mv_start is 4-aligned so local alignment suffices

    mv_end = mv_start + len(mv)
    thumb = _pad4(p.thumbnail)

    blobs, offsets, cur = [], [], mv_end + len(thumb)
    for b in p.banners:
        blob = struct.pack("<I", BANNER_META_LEN)
        blob += _str(b.banner_id, 0x20, "ascii") + bytes(0x10)
        blob += b.time_priority[:8].ljust(8, b"\x00")
        blob += _str(b.url, 0x100, "ascii")
        blob += b.color[:4]
        blob += _str(b.text, 0x28, "utf-16-le")
        blob = _pad4(blob + struct.pack("<I", len(b.image)) + b.image)
        offsets.append(cur)
        cur += len(blob)
        blobs.append(blob)

    hdr = struct.pack(f"<7I{n}I", 0, mv_start, mv_start, mv_end, len(p.thumbnail), 0, n, *offsets)
    return hdr + mv + thumb + b"".join(blobs)


def parse(data: bytes) -> Payload:
    _, hdr_end, mv_start, mv_end, thumb_len, _, n = struct.unpack_from("<7I", data, 0)
    offsets = struct.unpack_from(f"<{n}I", data, 28)
    o = mv_start
    if struct.unpack_from("<I", data, o)[0] != MV_LEN:
        raise ValueError("bad movie header")
    video_id = _read_str(data, o + 4, 0x20, "ascii")
    release, expiration = _read_dt(data, o + 0x24), _read_dt(data, o + 0x2C)
    title = _read_str(data, o + 0x34, 0x78, "utf-16-le")
    age = data[o + 0xAE]
    video_len = struct.unpack_from("<I", data, o + 0xB4)[0]
    description = _read_str(data, o + 0xB8, 0x190, "utf-16-le")
    vo = o + MV_LEN + 0x20 * n
    banners = []
    for bo in offsets:
        img_len = struct.unpack_from("<I", data, bo + 0x168)[0]
        banners.append(Banner(
            banner_id=_read_str(data, bo + 4, 0x20, "ascii"),
            time_priority=data[bo + 0x34:bo + 0x3C],
            url=_read_str(data, bo + 0x3C, 0x100, "ascii"),
            color=data[bo + 0x13C:bo + 0x140],
            text=_read_str(data, bo + 0x140, 0x28, "utf-16-le"),
            image=data[bo + 0x16C:bo + 0x16C + img_len],
        ))
    return Payload(video_id, title, description, release, expiration,
                   data[vo:vo + video_len], data[mv_end:mv_end + thumb_len], banners, age)
