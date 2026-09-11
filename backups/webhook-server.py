#!/usr/bin/env python3
"""Minimal local trigger for backup.sh, called from a link in the Grafana
'Backups' dashboard. No external dependencies (stdlib only). Runs directly
on the host so backup.sh's own `docker run` calls work exactly as they do
under cron -- no Docker-in-Docker, no docker.sock mounted anywhere.

Protected by a shared-secret token (BACKUP_TRIGGER_TOKEN in .env), on top
of only being reachable from the home LAN/Tailscale (never published to
the internet). Overlapping runs are prevented by backup.sh's own flock.
"""
import http.server
import os
import subprocess
import time

TOKEN = os.environ["BACKUP_TRIGGER_TOKEN"]
BASE_DIR = "/home/home/home"
SCRIPT = f"{BASE_DIR}/backups/backup.sh"
LOG_FILE = f"{BASE_DIR}/backups/backup.log"
LOCK_FILE = f"{BASE_DIR}/backups/backup.lock"
PORT = 8088

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Backup</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#111217;color:#ccccdc;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;}}
.card{{text-align:center;padding:32px 40px;border-radius:10px;background:rgba(204,204,220,0.06);
border:1px solid rgba(204,204,220,0.12);}}
.icon{{font-size:40px;margin-bottom:12px;}}
h1{{font-size:18px;margin:0 0 6px;}}
p{{font-size:13px;color:#8e8e9c;margin:0;}}
</style></head>
<body><div class="card"><div class="icon">{icon}</div><h1>{title}</h1><p>{msg}</p></div></body></html>"""


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


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_html(self, code, icon, title, msg):
        body = PAGE.format(icon=icon, title=title, msg=msg).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path == "/healthz":
            self._send_html(200, "✅", "OK", "webhook-server activo")
            return

        if path != "/backup-now":
            self._send_html(404, "❓", "No encontrado", "")
            return

        if params.get("token") != TOKEN:
            self._send_html(401, "\U0001f512", "No autorizado", "Token invalido o ausente")
            return

        if backup_in_progress():
            self._send_html(200, "⏳", "Ya en curso", "Ya hay un backup ejecutandose, espera a que termine.")
            return

        subprocess.Popen(
            ["/bin/bash", SCRIPT],
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._send_html(
            202, "\U0001f680", "Backup iniciado",
            "Tardara uno o dos minutos. Puedes cerrar esta pestana y consultar el estado en el dashboard."
        )

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[{time.strftime('%F %T')}] backup webhook listening on :{PORT}")
    server.serve_forever()
