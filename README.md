# home

Infraestructura de mi homelab casero (un Optiplex), gestionada como
`docker-compose.yaml` + configuración versionada. Incluye monitorización
(Prometheus/Grafana), DNS/adblock (Pi-hole), acceso remoto (Tailscale),
gestor de contraseñas (Vaultwarden), proxy inverso con TLS (Nginx Proxy
Manager), Home Assistant y backups automatizados con `restic`.

## Servicios (`docker-compose.yaml`)

| Servicio | Descripción |
|---|---|
| `homeassistant` | Domótica. `network_mode: host`, usa un dongle Zigbee USB. |
| `pihole` | DNS + adblock para toda la red. |
| `vaultwarden` | Servidor Bitwarden self-hosted. |
| `nginx-proxy-manager` | Proxy inverso y gestión de certificados Let's Encrypt. |
| `node-exporter` | Métricas del host para Prometheus. |
| `cadvisor` | Métricas de contenedores Docker. |
| `prometheus` | Almacena las métricas (retención 730 días). |
| `grafana` | Dashboards, en `https://grafana.kikeramirez.org`. |
| `blackbox-exporter` | Sondas de disponibilidad HTTP/ICMP. |
| `speedtest-exporter` | Métricas periódicas de velocidad de conexión. |
| `tailscale` | VPN mesh / acceso remoto, anuncia la ruta a la LAN. |

## Estructura del repositorio

```
docker-compose.yaml       # Definición de todos los servicios
.env                       # Secretos (NO versionado, ver .env.example)
backups/                   # Scripts de backup con restic + métricas custom
grafana/provisioning/      # Datasources, dashboards y alerting de Grafana
prometheus/prometheus.yml  # Configuración de scraping
blackbox/config.yml        # Módulos de sondeo de blackbox-exporter
homeassistant/config/      # Configuración de Home Assistant (parcial, ver abajo)
pihole/, vaultwarden/, nginx-proxy-manager/, tailscale/
                            # Bind mounts de datos runtime de cada servicio
                            # (bases de datos, certificados, estado -> ignorados)
```

**Importante:** los directorios de datos runtime de cada servicio (bases de
datos SQLite, certificados TLS, claves, tokens de sesión, el `.storage` y
`.cloud` de Home Assistant, el estado de Tailscale, etc.) están excluidos
vía `.gitignore` porque contienen secretos y datos personales. Solo se
versiona la configuración "de código" (compose, provisioning, scripts).

## Backups (`backups/`)

- `backup.sh`: backup nocturno (cron 03:30) de datos de las apps + config,
  con rotación 7 diaria / 4 semanal / 6 mensual, vía `restic`.
- `backup-monthly.sh`: backup mensual completo del sistema (cron día 1 a
  las 00:00), conservado para siempre con el tag `monthly-full`. Incluye
  además la receta de infraestructura (`docker-compose.yaml` + `.env`),
  configuración de Prometheus/Blackbox, estado de Tailscale y el volumen
  con datos de Grafana.
- `webhook-server.py`: servidor mínimo (stdlib only) que expone un trigger
  HTTP para lanzar `backup.sh` a demanda desde un enlace del dashboard
  "Backups" de Grafana. Protegido por un token compartido
  (`BACKUP_TRIGGER_TOKEN`) y solo accesible desde la LAN/Tailscale.
- `container-health-metrics.sh`: expone el estado de `HEALTHCHECK` de cada
  contenedor (cAdvisor no lo hace) al textfile collector de node-exporter.
  Se ejecuta cada minuto vía cron.
- `network-metrics.sh`: vuelca métricas de Pi-hole (resumen + dispositivos)
  al textfile collector.
- `backup-common.sh`: helpers compartidos (lock, logging, métricas) usados
  por los dos scripts de backup.

El repositorio de `restic` se guarda en un disco externo montado en
`/media/home/home-backups`, cifrado con la contraseña de
`backups/restic-password.txt` (no versionada).

## Puesta en marcha

1. Clona el repo en el host que va a correr los servicios.
2. Copia `.env.example` a `.env` y rellena los valores reales:
   ```bash
   cp .env.example .env
   ```
3. Levanta los servicios:
   ```bash
   docker compose up -d
   ```
4. Restaura desde backup lo que se excluyó del repo (bases de datos,
   certificados, estado de Tailscale, `.storage`/`.cloud` de Home
   Assistant) si vienes de una instalación existente, o vuelve a
   configurar cada servicio desde cero si es una instalación nueva.
5. Configura los crons de `backups/` (`backup.sh`, `backup-monthly.sh`,
   `container-health-metrics.sh`, `network-metrics.sh`) según los
   horarios indicados en la cabecera de cada script.

## Notas

- Dashboards y alertas de Grafana viven tanto en JSON provisionado
  (`grafana/provisioning/dashboards/`) como en el volumen `grafana_data`
  (cambios hechos desde la UI); el backup mensual cubre ambos.
- `home-launcher.json` es un dashboard tipo "portal" de acceso a los
  servicios del homelab.
