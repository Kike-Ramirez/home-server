# Restoring the homelab from backup (new machine or dead disk)

[🇪🇸 Español](RESTAURAR-DESDE-BACKUP.md) · 🇬🇧 English

Use this when: the machine that ran the stack has died (disk, motherboard,
whatever) and you want to bring it all back up on a different one, starting
from the last backup on the external USB drive. If instead the system is
still alive and you just want to recover a specific snapshot (e.g. undoing
an HA restore that went wrong), use `backups/restore.sh` directly — it's
built for that and already does everything (stops the stack, restores,
brings it back up), see `CLAUDE.md`.

## 0. What you absolutely need before starting

- **The physical backups USB drive**, with the restic repo on it. If the
  USB drive has also been lost/destroyed (theft, fire, whatever, along with
  the machine), there's an offsite copy on Google Drive with only the
  `monthly-full` snapshots — see the note at the end of this step.
- **`backups/restic-password.txt`** recovered from wherever you saved it
  outside this machine (password manager, paper...). It's in `.gitignore`
  on purpose — without it the restic repo is undecipherable and
  unrecoverable (this applies equally to the offsite repo: same password,
  same encryption). If you don't have it, no restore is possible.
- Docker Engine + `docker compose` and `git` installed on the new machine.

**If you're restoring from the offsite repo (no USB drive):** `restore.sh`
and the steps below assume the repo lives at
`/media/home/home-backups/home-backups` via Docker (the `restic/restic`
image doesn't ship the rclone backend), so instead of touching those
scripts, "pull" the offsite repo down into a new local repo first, then
follow the rest of the guide exactly as written:

```bash
# native restic (not the container) + rclone, with a "gdrive" remote that
# has access to the same Google Drive account (rclone config; it doesn't
# need to be the same Client ID/secret used before, it just needs to
# authorize the same account)
mkdir -p /media/home/home-backups/home-backups
RESTIC_REPOSITORY=/media/home/home-backups/home-backups \
RESTIC_PASSWORD_FILE=backups/restic-password.txt \
restic init

restic copy \
  --from-repo rclone:gdrive:home-backups-offsite \
  --from-password-file backups/restic-password.txt \
  --repo /media/home/home-backups/home-backups \
  --password-file backups/restic-password.txt
```

From here, follow step 3 onward exactly as written (you'll only have
`monthly-full` snapshots, which is exactly what you need).

## 1. Clone the repo (empty, no runtime data yet)

```bash
git clone git@github.com:Kike-Ramirez/home-server.git /home/home/home
cd /home/home/home
```

The `backups/` scripts assume this exact path (`BASE_DIR` in
`backup-common.sh`) — clone it here or edit that constant.

## 2. Mount the USB drive and place the restic password

```bash
# Mount the USB drive at /media/home/home-backups (the restic repo must end
# up at /media/home/home-backups/home-backups, which is what
# backup-common.sh expects)

cp /path/where/you/kept/restic-password.txt backups/restic-password.txt
chmod 600 backups/restic-password.txt
```

## 3. Find the snapshot to restore

You want the most recent `monthly-full` (it includes EVERYTHING:
docker-compose.yaml, .env, Prometheus/Blackbox config, Tailscale state,
and the Grafana DB, on top of the apps' data) — a `nightly` doesn't carry
those infrastructure files, and on a new machine you need them.

```bash
docker run --rm -v /media/home/home-backups/home-backups:/repo \
  -v "$(pwd)/backups/restic-password.txt:/repo-password:ro" \
  -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest snapshots --tag monthly-full
```

Note the (short) `id` of the most recent one.

## 4. Create the destinations `restore.sh` expects

On a new machine these paths don't exist yet; `restore.sh` mounts some of
them as a file (not a directory), so they need to be created empty first
or Docker will create them as a directory and the restore will fail:

```bash
mkdir -p pihole/etc-pihole vaultwarden/data \
  nginx-proxy-manager/data nginx-proxy-manager/letsencrypt \
  tailscale/state

touch docker-compose.yaml .env
```

(the `docker-compose.yaml`/`.env` you just `touch`ed get fully overwritten
in the next step — this is only so Docker's bind mount treats them as a
file.)

## 5. Restore

```bash
backups/restore.sh <snapshot_id>
```

This does (see the header of `backups/restore.sh` for details): first
takes a `pre-restore` snapshot of the current state (nearly empty, that's
normal, it's your safety net), stops the stack (a no-op, nothing's running
yet), restores every path from the chosen `monthly-full`, and brings it up
with `docker compose up -d` using the `docker-compose.yaml`/`.env` that
just came back.

## 6. What the backup does NOT bring — needs redoing by hand

- **Crontab**: not backed up. Install it exactly as it is in
  [`SETUP-DESDE-CERO.en.md`, step 13](SETUP-DESDE-CERO.en.md#13-crontab).
- **`telegram-agent/.venv`**: not backed up (it's reproducible). Recreate it:
  ```bash
  cd telegram-agent
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt && deactivate
  ```
- **Sebastián's systemd service**: the `sebastian-bot.service` file does
  come back with `git clone` (it's version-controlled), but it isn't
  installed under `/etc/systemd/system/` on the new machine:
  ```bash
  sudo cp telegram-agent/sebastian-bot.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now sebastian-bot
  ```
- **Authenticated `claude` CLI**: Sebastián reuses the system user's CLI
  session — it has to be installed and logged in (`claude login`) on the
  new machine, or Sebastián won't start.
- **`telegram-agent/session_id.txt`**: not backed up on purpose (low
  value) — Sebastián simply starts a new conversation, with no short-term
  memory of the previous thread. His `memory.md` notebook does come back
  (it's been included in the nightly backup since 2026-09-13), so he
  doesn't lose what was noted down long-term.
- **Zigbee dongle**: if you're changing machines, the path
  `/dev/serial/by-id/usb-ITead_Sonoff_...` in `docker-compose.yaml` might
  not match exactly — check it with `ls /dev/serial/by-id/` on the new
  machine and adjust `docker-compose.yaml` if it differs.

## 7. Verification

Same as `SETUP-DESDE-CERO.en.md`, step 14:
`docker compose ps`, Grafana reachable, `systemctl status sebastian-bot`,
Sebastián responds in the Telegram group, a test backup by hand.
