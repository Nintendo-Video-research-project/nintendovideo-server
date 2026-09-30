"""Episode library, rotation state and on-disk BOSS cache.

content/{prefix}/slot{N}/{prefix}{N}.{date}.{a}.{b}.moflex   raw MOFLEX video
content/{prefix}/titles.txt                                   "{stem}.boss: Title"
state.json                                                    current episode per task
cache/{task}.{since}.boss                                     encrypted container served
"""
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boss
import nvsp

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content"
CACHE = ROOT / "cache"
STATE_FILE = ROOT / "state.json"
ASSETS = ROOT / "assets"

REGIONS = {  # task prefix -> title id
    "ESE_MD": 0x000400000004AA00,  # USA
    "ESP_MD": 0x000400000004AB00,  # Europe
    "EWP_MD": 0x000400000004AB00,  # Europe (English/PAL-wide)
    "ESJ_MD": 0x000400000004A900,  # Japan
}
SLOTS = (1, 2, 3, 4)
TASKS = [f"{p}{s}" for p in REGIONS for s in SLOTS]

ROTATE_EVERY = timedelta(days=7)
# The NVSP header is stamped at publish time (when a task rotates onto an
# episode): release = publish moment, expiration = publish moment + 7 days.
EXPIRES_AFTER = timedelta(days=7)

NAME_RE = re.compile(r"^(?P<prefix>[A-Z]{3}_MD)(?P<slot>\d)\.(?P<date>\d{4}-\d{2}-\d{2})"
                     r"\.(?P<a>\d+)\.(?P<b>\d+)$")

_lock = threading.Lock()


@dataclass(frozen=True)
class Episode:
    path: Path
    prefix: str
    slot: int
    date: str
    title: str

    @property
    def task(self) -> str:
        return f"{self.prefix}{self.slot}"

    @property
    def stem(self) -> str:
        return self.path.name.removesuffix(".moflex")


def parse_name(stem: str):
    m = NAME_RE.match(stem)
    return m.groupdict() if m else None


def load_titles(prefix: str) -> dict[str, str]:
    f = CONTENT / prefix / "titles.txt"
    titles = {}
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            key, sep, title = line.partition(": ")
            if sep:
                titles[key.removesuffix(".boss").strip()] = title.strip()
    return titles


def episodes(task: str) -> list[Episode]:
    """Episodes for a task, oldest first, with re-uploads of the same video collapsed."""
    prefix, slot = task[:-1], int(task[-1])
    d = CONTENT / prefix / f"slot{slot}"
    titles = load_titles(prefix)
    eps, seen = [], set()
    for p in sorted(d.glob("*.moflex")):
        stem = p.name.removesuffix(".moflex")
        info = parse_name(stem)
        if not info:
            continue
        title = titles.get(stem, "")
        key = (title, p.stat().st_size)
        if title and key in seen:
            continue
        seen.add(key)
        eps.append(Episode(p, prefix, slot, info["date"], title))
    return eps


# ── state ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp, STATE_FILE)


def current(state: dict, task: str) -> Episode | None:
    entry = state.get(task)
    if not entry:
        return None
    return next((e for e in episodes(task) if e.path.name == entry["file"]), None)


def set_episode(state: dict, task: str, ep: Episode, now: int | None = None):
    state[task] = {"file": ep.path.name, "since": int(now or time.time())}
    log.info("%s -> %s (%s)", task, ep.path.name, ep.title or "untitled")


def advance(state: dict, task: str, now: int | None = None) -> Episode | None:
    """Move a task to the next-newer episode, wrapping to the oldest."""
    eps = episodes(task)
    if not eps:
        return None
    cur = state.get(task, {}).get("file")
    names = [e.path.name for e in eps]
    idx = (names.index(cur) + 1) % len(eps) if cur in names else 0
    set_episode(state, task, eps[idx], now)
    return eps[idx]


def tick(now: float | None = None) -> bool:
    """Initialise empty tasks and rotate stale ones. Returns True if anything changed."""
    now = now or time.time()
    with _lock:
        state = load_state()
        changed = False
        for task in TASKS:
            entry = state.get(task)
            if entry is None or current(state, task) is None:
                changed |= advance(state, task, int(now)) is not None
            elif now - entry["since"] >= ROTATE_EVERY.total_seconds():
                changed |= advance(state, task, int(now)) is not None
        if changed:
            save_state(state)
            prune_cache(state)
        return changed


# ── container cache ──────────────────────────────────────────────────────────

def _payload(ep: Episode, since: int) -> bytes:
    rel = datetime.fromtimestamp(since, timezone.utc).replace(tzinfo=None)
    tp = ASSETS / "thumbs" / (ep.stem + ".jpg")  # per-episode keyframe (make_thumbs.py), placeholder as fallback
    thumb = (tp if tp.exists() else ASSETS / "thumbnail.jpg").read_bytes()
    bp = ASSETS / "banners" / (ep.stem + ".jpg")  # per-episode keyframe banner, placeholder as fallback
    banner = (bp if bp.exists() else ASSETS / "banner.jpg").read_bytes()
    vid = f"{ep.prefix[:3]}{ep.slot}{ep.date.replace('-', '')}"
    title = ep.title or f"Nintendo Video {ep.date}"
    return nvsp.build(nvsp.Payload(
        video_id="MD" + vid,
        title=title,
        description=f"{title}\nOriginally released {ep.date}.",
        release=rel,
        expiration=rel + EXPIRES_AFTER,
        video=ep.path.read_bytes(),
        thumbnail=thumb,
        banners=[nvsp.Banner(banner_id="ID" + vid, image=banner)],
    ))


def container_for(task: str) -> Path | None:
    """Path to the encrypted container currently served for `task`, building it if needed."""
    with _lock:
        state = load_state()
        ep = current(state, task)
        if ep is None:
            return None
        since = state[task]["since"]
        out = CACHE / f"{task}.{since}.boss"
        if out.exists():
            return out
        CACHE.mkdir(exist_ok=True)
        data = boss.encrypt(_payload(ep, since), title_id=REGIONS[ep.prefix],
                            ns_data_id=ep.slot,
                            version=int(datetime.fromtimestamp(since, timezone.utc).strftime("%Y%m%d%H"))
                            + list(REGIONS).index(ep.prefix) * len(SLOTS) + (ep.slot - 1),
                            timestamp=since)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, out)
        log.info("built %s from %s", out.name, ep.path.name)
        return out


def prune_cache(state: dict):
    keep = {f"{t}.{v['since']}.boss" for t, v in state.items()}
    for f in CACHE.glob("*"):
        if f.name not in keep:
            f.unlink(missing_ok=True)
