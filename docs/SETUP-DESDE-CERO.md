# Levantar el homelab desde cero

🇪🇸 Español · [🇬🇧 English](SETUP-DESDE-CERO.en.md)

Guía paso a paso para desplegar todo el stack en una máquina nueva, sin
backup previo. Si ya existía un backup de un sistema anterior (disco roto,
migración de hardware), usa en su lugar
[`RESTAURAR-DESDE-BACKUP.md`](RESTAURAR-DESDE-BACKUP.md).

Los scripts de `backups/` asumen que el repo vive en `/home/home/home`
exactamente (está hardcodeado en `backups/backup-common.sh`) — clónalo ahí o
ajusta esa ruta antes de continuar.

## 1. Prerrequisitos

- Máquina Linux con Docker Engine + plugin `docker compose` instalados.
- `python3-yaml` (`sudo apt install python3-yaml`) — lo usa `daily-report.py`
  para leer `grafana/provisioning/alerting/rules.yaml` directamente.
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
| `GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` | `admin` + `openssl rand -base64 24` — evita que Grafana quede en `admin/admin` si se recrea el volumen `grafana_data`. |
| `TS_AUTHKEY` | Tailscale admin console → Settings → Keys → generar una auth key reusable. **Una vez el nodo esté autenticado y `docker compose ps tailscale` lo muestre `healthy`** (el estado persiste en `tailscale/state/`, no vuelve a necesitar la key salvo que se borre ese estado), revócala desde la consola y vacía `TS_AUTHKEY=` en `.env` — dejarla viva indefinidamente en texto plano es un secreto de "añadir dispositivo a mi tailnet" sin necesidad real. |
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

## 10. Webhook de backups como servicio systemd

`webhook-server.py` corre en el **host** (no contenedorizado, a propósito,
para evitar Docker-in-Docker al lanzar `docker run` de restic) y expone
`backup-now`/`restore`/`restore-confirm` para el dashboard "Backups". Se
gestiona con systemd en vez de `@reboot`/`nohup` para que se reinicie solo
si muere:

```bash
sudo cp backups/webhook-server.service /etc/systemd/system/webhook-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now webhook-server
sudo systemctl status webhook-server --no-pager
curl -s http://localhost:8088/healthz
```

`EnvironmentFile=/home/home/home/.env` en la unit le da acceso a
`BACKUP_TRIGGER_TOKEN` (y al resto de `.env`, igual que hace `docker
compose`) — ajusta esa ruta si no clonaste el repo en `/home/home/home`.

## 11. Mantenimiento automático del sistema (Ubuntu)

Esto es config del **host**, no del repo (no hay nada que versionar salvo lo
que ya está en `backups/docker-prune.sh` y el crontab del siguiente paso).
Se necesita para dos cosas: que el sistema operativo se actualice solo (no
solo los contenedores) y que se avise por correo si algo falla.

### 11.1 Actualizaciones de seguridad automáticas (`unattended-upgrades`)

Por qué: sin esto, los parches de seguridad del SO (kernel, OpenSSL, etc.)
solo se aplican si haces `apt upgrade` a mano. En una máquina que corre
servicios expuestos a internet (Nginx Proxy Manager, Vaultwarden) conviene
que al menos los parches de seguridad se apliquen solos.

```bash
sudo apt install unattended-upgrades apt-listchanges
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

Esto escribe `APT::Periodic::Unattended-Upgrade "1";` en
`/etc/apt/apt.conf.d/20auto-upgrades`. No hace falta timer propio: lo
disparan los timers de systemd de APT (`apt-daily.timer` para refrescar el
índice, `apt-daily-upgrade.timer` para aplicar las actualizaciones) — ya
vienen con el paquete `apt`, comprueba que estén activos:

```bash
systemctl list-timers | grep apt
```

Config fina en `/etc/apt/apt.conf.d/50unattended-upgrades` (por defecto solo
actualiza el repo `-security`; ajusta `Allowed-Origins` y
`Automatic-Reboot` si quieres más o reinicios automáticos tras kernel).

### 11.2 Correo de aviso si `unattended-upgrades` falla

Por qué: sin un MTA local, `unattended-upgrades` no tiene forma de mandar
correo aunque se lo pidas en su config — necesita algo detrás del binario
`/usr/sbin/sendmail`. Reutilizamos el SMTP de Gmail que ya está en `.env`
(el mismo que usan Grafana/Vaultwarden/`daily-report.py`), en vez de montar
un Postfix completo.

```bash
sudo apt install msmtp msmtp-mta
```

Crea `/etc/msmtprc` (usa las credenciales `SMTP_USERNAME`/`SMTP_PASSWORD` de
tu `.env`):

```
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/msmtp.log

account        gmail
host           smtp.gmail.com
port           587
from           <SMTP_USERNAME>
user           <SMTP_USERNAME>
password       <SMTP_PASSWORD>

account default : gmail
```

```bash
sudo chown root:root /etc/msmtprc
sudo chmod 600 /etc/msmtprc
```

`msmtp-mta` ya deja `/usr/sbin/sendmail` apuntando a `msmtp` — no hay que
tocar `update-alternatives`.

Luego, en `/etc/apt/apt.conf.d/50unattended-upgrades`:

```
Unattended-Upgrade::Mail "<tu-email>";
Unattended-Upgrade::MailReport "only-on-error";
```

**AppArmor confina el binario `msmtp`** (perfil propio del paquete) y por
defecto no le deja ni crear ni bloquear su propio logfile fuera de las rutas
que trae el perfil — verás en `dmesg` algo como
`apparmor="DENIED" operation="mknod"` o `operation="file_lock"` sobre
`/var/log/msmtp.log`. Dale permiso explícito con un override local (no
edites el perfil del paquete directamente, se sobrescribiría en updates):

```bash
echo '/var/log/msmtp.log rwk,' | sudo tee -a /etc/apparmor.d/local/usr.bin.msmtp
sudo apparmor_parser -r /etc/apparmor.d/usr.bin.msmtp
```

Nota: `rw` sin la `k` no basta — `msmtp` hace `flock()` sobre el logfile
antes de escribir, y AppArmor trata el permiso de lock (`k`) por separado
del de lectura/escritura.

Verifica el envío real (como root, que es quien ejecuta `unattended-upgrades`):

```bash
sudo msmtp -a gmail <tu-email> <<< "Test unattended-upgrades"
echo "exit=$?"   # debe dar 0, sin ningún mensaje de msmtp
```

### 11.3 Rotación de los logs de `backups/` y de Sebastián

Por qué: `backup.log`, `webhook-server.log`, `daily-report.log`, etc. crecen
sin límite si nada los rota — `backups/logrotate.conf` (versionado) cubre
todos los `backups/*.log` más `telegram-agent/agent.log`.

```bash
sudo cp backups/logrotate.conf /etc/logrotate.d/homelab
sudo logrotate -d /etc/logrotate.d/homelab   # dry-run, valida la config
```

Semanal, 8 semanas de histórico, comprimido. `su home home` en la config es
necesario porque `backups/` es `775` (no solo escribible por root) —
logrotate se niega a rotar ahí sin decirle explícitamente qué usuario/grupo
usar. Lo dispara el cron diario que ya trae Ubuntu (`/etc/cron.daily/logrotate`),
no hace falta un cron propio.

## 12. Backup offsite en Google Drive (3-2-1)

Por qué: el USB de backups local es un único punto de fallo físico (robo,
incendio, fallo del disco a la vez que el servidor). Se manda una copia de
los snapshots `monthly-full` (los que ya conservas para siempre) a un
segundo repo restic independiente en Google Drive — solo esos, no los
`nightly`, para no gastar ancho de banda/almacenamiento de más.

Necesitas `restic` nativo en el host (no el contenedor `restic/restic`, esa
imagen no trae el backend rclone) y `rclone`:

```bash
sudo apt install rclone
# restic nativo: https://github.com/restic/restic/releases, o
# curl -fsSL https://raw.githubusercontent.com/restic/restic/master/install.sh | sh
```

Configura el remote (requiere navegador para el OAuth):

```bash
rclone config   # nueva remote "gdrive", tipo "drive"
```

**Usa tus propias credenciales OAuth**, no las compartidas por defecto de
rclone — la cuota compartida de la API de Google Drive se agota enseguida
con muchos usuarios de rclone en el mundo detrás (`rateLimitExceeded`
constante, confirmado en producción). En [Google Cloud
Console](https://console.cloud.google.com/): crea un proyecto → habilita la
**Google Drive API** → "Credentials" → "Create Credentials" → "OAuth client
ID" → tipo **Desktop app** → copia `Client ID`/`Client secret` y pégalos en
`rclone config` (edita el remote `gdrive`, campos `client_id`/`client_secret`,
luego `rclone config reconnect gdrive:` para re-autorizar con las nuevas
credenciales).

Inicializa el repo offsite (misma contraseña que el repo local):

```bash
RESTIC_REPOSITORY=rclone:gdrive:home-backups-offsite \
RESTIC_PASSWORD_FILE=backups/restic-password.txt \
restic init
```

`backups/backup-offsite.sh` hace el `restic copy --tag monthly-full` desde
el repo local a este. La primera vez sube todo el contenido desde cero
(puede tardar, y verás reintentos puntuales por `rateLimitExceeded` — es
normal, el endpoint de creación de ficheros de Drive tiene su propio límite
aparte de la cuota general del proyecto); ejecútala a mano una vez antes de
confiar en el cron:

```bash
backups/backup-offsite.sh
tail -f backups/backup-offsite.log
```

## 13. Crontab

```bash
crontab -e
```

Pega exactamente esto (ajusta rutas solo si no clonaste en `/home/home/home`):

```cron
30 3 * * * /home/home/home/backups/backup.sh
0 0 1 * * /home/home/home/backups/backup-monthly.sh
30 1 1 * * /home/home/home/backups/backup-offsite.sh
0 2 15 * * /home/home/home/backups/restic-check.sh
*/5 * * * * /home/home/home/backups/network-metrics.sh
* * * * * /home/home/home/backups/container-health-metrics.sh
0 8 * * * /usr/bin/python3 /home/home/home/backups/daily-report.py >> /home/home/home/backups/daily-report.log 2>&1
0 4 * * 0 /home/home/home/backups/docker-prune.sh
```

(el webhook de backups ya no va por cron — ver paso 10, corre como servicio systemd.)

## 14. Verificación final

- `docker compose ps` — todo arriba y "healthy".
- Grafana accesible en tu dominio, dashboards cargados (`grafana/provisioning/dashboards/`).
- `systemctl status sebastian-bot` — `active (running)`.
- Escribe al bot en el grupo de Telegram y confirma que responde.
- `systemctl status webhook-server` — `active (running)`; `curl http://localhost:8088/healthz` responde 200.
- Lanza un backup de prueba: `backups/backup.sh` a mano y revisa `backups/backup.log`.
- Confirma que llega el informe diario o espera al cron de las 08:00.
- `sudo msmtp -a gmail <tu-email> <<< "test"` sin errores — confirma que
  `unattended-upgrades` podrá avisar por correo si algo falla.
- `RESTIC_REPOSITORY=rclone:gdrive:home-backups-offsite RESTIC_PASSWORD_FILE=backups/restic-password.txt restic snapshots` muestra al menos un `monthly-full`.
- `sudo logrotate -d /etc/logrotate.d/homelab` sin errores (`insecure permissions`, etc.).
