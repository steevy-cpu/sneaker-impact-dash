# Operations runbook

How the dash runs in production on the station server ("aipc"). Architecture is in [architecture.md](architecture.md).

## Services

All are systemd **user** services (no sudo; linger is enabled, so they start at boot with nobody logged in). Source of truth: `deploy/systemd/` — `deploy/systemd/install.sh --check` shows whether `~/.config/systemd/user` still matches the repo.

| Unit | What | Port |
|------|------|------|
| `sneaker-dash-http` | uvicorn `backend.main:app` | 8000 |
| `sneaker-dash-https` | same app with TLS (`certs/dash-*.pem`); the station's browser camera needs a secure context | 8443 |
| `sneaker-dash-preview` | resident SAM3 worker (capture guide + engine segmentation) | 127.0.0.1:8766 |
| `ollama` | local VLM server | 11434 |
| `sneaker-dash-backup.timer` | nightly 02:30 data backup | — |
| `wifi-watchdog.timer` | every 2 min re-activates the station-link Wi-Fi if NetworkManager gave up | — |

```bash
systemctl --user restart sneaker-dash-http sneaker-dash-https     # after ANY backend or .env change (restart BOTH)
journalctl --user -u sneaker-dash-https -f                        # logs ([worker]/[outbox] lines included)
systemctl --user status sneaker-dash-preview ollama
```

Don't start uvicorn by hand with `nohup` — it collides with the services on the port. Frontend-only edits need no restart (bump the `?v=` on the changed JS/CSS). Restart when the queue is idle if you can: check `select status, count(*) from table_photos group by status` for `processing`.

## Safe maintenance

- **Pause new work** without touching captures or in-flight jobs: set `app_config` key `worker_paused` to `"1"` (back to `"0"` to resume). The worker checks it every poll.
- **One GPU, one VLM.** The worker refuses to claim while any photo is `processing`, because two concurrent engines over-subscribe the 16 GB GPU and wedge ollama. Never load a second model into ollama while photos are processing; benchmark with the queue empty. A wedged ollama (`ollama ps` shows "Stopping…" for minutes, vision calls hang) is cleared with `systemctl --user restart ollama`.
- **Stuck jobs** are re-queued automatically after `ENGINE_JOB_TIMEOUT + 120 s`. An engine crash by signal (exit −11, the libcuda stack over-read) gets one automatic retry; `ENGINE_ENV_PAD_KB` is the workaround — leave it on.
- **Don't slow the live site.** Keep blocking work, network calls and long write transactions off request handlers; do them in the worker and commit briefly.

## Backups and restore

`scripts/backup_dash.sh` (nightly) writes to `~/Backups/sneaker-dash/`: a gzipped online SQLite snapshot (newest 30 kept) plus rsync mirrors of `images/` and `label_data/`. It is the same physical disk, so it guards against corruption and bad deploys, not disk failure.

Manual one-off snapshots (e.g. before a destructive data script) go in `~/Backups/sneaker-dash/manual-snapshots/`, not in the repo directory. To restore the DB: stop both dash services, `gunzip -c ~/Backups/sneaker-dash/db/sneakers-<stamp>.db.gz > sneakers.db`, remove the stale `sneakers.db-wal`/`-shm`, start the services.

## IT gate

With `IT_PASSWORD` set in `.env`, the site is locked to Capture / Tableau / Config for everyone; IT staff unlock the rest at `/it` (signed cookie, `IT_SESSION_HOURS`). Changing the password logs everyone out. Unset it to open the whole site.

## Station unreachable

"Site down at the station" usually means the **server's Wi-Fi**, not the services — the dash can stay up on the second interface. Check `nmcli device status` first; `nmcli con up CoastalExport ifname wlp129s0f0` restores the station-link interface (the watchdog timer does this automatically). Bookmarking `http://aipc.local:8000` avoids dependence on the IP. The station camera is controlled over SSH to the host named in `CAMERA_HOST` (use an mDNS name rather than a DHCP IP, which goes stale).

## Feature switches worth knowing

Flags live in `.env` (see `backend/config.py` for the full list): `ENGINE_SEGMENT_BACKEND` / `ENGINE_SEGMENT_MODEL` (SAM3 vs YOLOE — two-line revert), `PREVIEW_SEGMENT_ENABLED` (capture guide), `SEEN_SHOE_ENABLED`, `SHOE_GATE_ENABLED`, `VISUAL_SEARCH_ENABLED`, `CLOUD_BACKEND` / `GEMINI_MODEL`, `AIRTABLE_SYNC_ENABLED`, `IT_PASSWORD`. Restart both dash services after changing any of them.
