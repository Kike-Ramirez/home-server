# home — homelab en `/home/home/home`

Infraestructura de un homelab casero (Dell Optiplex, dominio `kikeramirez.org`) gestionada como código: `docker-compose.yaml` + configuración versionada, con monitorización, alertado y backups automatizados. Detalles completos en `README.md`; este fichero recoge lo que Claude necesita para trabajar bien aquí sin repetir contexto en cada sesión.

## Servicios (`docker-compose.yaml`)

| Servicio | Rol | Notas |
|---|---|---|
| `homeassistant` | Domótica | `network_mode: host`, dongle Zigbee USB. Speedtest y Nmap Tracker viven aquí (no como exporters aparte). |
| `pihole` | DNS + adblock | |
| `vaultwarden` | Bitwarden self-hosted | Datos sensibles en `vaultwarden/data/` (gitignored). |
| `nginx-proxy-manager` | Reverse proxy + Let's Encrypt | |
| `node-exporter`, `cadvisor`, `blackbox-exporter` | Exporters de Prometheus | cAdvisor **debe** correr con `-docker_only=true` (si no, reporta todo el cgroup tree del host, no solo contenedores). |
| `prometheus` | Métricas, retención 730 días | Retención larga es a propósito: los backups mensuales se guardan para siempre y las gráficas de tamaño/histórico deben cubrir esa escala. |
| `grafana` | Dashboards en `https://grafana.kikeramirez.org` | Ver reglas de diseño más abajo. |
| `tailscale` | VPN mesh / acceso remoto | Anuncia ruta a la LAN. |

## Estructura del repo

- `docker-compose.yaml`, `.env` (no versionado, ver `.env.example`)
- `backups/` — scripts de backup/restore + métricas custom (ver abajo)
- `grafana/provisioning/{datasources,dashboards,alerting}/`
- `prometheus/prometheus.yml`, `blackbox/config.yml`
- `homeassistant/config/` — solo se versiona configuración "de código" (`configuration.yaml`, `automations.yaml`, `scenes.yaml`, `scripts.yaml`), el resto (DBs, `.storage`, `secrets.yaml`, logs) está en `.gitignore`
- `pihole/`, `vaultwarden/`, `nginx-proxy-manager/`, `tailscale/` — bind mounts de datos runtime, todo gitignored salvo lo que sea config declarativa

## Backups y restore (`backups/`)

- Motor: **restic** (cifrado, deduplicado), repo en USB externo montado en `/media/home/home-backups`.
- `backup.sh` (cron 03:30): nightly, tag `nightly`, rotación 7d/4w/6m.
- `backup-monthly.sh` (cron día 1 00:00): `monthly-full`, **conservado para siempre** (`--keep-tag monthly-full`), incluye además `docker-compose.yaml`, `.env`, config Prometheus/Blackbox, estado Tailscale y BD de Grafana.
- `backup-offsite.sh` (cron día 1 01:30, tras el mensual): copia los snapshots `monthly-full` a un segundo repo restic en Google Drive (`rclone:gdrive:home-backups-offsite`, remote `gdrive` configurado con credenciales OAuth propias en `rclone config`, no las compartidas de rclone — la cuota compartida por defecto da `rateLimitExceeded` constante). Cubre pérdida total del USB local (3-2-1). Usa `restic` nativo (`~/.local/bin/restic`), no el contenedor `restic/restic` — esa imagen no trae el backend rclone.
- `restic-check.sh` (cron día 15, 02:00): verifica integridad de ambos repos. Local con `--read-data-subset=10%` (barato, disco local); offsite solo estructura/índices, sin descargar datos (evita gastar cuota de la API de Drive). Si el proceso se mata a medias (p. ej. `kill`), puede dejar un lock huérfano en el repo — se libera con `restic unlock`.
- `backups/logrotate.conf` (instalado en `/etc/logrotate.d/homelab`, no vía cron propio — lo dispara `/etc/cron.daily/logrotate` de Ubuntu): rota todos los `backups/*.log` + `telegram-agent/agent.log`, semanal, 8 semanas, comprimido. Lleva `su home home` porque `backups/` es `775`, no solo escribible por root.
- `docker-prune.sh` (cron domingos 04:00): `docker system prune -f` — contenedores parados, redes huérfanas, imágenes dangling y build cache. No toca imágenes con tag en uso ni volúmenes.
- `restore.sh`: restaura un snapshot completo sobre el sistema en vivo. Crea automáticamente un snapshot `pre-restore` de seguridad, para el stack, restaura, y lo levanta de nuevo.
- `webhook-server.py` (puerto 8088, en el **host** vía servicio systemd `webhook-server.service`, no contenedorizado a propósito para evitar Docker-in-Docker): expone `backup-now`/`restore`/`restore-confirm` para el dashboard "Backups". Token en `.env`, **nunca** se escribe en JSON de dashboards (se detectó como fuga de credencial una vez) — el dashboard usa una template variable de tipo textbox vacía que cada usuario rellena una vez y guarda como bookmark.
- `container-health-metrics.sh` (cron cada minuto) y `network-metrics.sh` (Pi-hole) alimentan el textfile collector de node-exporter.

> ⚠️ **`restore.sh` sobrescribe datos en producción.** Aunque crea un `pre-restore` automático, nunca lo ejecutes sin que el usuario lo pida explícitamente y confirme.
> ⚠️ **`restic forget --prune`** borra snapshots de verdad. Los scripts ya tienen la política correcta (`--keep-tag monthly-full`) — no ejecutes `forget`/`prune` manualmente salvo que se pida y se entienda qué se va a borrar.

## Monitorización y alertado

- **Prometheus** (`prometheus/prometheus.yml`): scrapea node-exporter, cAdvisor, blackbox-exporter (ICMP + HTTP `http_2xx`), Home Assistant vía `/api/prometheus` (token en `prometheus/ha-token.txt`, gitignored), y Grafana/sí mismo.
- **Alerting** (`grafana/provisioning/alerting/rules.yaml`): disco lleno, cualquier target caído, contenedores en crash-loop o parados (`container_docker_running`, custom), backup atrasado/USB sin espacio, Pi-hole/Vaultwarden/NPM/Home Assistant/webhook-server inalcanzables, check de integridad restic (local u offsite) sin éxito en >40 días. Notifica por email.
- **`backups/daily-report.py`** (cron 08:00): correo diario con el estado general. Deriva los "problemas" parseando `grafana/provisioning/alerting/rules.yaml` y evaluando cada regla contra Prometheus con su propio umbral (`check_problems()`) — no duplica umbrales a mano, esa es la única fuente de verdad. Solo mantiene aparte un diccionario `HINTS` (uid → pista en español) porque eso no tiene sentido en `rules.yaml`. Requiere `python3-yaml` (`apt install python3-yaml`) en el host.
- **Filtro `$container`** en los dashboards Contenedores/Homelab Overview: hace match contra la label de docker-compose (`container_label_com_docker_compose_project_config_files`), no contra un prefijo de nombre. Su regex ancla `name=` a un boundary de label (`[{,]\s*name="..."`) — si tocas ese regex y lo dejas sin anclar, puede hacer match con labels que no son el nombre real (p. ej. `container_label_io_hass_base_name`).
- Los dashboards/alertas de Grafana viven en dos sitios: el JSON provisionado (`grafana/provisioning/dashboards/`, lo que versiona este repo) y el volumen runtime `grafana_data` (cambios hechos desde la UI, no versionados). Si editas el JSON, comprueba que no diverge de lo que hay ya cargado en la UI — el backup mensual cubre ambos, pero este repo solo refleja el primero.

## Reglas de diseño de los dashboards de Grafana

El usuario tiene gustos muy específicos y ya validados para el suite de dashboards (Home, Homelab Overview, Network, Contenedores, Sistema (detalle), Backups). Al tocar cualquier panel HTML custom o layout:

- **Estilo sobrio, nativo de Grafana**: nada de gradientes ni sombras fuertes. Fondos sutiles `rgba(204,204,220,0.05-0.12)` sobre el tema oscuro, bordes 1px, badges pequeños en vez de iconos grandes.
- **Paleta semántica fija**: verde `#30D158`, naranja `#FF9F0A`, rojo `#FF453A`, azul `#5794f2`, cian `#33b1e0`, morado `#b877d9`.
- **Tipografía canónica** para paneles HTML: `font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif`; h3 `13px/600/#ccccdc`; texto secundario `11.5px/#8e8e9c/line-height:1.4`; header de tabla `10.5px/600/uppercase/letter-spacing:.03em/#8e8e9c`; celda `12px` (`#ccccdc/600` etiqueta, `#8e8e9c` detalle); badge `16px`.
- **Sin filas colapsables** (`"collapsed": false` siempre) en los 5 dashboards custom — excepción única: `node-exporter-full.json` (dashboard comunitario importado de 140 paneles), donde solo se aplica la tipografía y se deja el resto intacto.
- **Filas a ancho completo** (`gridPos.w` suma 24) y **sin scroll interno** en ningún panel — dimensiona `h` para que quepan todas las filas/series/barras. Tras cualquier cambio de layout, verifica que no haya `gridPos` solapados y que cada fila sume 24.
- **Info más valiosa primero**: fila de estado/KPIs compacta siempre visible arriba; el resto en detalle debajo (sin colapsar, ver punto anterior).
- **Diseños bloqueados explícitamente por el usuario** (p. ej. Home / `home-launcher`) no se tocan en panels/layout sin preguntar primero — un bloqueo dura hasta que el usuario diga lo contrario, así que confírmalo si no estás seguro de si sigue vigente. Metadata funcional (añadir un link de navegación) sí es válida sin preguntar.
- Todos los dashboards deben estar cross-linked entre sí vía `links[]`.

## Secretos — nunca versionar

`.env`, `backups/restic-password.txt`, `prometheus/ha-token.txt`, cualquier DB/estado runtime (`vaultwarden/data/`, `pihole/etc-pihole/`, `nginx-proxy-manager/{data,letsencrypt}/`, `tailscale/state/`, `homeassistant/config/{.storage,.cloud,secrets.yaml,*.db*}`), logs y locks de `backups/`. Antes de un `git add` amplio, revisa `git status` — es fácil arrastrar algo de esta lista sin querer.

## Cómo trabajar en este repo

- **Peticiones directas** ("añade X", "arregla Y", "quita Z"): actúa directamente, verifica, y reporta qué cambió. El usuario es de infra y está cómodo con reinicios/recreación de contenedores, reescritura de servicios del compose, cambios de crontab, etc. sin aprobación previa por adelantado.
- **Peticiones abiertas** ("proponme ideas", "¿qué opinas de...", decisiones poco reversibles de arquitectura): compara opciones reales, da una recomendación, y confirma 2-3 decisiones concretas antes de implementar (usa preguntas si hace falta).
- Prioriza el diagnóstico de causa raíz sobre el parche superficial, y explica el mecanismo del fix, no solo "ya está arreglado".
- Hay una lista de auditoría pendiente (hallazgos de una revisión completa del repo) que se trabaja de uno en uno; si el usuario dice "sigamos con la auditoría" o similar, retómala desde el estado guardado en memoria en vez de re-auditar todo desde cero.

## Estilo de conversación

- Siempre pregunta todo lo que necesites saber antes de darme una respuesta.
- Sé conciso, ve al grano. Responde solo a lo que pregunto y solo da la información necesaria y básica para la respuesta. No quiero respuestas largas.
- Me gusta trabajar con este estilo: partir de un draft (que puede ser inexacto) y mediante muchas consultas rápidas ir iterando hasta llegar a la solución final. 
- Recuerdame siempre si hay tareas pendientes de terminar antes de emprender otra.
- Si pido una tarea y observas que se puede hacer mejor, avísame antes de empezar proponiéndome una alternativa mejor.
- Pregúntame siempre lo necesario para entender el trabajo que estamos haciendo en este repo: misión, objetivos, valores, estilo, reglas, permisos... todo lo que necesites saber.

## Comandos útiles

```bash
# Estado de los contenedores
docker compose ps
docker compose logs -f <servicio>

# Snapshots de restic (repo en USB externo)
docker run --rm -v /media/home/home-backups/home-backups:/repo \
  -v /home/home/home/backups/restic-password.txt:/repo-password:ro \
  -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest snapshots

# Crons instalados
crontab -l

# Validar sintaxis del compose sin levantar nada
docker compose config -q
```
