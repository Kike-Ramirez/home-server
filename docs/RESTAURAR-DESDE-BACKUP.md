# Restaurar el homelab desde backup (máquina nueva o disco roto)

Para esto: la máquina que corría el stack ha muerto (disco, placa, lo que
sea) y quieres levantarlo todo de nuevo en otra, partiendo del último backup
en el USB externo. Si en cambio el sistema sigue vivo y solo quieres
recuperar un snapshot puntual (p.ej. deshacer una restauración de HA que
salió mal), usa `backups/restore.sh` directamente — está pensado para eso y
ya lo hace todo (para el stack, restaura, lo levanta), ver `CLAUDE.md`.

## 0. Qué necesitas SÍ o SÍ antes de empezar

- **El disco USB de backups** físico, con el repo restic dentro.
- **`backups/restic-password.txt`** recuperado de donde lo guardaste fuera
  de esta máquina (gestor de contraseñas, papel...). Está en `.gitignore` a
  propósito — sin él, el repo restic es indescifrable e irrecuperable. Si no
  lo tienes, no hay restore posible.
- Docker Engine + `docker compose` y `git` instalados en la máquina nueva.

## 1. Clonar el repo (vacío, sin datos runtime todavía)

```bash
git clone git@github.com:Kike-Ramirez/home-server.git /home/home/home
cd /home/home/home
```

Los scripts de `backups/` asumen esta ruta exacta (`BASE_DIR` en
`backup-common.sh`) — clónalo aquí o edita esa constante.

## 2. Montar el USB y colocar la contraseña de restic

```bash
# Monta el USB en /media/home/home-backups (debe quedar el repo restic en
# /media/home/home-backups/home-backups, que es lo que espera backup-common.sh)

cp /ruta/donde/tenías/restic-password.txt backups/restic-password.txt
chmod 600 backups/restic-password.txt
```

## 3. Localizar el snapshot a restaurar

Quieres el último `monthly-full` (incluye TODO: docker-compose.yaml, .env,
config de Prometheus/Blackbox, estado de Tailscale y la BD de Grafana,
además de los datos de las apps) — un `nightly` no trae esos ficheros de
infraestructura y en una máquina nueva los necesitas.

```bash
docker run --rm -v /media/home/home-backups/home-backups:/repo \
  -v "$(pwd)/backups/restic-password.txt:/repo-password:ro" \
  -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest snapshots --tag monthly-full
```

Apunta el `id` (corto) del más reciente.

## 4. Crear los destinos que `restore.sh` espera

En una máquina nueva estos paths todavía no existen; `restore.sh` monta
algunos como fichero (no directorio), así que hay que crearlos vacíos antes
o Docker los crea como directorio y el restore falla:

```bash
mkdir -p pihole/etc-pihole vaultwarden/data \
  nginx-proxy-manager/data nginx-proxy-manager/letsencrypt \
  tailscale/state

touch docker-compose.yaml .env
```

(`docker-compose.yaml`/`.env` que acabas de tocar con `touch` se sobrescriben
del todo en el paso siguiente — es solo para que el bind mount de Docker los
trate como fichero.)

## 5. Restaurar

```bash
backups/restore.sh <snapshot_id>
```

Esto (ver cabecera de `backups/restore.sh` para el detalle): hace primero un
snapshot `pre-restore` del estado actual (casi vacío, es normal, es tu red
de seguridad), para el stack (no-op, aún no hay nada corriendo), restaura
todas las rutas del `monthly-full` elegido, y levanta `docker compose up -d`
con el `docker-compose.yaml`/`.env` que acaban de volver.

## 6. Lo que el backup NO trae — hay que rehacerlo a mano

- **Crontab**: no se respalda. Instálalo tal cual está en
  [`SETUP-DESDE-CERO.md`, paso 10](SETUP-DESDE-CERO.md#10-crontab).
- **`telegram-agent/.venv`**: no se respalda (es reproducible). Recréalo:
  ```bash
  cd telegram-agent
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt && deactivate
  ```
- **Servicio systemd de Sebastián**: el fichero `sebastian-bot.service` sí
  vuelve con el `git clone` (está versionado), pero no está instalado en
  `/etc/systemd/system/` de la máquina nueva:
  ```bash
  sudo cp telegram-agent/sebastian-bot.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now sebastian-bot
  ```
- **CLI `claude` autenticado**: Sebastián reutiliza la sesión del CLI del
  usuario del sistema — tiene que estar instalado y con `claude login` hecho
  en la máquina nueva, o Sebastián no arrancará.
- **`telegram-agent/session_id.txt`**: no se respalda a propósito (bajo
  valor) — Sebastián simplemente empieza una conversación nueva, sin
  memoria de corto plazo del hilo anterior. Su libreta `memory.md` sí vuelve
  (va dentro del backup nightly desde 2026-09-13), así que no pierde lo que
  tenía anotado a largo plazo.
- **Dongle Zigbee**: si cambias de máquina, la ruta
  `/dev/serial/by-id/usb-ITead_Sonoff_...` en `docker-compose.yaml` puede no
  coincidir exactamente — verifícala con `ls /dev/serial/by-id/` en la
  máquina nueva y ajusta el `docker-compose.yaml` si difiere.

## 7. Verificación

Igual que en `SETUP-DESDE-CERO.md`, paso 11:
`docker compose ps`, Grafana accesible, `systemctl status sebastian-bot`,
Sebastián responde en el grupo de Telegram, backup de prueba a mano.
