"""ImageToCad: sube una imagen, la IA la interpreta (preguntando dudas) y descargas DXF/DWG. Solo local (127.0.0.1)."""
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory, session
from werkzeug.security import check_password_hash

import ai
import cad

ROOT = Path(__file__).parent
WORK = ROOT / "work"
MAX_ROUNDS = 3  # rondas de preguntas antes de forzar el dibujo

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
render_lock = threading.Lock()  # pyplot no es thread-safe
sessions = {}  # ponytail: en memoria; se pierde al reiniciar. Pasar a disco si hace falta reanudar.


def load_env(path=ROOT / ".env"):
    """Lee .env sin dependencias (las variables ya definidas en el entorno tienen prioridad)."""
    if path.exists():
        for line in path.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()
CFG = {  # configuración interna (.env); el navegador nunca la ve ni la cambia
    "provider": os.environ.get("AI_PROVIDER", "anthropic"),
    "model": os.environ.get("AI_MODEL", ""),
    "api_key": os.environ.get("AI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""),
    "base_url": os.environ.get("AI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", ""),
    "effort": os.environ.get("AI_EFFORT", "medium"),
}
# Modos de análisis: lo que elige el usuario es solo el nombre; modelo y esfuerzo salen de aquí (y del .env).
# Fuera de Anthropic no hay un "modelo grande" conocido: los modos usan AI_MODEL salvo que se definan AI_MODEL_*.
STRONG = "claude-opus-5-5" if CFG["provider"] in ("anthropic", "claude_cli") else CFG["model"]
MODES = {
    "normal": {"model": CFG["model"], "effort": CFG["effort"]},
    "exhaustivo": {"model": (os.environ.get("AI_MODEL_EXHAUSTIVO") or STRONG), "effort": os.environ.get("AI_EFFORT_EXHAUSTIVO", "medium")},
    "ultra": {"model": (os.environ.get("AI_MODEL_ULTRA") or STRONG), "effort": os.environ.get("AI_EFFORT_ULTRA", "high")},
}
cad.set_oda_path(os.environ.get("ODA_PATH", ""))
ai_slots = threading.BoundedSemaphore(int(os.environ.get("AI_CONCURRENCY", 2)))  # límite de llamadas a la IA a la vez


# --- Acceso: login propio con sesión (cookie firmada). Sin AUTH_USER en el .env queda desactivado (uso local). ---
AUTH = {"user": os.environ.get("AUTH_USER", ""), "hash": os.environ.get("AUTH_PASSWORD_HASH", "")}
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)  # sin SECRET_KEY fijo, las sesiones mueren al reiniciar
app.config.update(SESSION_COOKIE_NAME="ic_sesion", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1", PERMANENT_SESSION_LIFETIME=30 * 24 * 3600)
DUMMY_HASH = "scrypt:32768:8:1$x$" + "0" * 128  # para tardar lo mismo si el usuario no existe
fails = {}  # ip -> (fallos, bloqueado_hasta); en memoria
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60


def client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr or "?"


@app.before_request
def need_login():
    if not AUTH["user"] or request.path in ("/login", "/api/login", "/api/logout") or session.get("u") == AUTH["user"]:
        return None
    if request.path.startswith("/api/"):
        return err("Sesión caducada: vuelve a entrar.", 401)
    return redirect("/login")


@app.get("/login")
def login_page():
    if AUTH["user"] and session.get("u") == AUTH["user"]:
        return redirect("/")
    return send_from_directory(ROOT / "static", "login.html")


@app.post("/api/login")
def login():
    ip = client_ip()
    n, until = fails.get(ip, (0, 0))
    if until > time.time():
        return err(f"Demasiados intentos. Espera {int((until - time.time()) // 60) + 1} min.", 429)
    body = request.get_json(silent=True) or {}
    user_ok = secrets.compare_digest(str(body.get("user", "")).encode(), AUTH["user"].encode())
    pass_ok = check_password_hash(AUTH["hash"] if user_ok else DUMMY_HASH, str(body.get("password", "")))
    if not (AUTH["user"] and user_ok and pass_ok):
        n += 1
        fails[ip] = (n, time.time() + LOCK_SECONDS if n >= MAX_FAILS else 0)
        time.sleep(1)  # frena la fuerza bruta
        return err("Usuario o contraseña incorrectos.", 401)
    fails.pop(ip, None)
    session.clear()
    session.permanent = True
    session["u"] = AUTH["user"]
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


# --- Límites de uso de la IA (cuidan la suscripción): ventana móvil de 5 h y tope de 24 h, por modo. 0 = modo desactivado. ---
def _lim(name, default):
    return int(os.environ.get(name, default))


LIMITS = {  # ventana -> (segundos, {modo: máximo de análisis}). Un análisis = una imagen subida (sus preguntas de seguimiento no cuentan)
    "5 h": (5 * 3600, {"normal": _lim("LIMIT_5H_NORMAL", 15), "exhaustivo": _lim("LIMIT_5H_EXHAUSTIVO", 4), "ultra": _lim("LIMIT_5H_ULTRA", 1)}),
    "24 h": (24 * 3600, {"normal": _lim("LIMIT_24H_NORMAL", 40), "exhaustivo": _lim("LIMIT_24H_EXHAUSTIVO", 10), "ultra": _lim("LIMIT_24H_ULTRA", 1)}),
}
quota_lock = threading.Lock()  # comprobar y anotar el uso a la vez (hay varios hilos)


def _uso_db():
    WORK.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(WORK / "uso.db")  # persiste: los reinicios no reponen el cupo
    db.execute("CREATE TABLE IF NOT EXISTS uso (ts REAL, modo TEXT)")
    db.execute("DELETE FROM uso WHERE ts < ?", (time.time() - max(w for w, _ in LIMITS.values()),))
    return db


def usage(db=None):
    """Para cada modo y ventana: usadas, máximo y cuándo se libera la más antigua."""
    own = db is None
    db = db or _uso_db()
    now, out = time.time(), {}
    for modo in MODES:
        out[modo] = {}
        for name, (secs, caps) in LIMITS.items():
            rows = [r[0] for r in db.execute("SELECT ts FROM uso WHERE modo=? AND ts>? ORDER BY ts", (modo, now - secs))]
            out[modo][name] = {"usado": len(rows), "limite": caps[modo], "libera": rows[0] + secs if rows else None}
    if own:
        db.close()
    return out


# --- Consumo real de tokens: una línea JSON por llamada en work/consumo.jsonl (solo interno, no se muestra en la web).
# Sirve para vigilar el coste por modo; el proveedor no siempre informa de lo que queda. ---
def record_usage(cfg, u):
    linea = {"ts": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds"), "modo": cfg.get("mode", "?"),
             "modelo": cfg.get("model", ""), **u}
    WORK.mkdir(parents=True, exist_ok=True)
    with quota_lock, open(WORK / "consumo.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(linea, ensure_ascii=False) + "\n")


ai.USAGE_HOOK = record_usage


def quota_take(modo):
    """Anota un análisis si hay cupo. Devuelve (mensaje_de_error | None, marca_de_tiempo | None)."""
    with quota_lock:
        db = _uso_db()
        try:
            for name, u in usage(db)[modo].items():
                if u["usado"] >= u["limite"]:
                    if u["limite"] <= 0:
                        return f"El modo {modo} está desactivado.", None
                    hora = datetime.fromtimestamp(u["libera"], ZoneInfo("Europe/Madrid")).strftime("%H:%M")
                    return f"Límite del modo {modo} alcanzado ({u['limite']} cada {name}). Podrás volver a usarlo a las {hora}.", None
            ts = time.time()
            db.execute("INSERT INTO uso VALUES (?, ?)", (ts, modo))
            db.commit()
            return None, ts
        finally:
            db.close()


def quota_refund(ts):
    """Devuelve el análisis si el primer intento falló por nuestra parte o por la IA (no se ha obtenido nada)."""
    if ts is None:
        return
    with quota_lock:
        db = _uso_db()
        try:
            db.execute("DELETE FROM uso WHERE ts = ?", (ts,))
            db.commit()
        finally:
            db.close()


def err(msg, code=400):
    return jsonify(error=msg), code


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


def fail(msg, code=400):
    return {"error": msg}, code


def _reply(sid, data, history):
    """Convierte la respuesta de la IA en preguntas pendientes o dibujo renderizado (diccionario)."""
    s = sessions[sid]
    if data["status"] == "drawing":
        cad.validate(data["drawing"])  # falla antes de confirmar el turno
        s["drawing"] = data["drawing"]  # se guarda crudo: cad.build vuelve a validar
    if data["status"] == "questions":
        if not data["questions"]:
            raise ai.AIError("La IA pidió aclaraciones sin preguntas. Reintenta.")
        if s["rounds"] >= MAX_ROUNDS:
            raise ai.AIError("La IA sigue con dudas tras varias rondas. Pulsa «Generar igualmente».")
        s["rounds"] += 1
        s["questions"] = data["questions"]
        s["history"] = history + [{"role": "assistant", "text": json.dumps(data, ensure_ascii=False)}]
        return {"sid": sid, "status": "questions", "questions": data["questions"]}
    s["history"] = history + [{"role": "assistant", "text": json.dumps(data, ensure_ascii=False)}]
    view = s["drawing"].get("view_hint", "iso")
    return {"sid": sid, "status": "drawing", "view": view, "assumptions": data["assumptions"], "dwg": cad.dwg_available(),
            "png": f"/api/preview/{sid}.png?view={view}&v={uuid.uuid4().hex[:6]}"}


def _check(data):
    """Valida el dibujo (ValueError si no vale) y devuelve sus incoherencias geométricas."""
    return cad.problems(cad.validate(data["drawing"])) if data["status"] == "drawing" else []


def _work(sid, user_text):
    """Un turno completo, en un hilo: (cuerpo, código). Si la IA falla, el historial no cambia y se puede reintentar."""
    s = sessions[sid]
    history = s["history"] + [{"role": "user", "text": user_text}]
    if not ai_slots.acquire(blocking=False):
        return fail("Servidor ocupado analizando otras imágenes. Reintenta en un momento.", 503)
    try:
        cfg = {**CFG, **MODES[s["mode"]], "mode": s["mode"]}
        nota = None
        for intento in (0, 1):  # un solo reintento, con el motivo por delante
            try:
                data = ai.ask(cfg, s["image"], history + ([{"role": "user", "text": nota}] if nota else []))
                extra = _check(data)
                if extra and intento == 0:
                    ai.log.warning("Incoherencias en el dibujo, se pide revisar: %s", extra)
                    nota = ("Antes de darlo por bueno, revisa tu dibujo: " + " ".join(extra) + " Corrígelo si es un error de lectura; "
                            "si es intencionado, mantenlo y explícalo en assumptions. Responde con el JSON completo.")
                    continue
                if extra:  # tras revisar siguen: se muestran al usuario en vez de callarlas
                    data["assumptions"] = [*data["assumptions"], *(f"[REVISAR] {p}" for p in extra)]
                return _reply(sid, data, history), 200
            except (ai.BadReply, ValueError, TypeError, AttributeError, KeyError) as e:
                if intento == 1:
                    raise
                ai.log.warning("Reintento tras respuesta no válida: %s", e)
                nota = f"Tu respuesta anterior no se pudo usar ({e}). Responde SOLO con el JSON del formato indicado, sin texto antes ni después."
        return fail("La IA no dio una respuesta utilizable.", 502)
    except ai.AIError as e:
        ai.log.warning("Error de la IA: %s", e)
        return fail(str(e), 502)
    except (ValueError, TypeError, AttributeError, KeyError) as e:  # dibujo inválido también tras reintentar
        ai.log.warning("Dibujo inválido tras reintentar: %r", e)
        return fail(f"La IA devolvió un dibujo inválido: {e}. Pulsa de nuevo «Generar igualmente».", 502)
    finally:
        ai_slots.release()


# Los análisis tardan y los proxies inversos suelen cortar las peticiones de más de ~100 s. Por eso el POST solo arranca un trabajo
# (hilo) y el navegador pregunta cada pocos segundos por /api/job/<id>. En memoria: un reinicio los pierde.
jobs = {}


def _job(jid, sid, user_text):
    s = sessions[sid]
    first = not s["history"]
    try:
        body, code = _work(sid, user_text)
    except Exception:  # nunca dejar un trabajo colgado
        ai.log.exception("Error inesperado en el trabajo %s", jid)
        body, code = fail("Error interno del servidor.", 500)
    if code >= 400 and first:
        quota_refund(s.get("quota"))  # no se obtuvo nada: el análisis no gasta cupo
    s["busy"] = False
    jobs[jid].update(state="done", body=body, code=code)


def _start(sid, user_text):
    s = sessions[sid]
    if s.get("busy"):
        return err("Esta imagen ya se está procesando.", 409)
    s["busy"] = True
    for k in [k for k, j in jobs.items() if time.time() - j["t"] > 3600]:
        del jobs[k]
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"state": "running", "t": time.time()}
    threading.Thread(target=_job, args=(jid, sid, user_text), daemon=True).start()
    return jsonify(job=jid, sid=sid), 202


@app.get("/api/job/<jid>")
def job(jid):
    j = jobs.get(jid)
    if not j:
        return err("No encuentro ese trabajo (¿se reinició el servidor?). Vuelve a intentarlo.", 404)
    if j["state"] == "running":
        return jsonify(state="running")
    return jsonify(state="done", code=j["code"], body=j["body"])


@app.post("/api/analyze")
def analyze():
    f = request.files.get("image")
    if not f:
        return err("Falta la imagen")
    modo = request.form.get("mode", "normal")
    modo = modo if modo in MODES else "normal"
    msg, ts = quota_take(modo)
    if msg:
        return err(msg, 429)
    sid = uuid.uuid4().hex[:12]
    d = WORK / sid
    d.mkdir(parents=True)
    try:
        ai.prepare_image(f.read(), d / "image.jpg")
    except ai.AIError as e:
        quota_refund(ts)
        return err(str(e))
    notes = (request.form.get("notes") or "").strip()
    first = ("Analiza la imagen. Antes de dibujar, comprueba qué dudas te quedan y pregúntalas (las notas del usuario no sustituyen tus preguntas)."
             + (f"\nNotas del usuario: {notes}" if notes else ""))
    sessions[sid] = {"image": d / "image.jpg", "history": [], "rounds": 0, "mode": modo, "quota": ts}
    return _start(sid, first)


@app.post("/api/answer")
def answer():
    body = request.get_json(force=True) or {}
    s = sessions.get(body.get("sid"))
    if not s:
        return err("Sesión desconocida", 404)
    qs, ans = s.get("questions", []), body.get("answers") or []
    lines = [f"P: {q}\nR: {(ans[i] if i < len(ans) else '') or 'no lo sé'}" for i, q in enumerate(qs)]
    if body.get("force") or s["rounds"] >= MAX_ROUNDS:
        lines.append("Genera ya el dibujo (status=drawing). Lo que no sepas, supónlo y márcalo [CONFIRMAR].")
    return _start(body["sid"], "\n".join(lines))


def _doc(sid, view):
    s = sessions.get(sid)
    if not s or "drawing" not in s:
        return None
    return cad.build(s["drawing"], view)


@app.get("/api/uso")
def uso():
    return jsonify(usage())



@app.get("/api/preview/<sid>.png")
def preview(sid):
    view = request.args.get("view", "iso")
    with render_lock:
        doc = _doc(sid, view)
        if doc is None:
            return err("Sin dibujo", 404)
        out = WORK / sid / f"{view}.png"
        cad.render_png(doc, out)
    return send_file(out, mimetype="image/png")


@app.get("/api/download/<sid>.<ext>")
def download(sid, ext):
    if ext not in ("dxf", "dwg"):
        return err("Formato desconocido", 404)
    view = request.args.get("view", "iso")
    with render_lock:
        doc = _doc(sid, view)
        if doc is None:
            return err("Sin dibujo", 404)
        out = WORK / sid / f"plano_{view}.{ext}"
        try:
            cad.save(doc, out)
        except Exception as e:  # ODA ausente o fallo de conversión
            return err(f"No se pudo generar el {ext.upper()}: {e}", 500)
    return send_file(out, as_attachment=True, download_name=f"plano_{view}.{ext}")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5055)))
