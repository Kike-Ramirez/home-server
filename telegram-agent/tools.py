"""Tools MCP custom que el agente puede usar: Home Assistant, Prometheus,
docker restart y git commit. Todo lo que no sea lectura (ha_call_service,
docker_restart, git_commit) queda marcado como WRITE_TOOLS en agent.py para
que pase por confirmación antes de ejecutarse de verdad.
"""
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated

from claude_agent_sdk import create_sdk_mcp_server, tool

REPO_DIR = Path(__file__).resolve().parent.parent
HA_URL = "http://localhost:8123"
# Reutiliza el mismo long-lived token que ya usa Prometheus para leer
# /api/prometheus (homeassistant/ha-token.txt, gitignored) — evita generar un
# segundo token en la UI de HA.
HA_TOKEN_FILE = REPO_DIR / "prometheus" / "ha-token.txt"
PROM_URL = "http://localhost:9090"

# Servicios permitidos para docker_restart (evita reiniciar cualquier cosa,
# p.ej. el propio contenedor del stack de monitorización sin querer).
ALLOWED_RESTART_SERVICES = {
    "homeassistant", "pihole", "vaultwarden", "nginx-proxy-manager",
    "grafana", "prometheus", "node-exporter", "cadvisor", "blackbox-exporter",
    "tailscale",
}


def _ha_headers():
    token = HA_TOKEN_FILE.read_text().strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@tool("ha_get_states", "Lee el estado de entidades de Home Assistant (dispositivos, sensores, etc.)", {
    "entity_id": Annotated[str, "ID exacto (p.ej. 'climate.salon'). Déjalo vacío para listar TODAS las entidades."],
    "domain": Annotated[str, "Filtra por dominio ('light', 'switch', 'climate', 'sensor'...) al listar todas. Opcional."],
})
async def ha_get_states(args):
    entity_id = (args.get("entity_id") or "").strip()
    if entity_id.lower() in ("", "all", "*", "todas", "todos"):
        req = urllib.request.Request(f"{HA_URL}/api/states", headers=_ha_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        domain = (args.get("domain") or "").strip().lower()
        if domain:
            data = [d for d in data if d["entity_id"].startswith(f"{domain}.")]
        # Resumen compacto (entity_id + estado + nombre) para no reventar el
        # contexto con cientos de entidades; pide una entity_id concreta para
        # ver atributos completos.
        summary = [
            {
                "entity_id": d["entity_id"],
                "state": d["state"],
                "friendly_name": d.get("attributes", {}).get("friendly_name"),
            }
            for d in data
        ]
        return {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}]}

    req = urllib.request.Request(f"{HA_URL}/api/states/{entity_id}", headers=_ha_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


@tool("ha_list_services", "Lista los servicios/acciones disponibles en Home Assistant, opcionalmente de un dominio (p.ej. 'climate', 'light')", {
    "domain": Annotated[str, "Dominio a consultar ('light', 'climate', 'switch'...). Vacío para listar todos los dominios."],
})
async def ha_list_services(args):
    req = urllib.request.Request(f"{HA_URL}/api/services", headers=_ha_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    domain = (args.get("domain") or "").strip().lower()
    if domain:
        data = [d for d in data if d.get("domain") == domain]
    else:
        data = [{"domain": d.get("domain"), "services": list((d.get("services") or {}).keys())} for d in data]
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


@tool("ha_call_service", "Llama a un servicio de Home Assistant (enciende/apaga algo, crea una automatización, etc.) — ACCION CON EFECTOS REALES", {
    "domain": str, "service": str, "entity_id": str, "data": dict,
})
async def ha_call_service(args):
    url = f"{HA_URL}/api/services/{args['domain']}/{args['service']}"
    payload = dict(args.get("data") or {})
    if args.get("entity_id"):
        payload["entity_id"] = args["entity_id"]
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=_ha_headers(), method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


@tool("prometheus_query", "Consulta PromQL contra Prometheus (histórico de métricas, consumo, etc.)", {"expr": str})
async def prometheus_query(args):
    url = f"{PROM_URL}/api/v1/query?{urllib.parse.urlencode({'query': args['expr']})}"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


@tool("docker_restart", "Reinicia un contenedor del stack del homelab — ACCION CON EFECTOS REALES", {"service": str})
async def docker_restart(args):
    service = args["service"]
    if service not in ALLOWED_RESTART_SERVICES:
        return {"content": [{"type": "text", "text": f"Servicio no permitido: {service}"}], "isError": True}
    result = subprocess.run(
        ["docker", "compose", "restart", service],
        cwd=REPO_DIR, capture_output=True, text=True, timeout=60,
    )
    return {"content": [{"type": "text", "text": result.stdout + result.stderr}]}


@tool("git_commit", "Commitea cambios pendientes en el repo del homelab — ACCION CON EFECTOS REALES", {"message": str})
async def git_commit(args):
    subprocess.run(["git", "add", "-A"], cwd=REPO_DIR, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", args["message"]],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    return {"content": [{"type": "text", "text": result.stdout + result.stderr}]}


WRITE_TOOL_NAMES = {"ha_call_service", "docker_restart", "git_commit"}

homelab_tools_server = create_sdk_mcp_server(
    name="homelab",
    version="0.1.0",
    tools=[ha_get_states, ha_list_services, ha_call_service, prometheus_query, docker_restart, git_commit],
)
