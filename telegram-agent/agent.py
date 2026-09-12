#!/usr/bin/env python3
"""Demonio que conecta Telegram con un agente Claude (Agent SDK) para
consultar/gestionar el homelab (Home Assistant + infra en la Dell Optiplex).

Arranca como proceso en el host (no dockerizado, igual que
backups/webhook-server.py) vía systemd o cron @reboot. Reusa la sesión ya
autenticada del CLI `claude` del usuario — no necesita ANTHROPIC_API_KEY.

Solo responde al chat_id configurado en TELEGRAM_CLIENT_ID. Las tools de
solo lectura (Read, Grep, Glob, WebSearch, ha_get_states, prometheus_query)
se ejecutan directamente; el resto (Edit/Write y las tools marcadas como
WRITE_TOOL_NAMES en tools.py) se proponen por Telegram y esperan un "sí"/"no"
del usuario antes de ejecutarse.

ESQUELETO — pendiente de probar end-to-end.
"""
import asyncio
import html
import json
import re
import urllib.request
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    TextBlock,
)

from tools import homelab_tools_server

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_DIR / ".env"
AGENT_DIR = Path(__file__).resolve().parent
MEMORY_FILE = AGENT_DIR / "memory.md"
SESSION_FILE = AGENT_DIR / "session_id.txt"

READ_ONLY_TOOLS = {
    "Read", "Grep", "Glob", "WebSearch",
    "ha_get_states", "ha_list_services", "prometheus_query",
}
YES_WORDS = {"si", "sí", "yes", "confirmo", "adelante", "dale", "ok", "vale"}
NO_WORDS = {"no", "cancela", "cancelar", "para"}

# Heurística para Bash: si el comando no toca ninguno de estos patrones de
# escritura/mutación conocidos, se considera "de solo lectura" y se ejecuta
# sin pedir confirmación. Es un filtro por texto, no un sandbox real — ante
# la duda (patrón no reconocido pero pinta sospechosa) se prefiere pedir
# confirmación a dejar pasar algo por error.
BASH_WRITE_PATTERNS = [
    "rm -", " rm ", " mv ", " cp -", "dd if=", "mkfs", "chmod ", "chown ",
    "kill -", "pkill", "reboot", "shutdown", "sudo ", "systemctl ",
    "docker restart", "docker stop", "docker rm", "docker kill", "docker compose",
    "git commit", "git push", "git reset", "git checkout", "git add ", "git rm",
    " > ", ">>", "tee ", "curl -x", "curl --request", "wget ",
    "pip install", "pip3 install", "apt-get install", "apt install", "npm install",
    "'w')", '"w")', "'a')", '"a")', "os.remove", "os.unlink", "shutil.rmtree",
    ".write(", "unlink()", "truncate(",
]


def is_bash_read_only(command: str) -> bool:
    lowered = command.lower()
    return not any(pattern in lowered for pattern in BASH_WRITE_PATTERNS)


def markdown_to_telegram_html(text: str) -> str:
    """Red de seguridad: el prompt le pide a Sebastián que escriba HTML de
    Telegram directamente, pero por si se despista y cuela sintaxis Markdown
    (herencia de cómo escribe normalmente), la convertimos aquí."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def load_memory():
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text(
            "# Libreta de Sebastián\n\n"
            "Notas que conviene recordar entre conversaciones: dispositivos "
            "de la casa, preferencias de Enrique, tareas a medias, etc.\n"
        )
    return MEMORY_FILE.read_text()

def build_system_prompt(memory_text: str) -> str:
    return f"""Eres Sebastián, el mayordomo del homelab de Enrique, hablando por Telegram.
El repo del homelab está en {REPO_DIR} (docker-compose.yaml + config versionada).
Puedes leer/editar ficheros del repo, consultar Home Assistant y Prometheus,
buscar en la web, reiniciar contenedores del stack y commitear cambios.
Cualquier acción con efectos reales (editar config, reiniciar un contenedor,
crear una automatización, commitear) requiere que el usuario confirme antes
de aplicarse: descríbela con claridad antes de intentarla.
Responde en español, breve y directo.

Conocimiento del homelab:
- Antes de dar por hecho cómo está montada la infraestructura (qué servicios
  hay, dónde vive cada config, qué es seguro tocar y qué no), lee
  {REPO_DIR}/CLAUDE.md — es la referencia viva del stack completo (compose,
  backups, monitorización, reglas de diseño). No te lo sepas de memoria, léelo
  cuando lo necesites, puede haber cambiado.
- Para dispositivos/entidades de Home Assistant: usa `ha_get_states` sin
  entity_id (o con un `domain` como 'light'/'climate'/'switch') para ver qué
  hay antes de actuar sobre algo — nunca adivines un entity_id. Usa
  `ha_list_services` para saber qué acciones admite un dominio antes de
  llamar a `ha_call_service`. Si necesitas más detalle del que da la API REST
  (áreas, dispositivo asociado...), los registros de HA están en
  `homeassistant/config/.storage/core.*_registry` (léelos con Read/Grep).

Tareas complejas:
- Cuando la petición no es trivial (añadir un dispositivo nuevo, diagnosticar
  algo, crear una automatización), investiga paso a paso con las
  herramientas que tienes — lee config, mira el estado real en HA/Prometheus,
  busca en la web si hace falta (p.ej. cómo se empareja un modelo concreto) —
  antes de responder o de proponer una acción. No te quedes en la primera
  suposición si algo no cuadra (404, entidad inexistente, etc.): reformula y
  sigue investigando en vez de rendirte a la primera.

Honestidad y cautela:
- Si te falta información para responder o actuar con seguridad (no
  encuentras el dato, la herramienta no te lo da, no estás seguro de qué
  entidad/servicio es el correcto), dilo explícitamente en vez de estimar o
  inventar una respuesta plausible. Un "esto no lo sé seguro" es siempre
  mejor que un dato inventado.
- Si la pregunta trata algo que no puedes verificar con lo que hay en este
  repo, en Home Assistant o en Prometheus (p.ej. especificaciones de un
  dispositivo, cómo se empareja un modelo concreto, un dato externo
  cualquiera), contrástalo con WebSearch antes de responder. Si tampoco
  encuentras una fuente pública fiable que lo confirme, dilo abiertamente
  ("no he podido comprobarlo") en vez de rellenar el hueco con una suposición.
- Antes de emprender cualquier acción (aunque sea de las que requieren
  confirmación), si hay ambigüedad o falta un dato relevante para hacerla
  bien (qué dispositivo exacto, qué umbral, qué habitación), pregúntalo
  primero en vez de asumir y proponer algo que podría no ser lo que quiere.

Memoria — tu libreta ({MEMORY_FILE}):
- Ahí guardas lo que conviene recordar entre conversaciones: dispositivos de
  la casa y sus entity_id, preferencias de Enrique, tareas que quedaron a
  medias. Consúltala si te hace falta contexto de antes; sus notas actuales:

{memory_text}

- Actualízala con Edit/Write cuando aprendas algo que valga la pena recordar
  a largo plazo (no la satures con trivialidades de una sola conversación).
  Esta libreta es de bajo riesgo y no necesita tu confirmación para editarse.

Personalidad — Sebastián, mayordomo clásico:
- Porte exquisito de mayordomo de toda la vida (piensa en Alfred o Jeeves),
  pero con lenguaje actual: tutea a Enrique y habla como alguien de hoy, no
  como un sirviente victoriano — nunca "señor", "señora" ni "usted". La
  elegancia va en el estilo y el vocabulario cuidado, no en fórmulas de
  tratamiento anticuadas.
- Bajo esa elegancia es socarrón y un punto gamberro: comentarios secos con
  retranca, ironía fina, alguna pulla cariñosa — nunca vulgar ni payaso.
  Se permite más soltura cuando todo va bien o la petición es trivial.
- Cuando hay un problema de verdad (algo roto, una confirmación de una
  acción con efectos reales, una alerta) se pone serio al instante: sigue
  siendo cortés pero corta el chascarrillo y va directo al grano — un buen
  mayordomo sabe cuándo dejar de bromear.
- Nunca rompe personaje ni menciona que es un modelo de IA o un script.

Formato de los mensajes (Telegram, parse_mode HTML). IMPORTANTE: el mensaje
se envía tal cual a la API de Telegram con parse_mode=HTML — SOLO entiende
estas etiquetas literales, nada de sintaxis Markdown:
- Negrita: <b>texto</b> — NUNCA **texto** (eso sale literal con los asteriscos).
- Código/valores técnicos: <code>texto</code> — NUNCA `texto` con backticks.
- Enlaces: <a href="https://ejemplo.com">texto del enlace</a> — NUNCA la
  sintaxis Markdown [texto](url), que en Telegram no se convierte en link y
  se ve tal cual con corchetes y paréntesis.
- Cursiva: <i>texto</i> — nunca _texto_ ni *texto*.
- No uses listas con "- " ni "* " al principio de línea como en Markdown;
  usa un emoji o "•" como viñeta si hace falta una lista.
- No existen etiquetas de color ni tamaño de fuente: el único "color" es el
  emoji semáforo de abajo.

Estilo visual:
- <b>Negrita</b> para títulos y etiquetas, <code>valores</code> para
  cifras/técnicos (temperaturas, nombres de contenedor, IDs).
- Simula "color" con emoji semáforo, igual que en los dashboards de Grafana:
  🟢 todo bien, 🟠 aviso, 🔴 crítico/error. Uno por línea de estado, no más.
- Un emoji temático en el título ayuda (🏠 HA, 📦 contenedores, 💾 backups,
  🌡️ temperatura, 🔌 dispositivo, ⚠️ confirmación) pero sin abusar — elegante,
  no ruidoso. Nunca emoji decorativo sin motivo semántico.
- Estructura corta: título en negrita, luego líneas con viñetas si hay varios
  datos; una sola línea si es un dato puntual. Evita bloques largos de texto.
- Un toque de personalidad/humor breve al final está bien de vez en cuando,
  pero no lo fuerces en cada mensaje."""


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


class TelegramBot:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id)
        self.api = f"https://api.telegram.org/bot{token}"
        self.offset = 0

    def _post(self, method, payload):
        req = urllib.request.Request(
            f"{self.api}/{method}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def send(self, text, reply_markup=None):
        """Devuelve el message_id enviado (útil para editarlo luego, p.ej.
        al resolver una confirmación con botones)."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._post("sendMessage", payload)
        return result.get("result", {}).get("message_id")

    def edit_message(self, message_id, text):
        self._post("editMessageText", {
            "chat_id": self.chat_id, "message_id": message_id,
            "text": text, "parse_mode": "HTML",
        })

    def answer_callback(self, callback_query_id, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)

    def send_chat_action(self, action="typing"):
        """El "Sebastián está escribiendo..." nativo de Telegram. Caduca a
        los ~5s, hay que refrescarlo mientras dure la tarea."""
        self._post("sendChatAction", {"chat_id": self.chat_id, "action": action})

    def get_updates(self):
        """Long polling (30s). Bloqueante — se llama desde un hilo/executor.
        Devuelve una lista de eventos: {"kind": "text", "text": ...} para
        mensajes normales, o {"kind": "callback", ...} para pulsaciones de
        los botones sí/no de una confirmación."""
        url = f"{self.api}/getUpdates?timeout=30&offset={self.offset}"
        with urllib.request.urlopen(url, timeout=35) as r:
            data = json.load(r)
        updates = data.get("result", [])
        events = []
        for u in updates:
            self.offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq:
                chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                if chat_id == self.chat_id:
                    events.append({
                        "kind": "callback",
                        "data": cq.get("data"),
                        "callback_query_id": cq["id"],
                        "message_id": cq.get("message", {}).get("message_id"),
                    })
                continue
            msg = u.get("message") or {}
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text")
            if text and chat_id == self.chat_id:
                events.append({"kind": "text", "text": text})
        return events


async def main():
    env = load_env()
    bot = TelegramBot(env["TELEGRAM_API_TOKEN"], env["TELEGRAM_CLIENT_ID"])

    # Puente entre el callback de confirmación (que corre dentro del loop del
    # SDK) y el polling de Telegram (que corre en el loop principal): cuando
    # hay una confirmación pendiente, el siguiente mensaje que llegue se
    # interpreta como sí/no en vez de reenviarse al agente como pregunta nueva.
    pending_confirmation: dict = {"future": None, "description": None, "message_id": None}

    CONFIRM_KEYBOARD = {
        "inline_keyboard": [[
            {"text": "✅ Sí", "callback_data": "confirm_yes"},
            {"text": "❌ No", "callback_data": "confirm_no"},
        ]]
    }

    async def can_use_tool(tool_name, input_data, context):
        if tool_name in READ_ONLY_TOOLS:
            return PermissionResultAllow()
        # La libreta de memoria es de bajo riesgo (solo notas) — se puede
        # editar sin pedir confirmación cada vez.
        if tool_name in ("Edit", "Write") and str(input_data.get("file_path", "")) == str(MEMORY_FILE):
            return PermissionResultAllow()
        # Bash de solo lectura (inspeccionar, no mutar) tampoco necesita
        # confirmación — ver BASH_WRITE_PATTERNS.
        if tool_name == "Bash" and is_bash_read_only(input_data.get("command", "")):
            return PermissionResultAllow()
        description = f"{tool_name}({json.dumps(input_data, ensure_ascii=False)})"
        message_id = bot.send(
            f"⚠️ <b>¿Te parece bien esto?</b>\n\n"
            f"Antes de tocar nada, quiero luz verde para:\n<code>{html.escape(description)}</code>",
            reply_markup=CONFIRM_KEYBOARD,
        )
        fut = asyncio.get_event_loop().create_future()
        pending_confirmation["future"] = fut
        pending_confirmation["description"] = description
        pending_confirmation["message_id"] = message_id
        approved = await fut
        pending_confirmation["future"] = None
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="El usuario rechazó la acción por Telegram.")

    previous_session = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else None

    options = ClaudeAgentOptions(
        system_prompt=build_system_prompt(load_memory()),
        cwd=str(REPO_DIR),
        mcp_servers={"homelab": homelab_tools_server},
        # OJO: cualquier tool listada aquí queda auto-aprobada SIN pasar por
        # can_use_tool (el SDK la trata como una allow-rule). Por eso solo
        # metemos las de solo lectura; las de escritura (Edit y las
        # mcp__homelab__* de WRITE_TOOL_NAMES) se dejan fuera para que caigan
        # siempre en el callback de confirmación.
        allowed_tools=[
            "Read", "Grep", "Glob", "WebSearch",
            "mcp__homelab__ha_get_states", "mcp__homelab__ha_list_services",
            "mcp__homelab__prometheus_query",
        ],
        can_use_tool=can_use_tool,
        permission_mode="default",
        # Retoma la conversación anterior si el demonio se reinició — así
        # Sebastián no pierde el hilo (memoria "de corto plazo"; la libreta
        # de memory.md es la de largo plazo, entre sesiones distintas).
        resume=previous_session,
    )

    incoming_queue: asyncio.Queue = asyncio.Queue()

    def _resolve_confirmation(approved):
        fut = pending_confirmation["future"]
        print(f"[confirmación] {'aceptada' if approved else 'rechazada'}: {pending_confirmation['description']}")
        message_id = pending_confirmation.get("message_id")
        if message_id:
            label = "✅ <b>Confirmado</b>" if approved else "❌ <b>Cancelado</b>"
            bot.edit_message(
                message_id,
                f"{label}\n\n<code>{html.escape(pending_confirmation['description'])}</code>",
            )
        fut.set_result(approved)

    async def poller():
        """Corre en paralelo al loop principal para poder resolver una
        confirmación pendiente aunque el loop principal esté bloqueado
        dentro de client.receive_response() esperando esa misma confirmación."""
        while True:
            events = await asyncio.to_thread(bot.get_updates)
            for ev in events:
                fut = pending_confirmation["future"]

                if ev["kind"] == "callback":
                    if fut is not None and not fut.done():
                        bot.answer_callback(ev["callback_query_id"])
                        _resolve_confirmation(ev["data"] == "confirm_yes")
                    else:
                        bot.answer_callback(ev["callback_query_id"], text="Ya no hay nada pendiente de confirmar.")
                    continue

                text = ev["text"]
                if fut is not None and not fut.done():
                    normalized = text.strip().lower()
                    if normalized in YES_WORDS:
                        _resolve_confirmation(True)
                    elif normalized in NO_WORDS:
                        _resolve_confirmation(False)
                    else:
                        bot.send("Sigo esperando tu <b>sí</b> o <b>no</b> sobre lo anterior (o pulsa un botón).")
                else:
                    await incoming_queue.put(text)

    async def keep_typing():
        """Refresca el indicador "escribiendo..." de Telegram cada 4s
        mientras Sebastián está liado con la consulta (caduca a los ~5s)."""
        while True:
            await asyncio.to_thread(bot.send_chat_action, "typing")
            await asyncio.sleep(4)

    async with ClaudeSDKClient(options=options) as client:
        print("Demonio arrancado, esperando mensajes de Telegram...")
        poller_task = asyncio.create_task(poller())
        try:
            while True:
                text = await incoming_queue.get()
                print(f"[recibido] {text!r}")
                await client.query(text)
                reply_parts = []
                typing_task = asyncio.create_task(keep_typing())
                try:
                    async for msg in client.receive_response():
                        if isinstance(msg, SystemMessage) and msg.subtype == "init":
                            session_id = msg.data.get("session_id")
                            if session_id:
                                SESSION_FILE.write_text(session_id)
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    reply_parts.append(block.text)
                finally:
                    typing_task.cancel()
                print(f"[respuesta final] {reply_parts!r}")
                if reply_parts:
                    bot.send(markdown_to_telegram_html("\n".join(reply_parts)))
        finally:
            poller_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
