#!/usr/bin/env python3
"""Nintendo Video content management.

  fetch [--regions ...] [--workers N]   download MOFLEX + titles from archive.org
  import-boss DIR                      recover MOFLEX from old-style .boss files (removes them)
  status                               show what each task is serving
  rotate [TASK]                        advance TASK (or all) to the next episode now
  set TASK FILE                        pin TASK to a specific .moflex file name
  verify TASK                          decrypt and parse the container being served
"""
import argparse
import concurrent.futures
import logging
import re
import sys
import time
import urllib.request
from pathlib import Path

import boss
import library
import nvsp

ARCHIVE = "https://archive.org/download/nintendo-video-moflex-archive/moflex-files"
MOFLEX_MAGIC = b"L2"  # followed by stream-specific sync bytes

log = logging.getLogger("manage")


def _get(url: str) -> bytes:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except OSError as e:
            if attempt == 3:
                raise
            log.warning("%s: %s, retrying", url, e)
            time.sleep(5 * (attempt + 1))


def _slot_path(stem: str) -> Path | None:
    info = library.parse_name(stem)
    if not info:
        return None
    return library.CONTENT / info["prefix"] / f"slot{info['slot']}" / f"{stem}.moflex"


def fetch(args):
    for prefix in args.regions:
        (library.CONTENT / prefix).mkdir(parents=True, exist_ok=True)
        (library.CONTENT / prefix / "titles.txt").write_bytes(_get(f"{ARCHIVE}/{prefix}/titles.txt"))
        listing = _get(f"{ARCHIVE}/{prefix}/").decode()
        stems = sorted(set(re.findall(r'href="(?:[^"]*/)?([A-Z]{3}_MD\d[^"/]*)\.boss\.moflex"', listing)))
        todo = [s for s in stems if (p := _slot_path(s)) and not p.exists()]
        log.info("%s: %d episodes, %d to download", prefix, len(stems), len(todo))

        def one(stem):
            dest = _slot_path(stem)
            data = _get(f"{ARCHIVE}/{prefix}/{stem}.boss.moflex")
            if data[:2] != MOFLEX_MAGIC:
                log.error("%s: not a MOFLEX file", stem)
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            log.info("  %s", dest.name)

        with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
            list(pool.map(one, todo))


def import_boss(args):
    for f in sorted(Path(args.dir).rglob("*.boss")):
        stem = f.name.removesuffix(".boss")
        dest = _slot_path(stem)
        if not dest:
            log.warning("skipping %s: unrecognised name", f.name)
            continue
        if not dest.exists():
            video = boss.extract_payload(f.read_bytes())
            if video[:2] != MOFLEX_MAGIC:
                try:
                    video = nvsp.parse(video).video
                except Exception:
                    pass
            if video[:2] != MOFLEX_MAGIC:
                log.error("%s does not contain raw MOFLEX, leaving it", f.name)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(video)
        f.unlink()
        log.info("imported %s", dest.name)


def status(args):
    state = library.load_state()
    for task in library.TASKS:
        eps = library.episodes(task)
        ep = library.current(state, task)
        since = time.strftime("%Y-%m-%d %H:%M", time.gmtime(state[task]["since"])) if ep else "-"
        print(f"{task}: {len(eps):3d} eps  since {since}  {ep.path.name if ep else '(none)'}  "
              f"{ep.title if ep else ''}")


def rotate(args):
    state = library.load_state()
    for task in [args.task] if args.task else library.TASKS:
        library.advance(state, task)
    library.save_state(state)
    library.prune_cache(state)
    status(args)


def set_(args):
    state = library.load_state()
    ep = next((e for e in library.episodes(args.task) if e.path.name in (args.file, args.file + ".moflex")), None)
    if not ep:
        sys.exit(f"{args.file} is not an episode of {args.task}")
    library.set_episode(state, args.task, ep)
    library.save_state(state)
    library.prune_cache(state)


def verify(args):
    library.tick()
    f = library.container_for(args.task)
    if not f:
        sys.exit("no content")
    p = nvsp.parse(boss.extract_payload(f.read_bytes()))
    print(f"{f.name}: id={p.video_id} title={p.title!r} release={p.release} expires={p.expiration} "
          f"video={len(p.video)} magic_ok={p.video[:2] == MOFLEX_MAGIC} thumb={len(p.thumbnail)} "
          f"banners={len(p.banners)}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("--regions", nargs="+", default=list(library.REGIONS))
    p.add_argument("--workers", type=int, default=3)
    p.set_defaults(fn=fetch)
    p = sub.add_parser("import-boss")
    p.add_argument("dir")
    p.set_defaults(fn=import_boss)
    sub.add_parser("status").set_defaults(fn=status)
    p = sub.add_parser("rotate")
    p.add_argument("task", nargs="?", choices=library.TASKS)
    p.set_defaults(fn=rotate)
    p = sub.add_parser("set")
    p.add_argument("task", choices=library.TASKS)
    p.add_argument("file")
    p.set_defaults(fn=set_)
    p = sub.add_parser("verify")
    p.add_argument("task", choices=library.TASKS)
    p.set_defaults(fn=verify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
