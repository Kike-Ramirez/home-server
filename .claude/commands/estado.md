---
description: Chequeo rápido de salud del homelab (contenedores, backups, alertas)
---

Haz un chequeo de salud del homelab en `/home/home/home` y resume el resultado en un párrafo corto + lista de problemas si los hay (nada de rodeos si todo está bien: dilo en una línea).

Comprueba:

1. `docker compose ps` — algún contenedor no `Up`/`healthy`.
2. `tail -20 backups/backup.log` — si el último backup nocturno (`03:30`) terminó con `OK` o `ERROR`.
3. `cat backups/metrics/backup.prom` y `cat backups/metrics/container-health.prom` — última ejecución y valores actuales de las métricas custom.
4. `curl -s http://localhost:9090/-/healthy` (Prometheus) y `curl -s http://localhost:3000/api/health` (Grafana) si están accesibles desde aquí.
5. `crontab -l` — que los 5 crons de `backups/` sigan presentes tal y como se documentan en `README.md`.

No toques nada, es solo diagnóstico. Si encuentras algo roto, no lo arregles automáticamente — repórtalo y espera instrucciones, salvo que sea algo trivial y ya reversible (p. ej. reiniciar un contenedor caído) y encaje con el estilo de trabajo directo de este repo (ver `CLAUDE.md`).
