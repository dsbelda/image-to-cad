"""Proveedores de IA. Contrato: ask(cfg, image_path, history) -> dict {status, questions, assumptions, drawing}.
history = lista de turnos [{"role": "user"|"assistant", "text": str}] (el primero es la petición inicial)."""
import base64
import io
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request

import anthropic
from PIL import Image

SYSTEM = """Eres un delineante CAD experto en instalaciones industriales y tuberías (P&ID, isométricos y vistas ortogonales).
Recibes una imagen: puede ser una FOTO con un croquis dibujado encima, un plano ortogonal (alzado/planta/perfil) o un isométrico.

CÓMO LEER LA IMAGEN
- Isométrico: los ejes son tres direcciones; una vertical (eje z) y dos a ±30° de la horizontal (ejes x e y). Una línea vertical es altura, las inclinadas son longitudes en planta. Los iconos (válvulas, instrumentos) aparecen deformados por la proyección: reconócelos por su forma.
- Ortogonales: cada vista muestra solo dos ejes (alzado: x,z; planta: x,y; perfil: y,z). Si hay varias vistas, cruza datos entre ellas para obtener las 3 coordenadas de cada elemento.
- Foto con croquis: hay perspectiva y no hay escala; usa las cotas escritas (mm, diámetros como 1", 2", DN32, "5xD") para fijar la escala y estima el resto por proporciones.
- Colores de un croquis a mano suelen indicar función (p. ej. rojo = tomas a anular, verde = línea nueva, azul = instrumentación, rosa = puntos de medida), pero confirma si hay duda.

GUÍA DE SÍMBOLOS (ISO 10628 / ISA 5.1) → tipo que debes usar
- valve: válvula de corte/globo/bola/compuerta. Dos triángulos unidos por el vértice (pajarita ▷◁) o un cuadrado cruzado. Una "T" o volante encima = accionamiento manual; válvula de bola con círculo.
- check_valve: antirretorno. Triángulo con una barra al final, o flecha dentro del tramo; el triángulo apunta en el sentido del flujo.
- gauge: manómetro/termómetro local. Círculo en el extremo de un palito corto que sale de la tubería (PI, TI).
- transmitter: transmisor (PT, TT, FT, LT). Círculo/rectángulo unido a la tubería por línea fina; letras dentro indican la variable.
- flowmeter: caudalímetro/contador. Rectángulo o círculo con "FE", "FT" o "M" sobre el propio tramo de tubería.
- reducer: reducción/ampliación. Trapecio o dos diámetros distintos a cada lado; anota los dos diámetros.
- flange: brida. Dos trazos paralelos cortos y perpendiculares a la tubería (o uno grueso).
- pump: bomba. Círculo con una flecha/triángulo de impulsión.
- measure_point: punto de medida o toma de muestra. Círculo pequeño relleno o con punto.
- cross_x: toma/tubería anulada, a demoler o tapada. Aspa (X) sobre la tubería.
- generic: cualquier elemento que no reconozcas con seguridad (no lo fuerces a otro tipo): ponle etiqueta con lo que parece y márcalo [CONFIRMAR].
- Siglas habituales: V = válvula, PI/PT = presión, TI/TT = temperatura, FI/FT/FE = caudal, LI/LT = nivel, PSV = válvula de seguridad, DN = diámetro nominal en mm (DN32 ≈ 1 1/4"), " = pulgadas.

PROCEDIMIENTO (síguelo mentalmente antes de responder)
1. Inventario: lista todas las líneas de tubería, todos los símbolos/instrumentos y todos los textos y cotas que se vean (también los pequeños o manuscritos). No te saltes ninguno.
2. Ejes: decide a qué eje (x, y o z) es paralelo cada tramo. Las tuberías de una instalación van casi siempre paralelas a un eje: cada tramo cambia SOLO UNA coordenada.
3. Escala: usa las cotas escritas; si una cota no cuadra con el dibujo, manda la cota escrita. Las cotas parciales deben sumar la total; si no suman, pregunta o supón y márcalo.
4. Coordenadas: fija un origen y calcula los puntos acumulando cotas. Cada codo es un punto de la polilínea; no cortes una tubería en varias si es continua.
5. Símbolos: colócalos SOBRE la tubería (su x,y,z debe estar en un tramo) con el eje del tramo en que van.
6. Revisión final: ¿sale cada símbolo del inventario en el dibujo? ¿todas las cotas del dibujo original están en dims? ¿ningún tramo es oblicuo sin motivo? ¿el diámetro de cada tubería es el que dice el dibujo?

MODELO DE SALIDA (3D, en milímetros)
- Ejes: x = derecha, y = hacia el fondo, z = arriba. Origen en un punto razonable (p. ej. suelo bajo el primer elemento).
- pipes: polilíneas de tubería con puntos x,y,z, rol (nueva, existente, instrumentacion, demolicion, medida) y diámetro (texto, p. ej. 2").
- symbols: iconos con posición x,y,z, eje de la tubería en que van (axis x/y/z) y etiqueta. Tipos: valve, check_valve, gauge, transmitter, flowmeter, reducer, flange, pump, measure_point, cross_x (toma anulada), generic.
- texts: notas. dims: cotas entre dos puntos 3D (text vacío = se mide sola; offset = separación en mm).
- view_hint: iso si el dibujo original es isométrico o la instalación es claramente tridimensional; alzado/planta si es una vista plana.

CUÁNDO PREGUNTAR
- Las notas del usuario NO sustituyen tus preguntas: tras leerlas, si aún queda cualquier duda relevante, pregunta. En la primera ronda prefiere preguntar antes que adivinar.
- Si falta algo que cambia el resultado (escala/cotas clave, diámetros ilegibles, qué es cada elemento, función de un color, a qué eje pertenece un tramo), responde status="questions" con preguntas CONCRETAS y CORTAS en español (máximo 6), numéricas o de elección cuando sea posible, y deja drawing vacío.
- No preguntes lo que puedas deducir razonablemente. Si ya tienes respuestas del usuario, úsalas y no repitas preguntas.
- Cuando tengas lo suficiente (o el usuario diga que generes ya) responde status="drawing" con el dibujo completo. Todo dato que hayas supuesto va en assumptions, y en el dibujo escribe "[CONFIRMAR]" en la etiqueta o nota correspondiente.
EJEMPLO (otro croquis, solo para ver el formato): "tubería nueva de 2": sube 3000 mm desde el suelo, gira 90° y sigue 4000 mm hacia la derecha; válvula V1 a media altura del tramo vertical"
{"status":"drawing","questions":[],"assumptions":["Vista de alzado (plano x,z) con y=0.","Origen en el pie del tramo vertical."],
 "drawing":{"title":"Tubería 2\\" en L","view_hint":"alzado",
  "pipes":[{"points":[{"x":0,"y":0,"z":0},{"x":0,"y":0,"z":3000},{"x":4000,"y":0,"z":3000}],"role":"nueva","diameter":"2\\""}],
  "symbols":[{"type":"valve","x":0,"y":0,"z":1500,"axis":"z","role":"nueva","label":"V1"}],
  "texts":[],"dims":[{"p1":{"x":0,"y":0,"z":0},"p2":{"x":0,"y":0,"z":3000},"text":"","offset":-400},{"p1":{"x":0,"y":0,"z":3000},"p2":{"x":4000,"y":0,"z":3000},"text":"","offset":400}]}}

Responde únicamente con el objeto JSON pedido."""

_pt = {"type": "object", "additionalProperties": False, "required": ["x", "y", "z"],
       "properties": {k: {"type": "number"} for k in "xyz"}}
_role = {"type": "string", "enum": ["nueva", "existente", "instrumentacion", "demolicion", "medida"]}
_str = {"type": "string"}


def _obj(props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


SCHEMA = _obj({
    "status": {"type": "string", "enum": ["questions", "drawing"]},
    "questions": {"type": "array", "items": _str},
    "assumptions": {"type": "array", "items": _str},
    "drawing": _obj({
        "title": _str,
        "view_hint": {"type": "string", "enum": ["iso", "alzado", "planta"]},
        "pipes": {"type": "array", "items": _obj({"points": {"type": "array", "items": _pt}, "role": _role, "diameter": _str})},
        "symbols": {"type": "array", "items": _obj({
            "type": {"type": "string", "enum": ["valve", "check_valve", "gauge", "transmitter", "flowmeter", "reducer",
                                                "flange", "pump", "measure_point", "cross_x", "generic"]},
            "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"},
            "axis": {"type": "string", "enum": ["x", "y", "z"]}, "role": _role, "label": _str})},
        "texts": {"type": "array", "items": _obj({"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}, "text": _str})},
        "dims": {"type": "array", "items": _obj({"p1": _pt, "p2": _pt, "text": _str, "offset": {"type": "number"}})},
    }),
})


class AIError(Exception):
    """Error de la IA con mensaje apto para mostrar al usuario."""


class BadReply(AIError):
    """La IA respondió, pero no con el formato pedido: se puede reintentar."""


log = logging.getLogger("imagetocad")
USAGE_HOOK = None  # función(cfg, uso) que anota el consumo de cada llamada (la pone app.py)


def prepare_image(raw: bytes, path, max_edge=1568):
    """Reduce y guarda como JPEG (las fotos de móvil superan el límite de la API)."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
    except Exception:
        raise AIError("No se pudo leer la imagen (formatos: PNG, JPG, WEBP).")
    img.thumbnail((max_edge, max_edge))
    img.save(path, "JPEG", quality=88)


def _parse(text):
    a, b = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[a:b + 1])
    except ValueError:
        log.warning("JSON no válido de la IA: %r", text[:500])
        raise BadReply(f"La IA no devolvió un JSON válido. Empieza así: «{text.strip()[:140]}»")
    if not isinstance(data, dict) or data.get("status") not in ("questions", "drawing"):
        log.warning("Respuesta sin 'status': %r", text[:500])
        raise BadReply("Respuesta de la IA sin 'status'.")
    data.setdefault("questions", []); data.setdefault("assumptions", [])
    return data


def _anthropic(cfg, image_path, history):
    img = base64.standard_b64encode(open(image_path, "rb").read()).decode()
    msgs = []
    for i, t in enumerate(history):
        if i == 0:  # la API no tiene estado: la imagen va en cada ronda
            content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                       {"type": "text", "text": t["text"]}]
        else:
            content = t["text"]
        msgs.append({"role": t["role"], "content": content})
    kw = {"output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    if cfg.get("effort"):
        kw["output_config"]["effort"] = cfg["effort"]
    client = anthropic.Anthropic(api_key=cfg.get("api_key") or None, base_url=cfg.get("base_url") or None)
    try:
        r = client.messages.create(model=cfg.get("model") or "claude-opus-5-5", max_tokens=16000,
                                   system=SYSTEM, messages=msgs, **kw)
    except anthropic.AuthenticationError:
        raise AIError("API key de Anthropic inválida o ausente. Revisa AI_API_KEY en el .env del servidor.")
    except anthropic.NotFoundError:
        raise AIError("Modelo no encontrado. Revisa AI_MODEL en el .env.")
    except anthropic.BadRequestError as e:
        raise AIError(f"Petición rechazada: {e.message}")
    except anthropic.RateLimitError:
        raise AIError("Límite de uso alcanzado. Espera un momento y reintenta.")
    except anthropic.APIConnectionError:
        raise AIError("Sin conexión con la API de Anthropic.")
    except anthropic.APIStatusError as e:
        raise AIError(f"Error de la API ({e.status_code}).")
    if r.stop_reason == "refusal":
        raise AIError("El modelo rechazó la petición.")
    if r.stop_reason == "max_tokens":
        raise AIError("La respuesta se cortó (demasiado grande). Simplifica la imagen.")
    return _parse(next((b.text for b in r.content if b.type == "text"), ""))


def _claude_cli(cfg, image_path, history):
    """Usa tu Claude Code instalado (suscripción) sin API key: lee la imagen con la herramienta Read."""
    convo = "\n\n".join(f"[{'USUARIO' if t['role'] == 'user' else 'TÚ'}]\n{t['text']}" for t in history)
    prompt = (f"{SYSTEM}\n\nLa imagen está en ./{image_path.name} (léela con la herramienta Read).\n\n"
              f"CONVERSACIÓN:\n{convo}\n\nResponde con el JSON del formato pedido.")
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--allowedTools", "Read",
           "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Agent"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    if cfg.get("effort"):
        cmd += ["--effort", cfg["effort"]]
    # La imagen la sube cualquiera con acceso a la web: el CLI corre en una burbuja donde solo existe la carpeta de
    # esta sesión (no ven /home, /srv, /root ni los .env). Sin bwrap no se ejecuta: mejor fallar que correr sin encierro.
    work = str(image_path.parent.resolve())
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--ro-bind", "/proc", "/proc", "--tmpfs", "/tmp",
           "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/srv", "--tmpfs", "/etc/ssl/private", "--tmpfs", "/etc/nginx",
           "--bind", work, work, "--dir", "/tmp/h", "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session",
           "--chdir", work, *cmd]
    # entorno mínimo (por el entorno y no por argumentos: así el token no sale en la lista de procesos)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp/h", "LANG": "C.UTF-8"}
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    except FileNotFoundError:
        raise AIError("Faltan 'bwrap' o 'claude' en el servidor. Instala Claude Code o usa el proveedor Anthropic API.")
    except subprocess.TimeoutExpired:
        raise AIError("Claude Code tardó demasiado.")
    try:
        env_out = json.loads(r.stdout)
    except ValueError:
        env_out = None
    if r.returncode or (isinstance(env_out, dict) and env_out.get("is_error")):
        detalle = ((env_out or {}).get("result") if isinstance(env_out, dict) else "") or r.stderr.strip() or r.stdout.strip()
        log.warning("Claude Code falló (código %s): %s", r.returncode, str(detalle)[:500])
        raise AIError(f"Claude Code falló: {str(detalle)[:300]}")
    if USAGE_HOOK and isinstance(env_out, dict):
        u = env_out.get("usage") or {}
        try:
            USAGE_HOOK(cfg, {"entrada": int(u.get("input_tokens") or 0), "salida": int(u.get("output_tokens") or 0),
                             "cache_w": int(u.get("cache_creation_input_tokens") or 0), "cache_r": int(u.get("cache_read_input_tokens") or 0),
                             "coste": float(env_out.get("total_cost_usd") or 0)})
        except Exception:  # medir nunca debe tumbar un análisis
            log.exception("No se pudo anotar el consumo")
    so = env_out.get("structured_output") if isinstance(env_out, dict) else None
    if isinstance(so, dict) and so.get("status") in ("questions", "drawing"):  # ya validado contra el esquema por el CLI
        so.setdefault("questions", []); so.setdefault("assumptions", [])
        return so
    return _parse(env_out["result"] if isinstance(env_out, dict) and "result" in env_out else r.stdout)


# Cualquier API compatible con OpenAI Chat Completions. AI_BASE_URL cambia el destino (vLLM, LM Studio, Azure…).
OPENAI_COMPATIBLE = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}


def _openai_compatible(cfg, image_path, history):
    if not cfg.get("model"):
        raise AIError("Define AI_MODEL en el .env (un modelo con visión del proveedor elegido).")
    img = base64.standard_b64encode(open(image_path, "rb").read()).decode()
    # ponytail: el esquema va en el prompt y no en response_format, que no todos los servidores aceptan;
    # si la respuesta no encaja, BadReply hace que app.py reintente una vez
    msgs = [{"role": "system", "content": f"{SYSTEM}\n\nFormato JSON obligatorio (JSON Schema):\n{json.dumps(SCHEMA)}"}]
    for i, t in enumerate(history):
        content = t["text"]
        if i == 0:
            content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}},
                       {"type": "text", "text": t["text"]}]
        msgs.append({"role": t["role"], "content": content})
    url = (cfg.get("base_url") or OPENAI_COMPATIBLE[cfg["provider"]]).rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, json.dumps({"model": cfg["model"], "messages": msgs}).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.get('api_key', '')}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            out = json.load(r)
        choice = out["choices"][0]
    except urllib.error.HTTPError as e:
        msg = {401: "API key inválida o ausente. Revisa AI_API_KEY.", 403: "API key sin permiso para este modelo.",
               404: "Modelo o URL no encontrados. Revisa AI_MODEL y AI_BASE_URL.",
               429: "Límite de uso alcanzado. Espera un momento y reintenta."}.get(e.code)
        raise AIError(msg or f"Error de la API ({e.code}): {e.read()[:300].decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise AIError(f"Sin conexión con {url}: {getattr(e, 'reason', e)}")
    except (KeyError, IndexError, ValueError):
        raise AIError("Respuesta de la API con un formato inesperado.")
    if choice.get("finish_reason") == "length":
        raise AIError("La respuesta se cortó (demasiado grande). Simplifica la imagen.")
    if USAGE_HOOK and isinstance(out.get("usage"), dict):
        u = out["usage"]
        try:
            USAGE_HOOK(cfg, {"entrada": int(u.get("prompt_tokens") or 0), "salida": int(u.get("completion_tokens") or 0),
                             "cache_w": 0, "cache_r": 0, "coste": 0.0})
        except Exception:  # medir nunca debe tumbar un análisis
            log.exception("No se pudo anotar el consumo")
    return _parse((choice.get("message") or {}).get("content") or "")


PROVIDERS = {"anthropic": _anthropic, "claude_cli": _claude_cli,
             **{name: _openai_compatible for name in OPENAI_COMPATIBLE}}


def ask(cfg, image_path, history):
    fn = PROVIDERS.get(cfg.get("provider", "anthropic"))
    if not fn:
        raise AIError(f"Proveedor desconocido: {cfg.get('provider')}. Opciones: {', '.join(PROVIDERS)}.")
    return fn(cfg, image_path, history)
