# Nintendo Video SpotPass server

Serves archived Nintendo Video episodes to patched 3DS clients (see `luma/` IPS patches pointing at `video.mariocube.com`).

- `server.py` – HTTP server: policylist, `CHECK`, and BOSS containers for tasks `ESE_MD1-4`, `ESP_MD1-4`, `EWP_MD1-4`, `ESJ_MD1-4`. Rotates every task to its next-older episode weekly (wrapping to newest).
- `manage.py` – `fetch`, `import-boss`, `status`, `rotate`, `set`, `verify`.
- `library.py` – episode index, rotation state (`state.json`), container cache (`cache/`).
- `nvsp.py` / `boss.py` – payload builder and BOSS encryption.
- `luma/titles/` – Luma3DS `code.ips` patches for the USA (`000400000004AA00`) and Europe (`000400000004AB00`) Nintendo Video titles that repoint the app's policylist URL at this server.
- `deploy/nintendovideo.service` – systemd unit (expects the code in `/opt/nintendovideo`).

Episodes are stored as raw MOFLEX in `content/{prefix}/slot{N}/`; titles come from the archive's `titles.txt`. Each container is built once per rotation, given a fresh Nintendo Video (NVSP) header at publish time: release = the moment it is published, expiration = 7 days later, and cached on disk.

## Setup

```
python3 manage.py fetch
systemctl enable --now nintendovideo
python3 manage.py status
```

Requires Python 3.10+ and `pycryptodome` (or `pycryptodomex`).

### BOSS key

The BOSS content AES key (3DS keyslot `0x38`) is **not** included in this repository. Supply it as 32 hex digits either in a `boss.key` file next to `boss.py`, or in the `NINTENDOVIDEO_BOSS_KEY` environment variable (`NINTENDOVIDEO_BOSS_KEY_FILE` points at a different file). `boss.key` is git-ignored.

### What is not in the repo

Downloaded episodes (`content/`), built containers (`cache*/`), banner/thumbnail images (`assets/`), rotation state (`state.json`) and any TLS/client key material are intentionally left out; `manage.py fetch` recreates the content.

## Credits

- **OniOkami666** for their help with this project. Related work: [Nintendo-Video-research-project/Nintendo-Video-test-server](https://github.com/Nintendo-Video-research-project/Nintendo-Video-test-server), an experimental test server for distributing Nintendo Video files.
- [3dbrew](https://www.3dbrew.org/wiki/SpotPass) for the SpotPass / BOSS container documentation.
- The archive.org "nintendo-video-moflex-archive" preservation of the episodes that `manage.py fetch` downloads.
