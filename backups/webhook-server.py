#!/usr/bin/env python3
"""Minimal local trigger for backup.sh y restore.sh, llamado desde enlaces
en el dashboard Grafana 'Backups'. Sin dependencias externas (stdlib only).
Corre directamente en el host para que los `docker run`/`docker compose` de
backup.sh y restore.sh funcionen igual que bajo cron -- sin Docker-in-Docker
ni montar el socket de Docker en ningun sitio.

Protegido por un token compartido (BACKUP_TRIGGER_TOKEN en .env), ademas de
solo ser alcanzable desde la LAN/Tailscale (nunca publicado a Internet).
Las ejecuciones solapadas las evita el flock propio de backup.sh/restore.sh.

Endpoints:
  GET  /backup-now?token=...                  lanza un backup manual
  GET  /restore?token=...&snapshot=<id>        pantalla de confirmacion
  POST /restore-confirm  (token, snapshot)     ejecuta el restore
"""
import http.server
import json
import os
import re
import subprocess
import time
import urllib.parse

TOKEN = os.environ["BACKUP_TRIGGER_TOKEN"]
BASE_DIR = "/home/home/home"
BACKUP_SCRIPT = f"{BASE_DIR}/backups/backup.sh"
RESTORE_SCRIPT = f"{BASE_DIR}/backups/restore.sh"
LOG_FILE = f"{BASE_DIR}/backups/backup.log"
LOCK_FILE = f"{BASE_DIR}/backups/backup.lock"
REPO_DIR = "/media/home/home-backups/home-backups"
PASSWORD_FILE = f"{BASE_DIR}/backups/restic-password.txt"
PORT = 8088

SNAPSHOT_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")

CONTENT_BY_TAG = {
    "nightly": "Vaultwarden, Pi-hole, Nginx Proxy Manager, Home Assistant, Grafana (provisioning)",
    "monthly-full": "Todo lo anterior + docker-compose.yaml, .env, config. Prometheus/Blackbox, Tailscale y la base de datos de Grafana",
    "pre-restore": "Snapshot de seguridad automatico tomado justo antes de un restore anterior (misma cobertura que el mensual)",
}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Backup</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#111217;color:#ccccdc;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;padding:24px;box-sizing:border-box;}}
.card{{text-align:center;padding:32px 40px;border-radius:10px;background:rgba(204,204,220,0.06);
border:1px solid rgba(204,204,220,0.12);max-width:480px;}}
.icon{{font-size:40px;margin-bottom:12px;}}
h1{{font-size:18px;margin:0 0 6px;}}
p{{font-size:13px;color:#8e8e9c;margin:0;line-height:1.5;}}
</style></head>
<body><div class="card"><div class="icon">{icon}</div><h1>{title}</h1><p>{msg}</p></div></body></html>"""

CONFIRM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Confirmar restore</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#111217;color:#ccccdc;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;padding:24px;box-sizing:border-box;}}
.card{{padding:28px 32px;border-radius:10px;background:rgba(204,204,220,0.06);
border:1px solid rgba(255,159,10,0.35);max-width:520px;}}
h1{{font-size:18px;margin:0 0 14px;color:#FF9F0A;}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:16px;}}
td{{padding:6px 0;vertical-align:top;}}
td.label{{color:#8e8e9c;width:110px;}}
td.value{{color:#ccccdc;}}
.warn{{background:rgba(255,69,58,0.1);border:1px solid rgba(255,69,58,0.35);border-radius:8px;
padding:12px 14px;font-size:12px;color:#ccccdc;line-height:1.5;margin-bottom:20px;}}
button{{width:100%;padding:14px;border:none;border-radius:8px;background:#FF453A;color:#fff;
font-size:14px;font-weight:600;cursor:pointer;}}
button:hover{{background:#e0392f;}}
</style></head>
<body><div class="card">
<h1>&#9888;&#65039; Confirmar restore</h1>
<table>
  <tr><td class="label">Snapshot</td><td class="value">{short_id}</td></tr>
  <tr><td class="label">Fecha</td><td class="value">{date}</td></tr>
  <tr><td class="label">Tipo</td><td class="value">{tag}</td></tr>
  <tr><td class="label">Restaura</td><td class="value">{content}</td></tr>
</table>
<div class="warn">
  Esto <b>sobrescribira datos en vivo</b> con los de este snapshot y parara/
  levantara todo el stack durante la operacion.{extra_warning}
  <br><br>Se creara automaticamente un snapshot de seguridad "pre-restore"
  del estado actual antes de tocar nada, por si hay que deshacerlo.
</div>
<form method="POST" action="/restore-confirm">
  <input type="hidden" name="token" value="{token}">
  <input type="hidden" name="snapshot" value="{full_id}">
  <button type="submit">Si, restaurar y sobrescribir</button>
</form>
</div></body></html>"""

MONTHLY_EXTRA_WARNING = (
    " Este snapshot en concreto incluye <b>docker-compose.yaml y .env</b>: "
    "puede revertir credenciales/tokens (por ejemplo la contrasena SMTP) a "
    "los que hubiera en ese momento."
)


def backup_in_progress():
    return os.path.exists(LOCK_FILE) and _flock_held(LOCK_FILE)


def _flock_held(path):
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def get_snapshot(snapshot_id):
    """Devuelve el dict del snapshot (restic --json) o None si no existe."""
    try:
        out = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{REPO_DIR}:/repo",
             "-v", f"{PASSWORD_FILE}:/repo-password:ro",
             "-e", "RESTIC_REPOSITORY=/repo",
             "-e", "RESTIC_PASSWORD_FILE=/repo-password",
             "restic/restic:latest",
             "snapshots", snapshot_id, "--json"],
            capture_output=True, text=True, timeout=30,
        )
        snaps = json.loads(out.stdout)
        return snaps[0] if snaps else None
    except Exception:
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_html(self, code, body_html):
        body = body_html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_card(self, code, icon, title, msg):
        self._send_html(code, PAGE.format(icon=icon, title=title, msg=msg))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(
            (k, urllib.parse.unquote_plus(v))
            for k, v in (p.split("=", 1) for p in query.split("&") if "=" in p)
        )

        if path == "/healthz":
            self._send_card(200, "✅", "OK", "webhook-server activo")
            return

        if path == "/backup-now":
            self._handle_backup_now(params)
            return

        if path == "/restore":
            self._handle_restore_confirm_page(params)
            return

        self._send_card(404, "❓", "No encontrado", "")

    def do_POST(self):
        if self.path != "/restore-confirm":
            self._send_card(404, "❓", "No encontrado", "")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        params = {
            k: v[0] for k, v in urllib.parse.parse_qs(body).items()
        }
        self._handle_restore_execute(params)

    def _handle_backup_now(self, params):
        if params.get("token") != TOKEN:
            self._send_card(401, "\U0001f512", "No autorizado", "Token invalido o ausente")
            return

        if backup_in_progress():
            self._send_card(200, "⏳", "Ya en curso", "Ya hay una operacion de backup/restore ejecutandose, espera a que termine.")
            return

        subprocess.Popen(
            ["/bin/bash", BACKUP_SCRIPT],
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._send_card(
            202, "\U0001f680", "Backup iniciado",
            "Tardara uno o dos minutos. Puedes cerrar esta pestana y consultar el estado en el dashboard."
        )

    def _handle_restore_confirm_page(self, params):
        if params.get("token") != TOKEN:
            self._send_card(401, "\U0001f512", "No autorizado", "Token invalido o ausente")
            return

        snapshot_id = params.get("snapshot", "")
        if not SNAPSHOT_ID_RE.match(snapshot_id):
            self._send_card(400, "⚠️", "ID de snapshot invalido", "Copia el ID tal cual aparece en la tabla del dashboard.")
            return

        snap = get_snapshot(snapshot_id)
        if snap is None:
            self._send_card(404, "❓", "Snapshot no encontrado", f"No existe ningun snapshot con id {snapshot_id}.")
            return

        tag = (snap.get("tags") or ["sin-tag"])[0]
        html = CONFIRM_PAGE.format(
            short_id=snap.get("short_id", snapshot_id[:8]),
            date=snap.get("time", "")[:19],
            tag=tag,
            content=CONTENT_BY_TAG.get(tag, tag),
            extra_warning=MONTHLY_EXTRA_WARNING if tag == "monthly-full" else "",
            token=params["token"],
            full_id=snap.get("id", snapshot_id),
        )
        self._send_html(200, html)

    def _handle_restore_execute(self, params):
        if params.get("token") != TOKEN:
            self._send_card(401, "\U0001f512", "No autorizado", "Token invalido o ausente")
            return

        snapshot_id = params.get("snapshot", "")
        if not SNAPSHOT_ID_RE.match(snapshot_id):
            self._send_card(400, "⚠️", "ID de snapshot invalido", "")
            return

        if backup_in_progress():
            self._send_card(200, "⏳", "Ya en curso", "Ya hay una operacion de backup/restore ejecutandose, espera a que termine.")
            return

        subprocess.Popen(
            ["/bin/bash", RESTORE_SCRIPT, snapshot_id],
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._send_card(
            202, "♻️", "Restore iniciado",
            "El stack se parara y volvera a levantarse solo. Sigue el progreso en backups/backup.log o en el dashboard."
        )

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[{time.strftime('%F %T')}] backup/restore webhook listening on :{PORT}")
    server.serve_forever()
