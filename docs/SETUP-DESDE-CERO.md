# Levantar el homelab desde cero

Guía paso a paso para desplegar todo el stack en una máquina nueva, sin
backup previo. Si ya existía un backup de un sistema anterior (disco roto,
migración de hardware), usa en su lugar
[`RESTAURAR-DESDE-BACKUP.md`](RESTAURAR-DESDE-BACKUP.md).

Los scripts de `backups/` asumen que el repo vive en `/home/home/home`
exactamente (está hardcodeado en `backups/backup-common.sh`) — clónalo ahí o
ajusta esa ruta antes de continuar.

## 1. Prerrequisitos

- Máquina Linux con Docker Engine + plugin `docker compose` instalados.
- Disco USB externo dedicado a backups.
- Dominio propio (para Nginx Proxy Manager + Let's Encrypt + Grafana).
- Dongle Zigbee USB (para Home Assistant), conectado antes de arrancar.
- Cuenta de Tailscale.
- Cuenta Gmail (o SMTP que admita STARTTLS) para el correo de Vaultwarden/Grafana/informe diario.
- CLI `claude` (Claude Code) instalado y **autenticado** en la máquina con el
  usuario que va a correr el stack — Sebastián (`telegram-agent/`) reutiliza
  esa sesión, no usa una API key aparte.
- Un bot de Telegram creado con `@BotFather` (`/newbot`) — apunta el token.

## 2. Clonar el repo

```bash
git clone git@github.com:Kike-Ramirez/home-server.git /home/home/home
cd /home/home/home
```

## 3. Configurar `.env`

```bash
cp .env.example .env
```

Rellena cada variable:

| Variable | De dónde sale |
|---|---|
| `TZ` | Normalmente ya viene bien con `Europe/Madrid`. |
| `PIHOLE_PASSWORD` | La que quieras para el admin de Pi-hole. |
| `VAULTWARDEN_DOMAIN` | El subdominio donde publicarás Vaultwarden. |
| `SIGNUPS_ALLOWED` | `false` salvo que quieras registro abierto. |
| `VAULTWARDEN_ADMIN_TOKEN` | `openssl rand -base64 48` |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Cuenta Gmail + [contraseña de aplicación](https://myaccount.google.com/apppasswords). |
| `TS_AUTHKEY` | Tailscale admin console → Settings → Keys → generar una auth key reusable. |
| `BACKUP_TRIGGER_TOKEN` | `openssl rand -hex 32` — lo usará el dashboard "Backups" para llamar al webhook. |
| `TELEGRAM_API_TOKEN` | El que te dio `@BotFather` al crear el bot. |
| `TELEGRAM_CLIENT_ID` | Chat_id del grupo/chat de Telegram donde quieres alertas + Sebastián — ver paso 9, se rellena al final. |
| `ANTHROPIC_API_KEY` | No la necesitas para Sebastián (usa el CLI `claude` ya autenticado); solo hace falta si además configuras la integración "Anthropic Conversation" nativa de HA desde su UI. |

## 4. Preparar ficheros/directorios que no vienen en git

Todo esto está en `.gitignore` a propósito (son datos runtime o secretos):

```bash
mkdir -p pihole/etc-pihole vaultwarden/data \
  nginx-proxy-manager/data nginx-proxy-manager/letsencrypt \
  tailscale/state

# Contraseña de cifrado de los backups — GUÁRDALA TAMBIÉN FUERA DE ESTA
# MÁQUINA (gestor de contraseñas, papel, etc.). Sin ella los backups
# cifrados son irrecuperables.
openssl rand -base64 32 > backups/restic-password.txt
chmod 600 backups/restic-password.txt
```

`homeassistant/config/secrets.yaml` lo generará HA solo al primer arranque
si no existe; si lo necesitas antes (p.ej. para Telegram, ver paso 9), créalo
tú con las claves que falten.

## 5. Montar el disco de backups e inicializar restic

```bash
# Monta tu USB en /media/home/home-backups (fstab o a mano) y luego:
mkdir -p /media/home/home-backups/home-backups

docker run --rm \
  -v /media/home/home-backups/home-backups:/repo \
  -v "$(pwd)/backups/restic-password.txt:/repo-password:ro" \
  -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest init
```

## 6. Levantar el stack

```bash
docker compose config -q   # valida sintaxis
docker compose up -d
docker compose ps          # todo "healthy" o "running" en unos minutos
```

## 7. Home Assistant: token de larga duración

Una vez HA arrancado (`http://<ip-host>:8123`), completa el asistente de
configuración inicial y crea tu usuario. Luego:

1. Perfil (icono de usuario) → pestaña "Seguridad" → "Tokens de acceso de larga duración" → crear uno.
2. Guarda el valor en `prometheus/ha-token.txt` (sin saltos de línea extra):
   ```bash
   echo -n "<token>" > prometheus/ha-token.txt
   ```
   Lo usan tanto Prometheus (`/api/prometheus`) como las tools de Sebastián.
3. `docker compose restart prometheus` para que lo recoja.

## 8. Nginx Proxy Manager + dominio

Entra a `http://<ip-host>:81` (credenciales por defecto la primera vez, te
pedirá cambiarlas), da de alta tus proxy hosts (Grafana, Vaultwarden, etc.)
apuntando a los contenedores por su nombre (`grafana:3000`, ...) y pide los
certificados Let's Encrypt desde ahí.

## 9. Telegram: bot, grupo y Sebastián

1. Crea un grupo de Telegram con quien vaya a hablar con Sebastián, añade el
   bot como miembro.
2. Desactiva su "privacy mode" para que vea todos los mensajes del grupo (no
   solo comandos/menciones): `@BotFather` → `/mybots` → tu bot → `Bot
   Settings` → `Group Privacy` → `Turn off`.
3. Manda cualquier mensaje en el grupo y consulta el chat_id:
   ```bash
   curl -s "https://api.telegram.org/bot<TELEGRAM_API_TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Busca `"chat":{"id": ...}` (negativo, es un grupo) y ponlo en
   `TELEGRAM_CLIENT_ID` en `.env`.
4. Instala el entorno de Sebastián:
   ```bash
   cd telegram-agent
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   deactivate
   ```
5. Instala y arranca el servicio de systemd:
   ```bash
   sudo cp sebastian-bot.service /etc/systemd/system/sebastian-bot.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now sebastian-bot
   sudo systemctl status sebastian-bot --no-pager
   ```
6. `docker compose restart grafana` (para que recoja `TELEGRAM_API_TOKEN`/`TELEGRAM_CLIENT_ID` y pueda mandar alertas por Telegram).

## 10. Crontab

```bash
crontab -e
```

Pega exactamente esto (ajusta rutas solo si no clonaste en `/home/home/home`):

```cron
30 3 * * * /home/home/home/backups/backup.sh
0 0 1 * * /home/home/home/backups/backup-monthly.sh
*/5 * * * * /home/home/home/backups/network-metrics.sh
* * * * * /home/home/home/backups/container-health-metrics.sh
0 8 * * * /usr/bin/python3 /home/home/home/backups/daily-report.py >> /home/home/home/backups/daily-report.log 2>&1
@reboot sleep 20 && BACKUP_TRIGGER_TOKEN=$(grep '^BACKUP_TRIGGER_TOKEN=' /home/home/home/.env | cut -d= -f2-) nohup python3 /home/home/home/backups/webhook-server.py >> /home/home/home/backups/webhook-server.log 2>&1 &
```

## 11. Verificación final

- `docker compose ps` — todo arriba y "healthy".
- Grafana accesible en tu dominio, dashboards cargados (`grafana/provisioning/dashboards/`).
- `systemctl status sebastian-bot` — `active (running)`.
- Escribe al bot en el grupo de Telegram y confirma que responde.
- Lanza un backup de prueba: `backups/backup.sh` a mano y revisa `backups/backup.log`.
- Confirma que llega el informe diario o espera al cron de las 08:00.
