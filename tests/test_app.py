import io, json, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import pytest
from PIL import Image
import ai, app as appmod
from tests.sample import SAMPLE


class Respuesta:
    """Lo que devuelve un trabajo terminado, con la misma forma que una respuesta de Flask."""
    def __init__(self, code, body): self.status_code, self._b = code, body
    def get_json(self): return self._b


class Cliente:
    """Cliente de pruebas que espera a que acaben los trabajos en segundo plano de /api/analyze y /api/answer."""
    def __init__(self, c): self.c = c
    def __getattr__(self, n): return getattr(self.c, n)
    def post(self, url, **kw):
        r = self.c.post(url, **kw)
        if r.status_code != 202:
            return r
        jid = r.get_json()["job"]
        for _ in range(500):
            j = self.c.get(f"/api/job/{jid}").get_json()
            if j["state"] == "done":
                return Respuesta(j["code"], j["body"])
            time.sleep(0.01)
        raise AssertionError("el trabajo no terminó")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "WORK", tmp_path / "work")
    return Cliente(appmod.app.test_client())


@pytest.fixture
def limites(monkeypatch):
    def poner(**por_modo):  # poner(normal=(5h, 24h), ...)
        for i, ventana in enumerate(("5 h", "24 h")):
            secs, caps = appmod.LIMITS[ventana]
            monkeypatch.setitem(appmod.LIMITS, ventana, (secs, {**caps, **{m: v[i] for m, v in por_modo.items()}}))
    return poner


def png():
    b = io.BytesIO(); Image.new("RGB", (4000, 3000), "white").save(b, "PNG"); b.seek(0); return b


def analizar(client, **extra):
    return client.post("/api/analyze", data={"image": (png(), "a.png"), **extra})


def test_question_then_drawing_flow(client, monkeypatch):
    calls = []
    def fake(cfg, path, history):
        calls.append(history)
        if len(history) == 1:
            return {"status": "questions", "questions": ["¿Diámetro?"], "assumptions": []}
        return {"status": "drawing", "questions": [], "assumptions": ["D=32"], "drawing": SAMPLE}
    monkeypatch.setattr(ai, "ask", fake)
    r = analizar(client).get_json()
    assert r["status"] == "questions"
    r = client.post("/api/answer", json={"sid": r["sid"], "answers": ["1 pulgada"]}).get_json()
    assert r["status"] == "drawing" and r["assumptions"] == ["D=32"]
    assert "R: 1 pulgada" in calls[1][-1]["text"] and len(calls[1]) == 3
    sid = r["sid"]
    assert client.get(r["png"]).data[:4] == b"\x89PNG"
    d = client.get(f"/api/download/{sid}.dxf?view=alzado")
    assert d.status_code == 200 and b"SECTION" in d.data
    assert (appmod.WORK / sid / "image.jpg").exists()


def test_ai_error_keeps_history_and_bad_input(client, monkeypatch):
    def boom(*a): raise ai.AIError("sin key")
    monkeypatch.setattr(ai, "ask", boom)
    r = analizar(client)
    assert r.status_code == 502 and r.get_json()["error"] == "sin key"
    assert client.post("/api/analyze", data={"image": (io.BytesIO(b"no es imagen"), "a.png")}).status_code == 400
    assert client.post("/api/analyze").status_code == 400
    assert client.get("/api/job/inexistente").status_code == 404


def test_modos_usan_su_modelo_y_el_nombre_se_valida(client, monkeypatch):
    vistos = []
    def fake(cfg, path, history):
        vistos.append((cfg["model"], cfg["effort"]))
        return {"status": "questions", "questions": ["¿?"], "assumptions": []}
    monkeypatch.setattr(ai, "ask", fake)
    analizar(client, mode="exhaustivo"); analizar(client, mode="ultra"); analizar(client, mode="inventado")
    assert vistos[0] == ("claude-opus-5-5", "medium")
    assert vistos[1] == ("claude-opus-5-5", "high")
    assert vistos[2][0] == appmod.MODES["normal"]["model"]  # un modo desconocido cae en normal


def test_limites_de_uso_por_modo(client, monkeypatch, limites):
    limites(normal=(2, 50), exhaustivo=(0, 50))
    monkeypatch.setattr(ai, "ask", lambda cfg, path, h: {"status": "questions", "questions": ["¿?"], "assumptions": []})
    assert analizar(client).status_code == 200 and analizar(client).status_code == 200
    r = analizar(client)
    assert r.status_code == 429 and "Límite" in r.get_json()["error"]
    r = analizar(client, mode="exhaustivo")
    assert r.status_code == 429 and "desactivado" in r.get_json()["error"]
    u = client.get("/api/uso").get_json()
    assert u["normal"]["5 h"]["usado"] == 2 and u["normal"]["5 h"]["limite"] == 2 and "ultra" in u


def test_ultra_una_vez_al_dia_pero_las_preguntas_no_cuentan(client, monkeypatch, limites):
    limites(ultra=(1, 1))
    def fake(cfg, path, history):
        if len(history) == 1:
            return {"status": "questions", "questions": ["¿Diámetro?"], "assumptions": []}
        return {"status": "drawing", "questions": [], "assumptions": [], "drawing": SAMPLE}
    monkeypatch.setattr(ai, "ask", fake)
    r = analizar(client, mode="ultra").get_json()
    assert r["status"] == "questions"
    assert client.post("/api/answer", json={"sid": r["sid"], "answers": ["1\""]}).get_json()["status"] == "drawing"  # no gasta otro
    assert analizar(client, mode="ultra").status_code == 429


def test_si_el_primer_intento_falla_no_gasta_cupo(client, monkeypatch, limites):
    limites(ultra=(1, 1))
    ok = {"status": "questions", "questions": ["¿?"], "assumptions": []}
    estado = {"falla": True}
    def fake(*a):
        if estado["falla"]: raise ai.AIError("La IA falló")
        return ok
    monkeypatch.setattr(ai, "ask", fake)
    assert analizar(client, mode="ultra").status_code == 502
    estado["falla"] = False
    assert analizar(client, mode="ultra").status_code == 200  # el análisis fallido se devolvió


def test_reintenta_una_vez_si_la_ia_responde_mal(client, monkeypatch):
    llamadas = []
    def fake(cfg, path, history):
        llamadas.append(history[-1]["text"])
        if len(llamadas) == 1:
            raise ai.BadReply("La IA no devolvió un JSON válido")
        return {"status": "questions", "questions": ["¿Diámetro?"], "assumptions": []}
    monkeypatch.setattr(ai, "ask", fake)
    r = analizar(client)
    assert r.status_code == 200 and len(llamadas) == 2 and "no se pudo usar" in llamadas[1]


def test_dibujo_invalido_dos_veces_da_502_legible(client, monkeypatch):
    monkeypatch.setattr(ai, "ask", lambda *a: {"status": "drawing", "questions": [], "assumptions": [], "drawing": {"pipes": "x"}})
    r = analizar(client)
    assert r.status_code == 502 and "dibujo inválido" in r.get_json()["error"]


def test_fallo_real_de_la_ia_no_se_reintenta(client, monkeypatch):
    n = []
    def fake(*a):
        n.append(1); raise ai.AIError("La IA falló: límite alcanzado")
    monkeypatch.setattr(ai, "ask", fake)
    r = analizar(client)
    assert r.status_code == 502 and len(n) == 1 and "límite" in r.get_json()["error"]


def test_incoherencias_geometricas_piden_revision_y_luego_se_avisan(client, monkeypatch):
    mal = {"status": "drawing", "questions": [], "assumptions": ["x"], "drawing": {
        "pipes": [{"points": [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 3000}], "role": "nueva", "diameter": "2\""}],
        "symbols": [{"type": "valve", "x": 2000, "y": 0, "z": 1500, "axis": "z", "role": "nueva", "label": "V1"}]}}
    notas = []
    def fake(cfg, path, history):
        notas.append(history[-1]["text"]); return mal
    monkeypatch.setattr(ai, "ask", fake)
    r = analizar(client)
    assert r.status_code == 200 and len(notas) == 2 and "revisa tu dibujo" in notas[1]
    assert any(a.startswith("[REVISAR]") and "V1" in a for a in r.get_json()["assumptions"])


def test_consumo_se_anota_en_el_log_y_no_se_expone_en_la_web(client):
    appmod.record_usage({"mode": "ultra", "model": "claude-opus-5-5"}, {"entrada": 10, "salida": 5, "cache_w": 1000, "cache_r": 5000, "coste": 0.5})
    linea = json.loads((appmod.WORK / "consumo.jsonl").read_text().splitlines()[-1])
    assert linea["modo"] == "ultra" and linea["entrada"] == 10 and linea["coste"] == 0.5 and linea["ts"].startswith("20")
    assert client.get("/api/consumo").status_code == 404


def test_proveedor_compatible_con_openai(tmp_path, monkeypatch):
    img = tmp_path / "i.jpg"; Image.new("RGB", (8, 8)).save(img, "JPEG")
    sent = {}
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def fake_urlopen(req, timeout):
        sent["url"], sent["auth"], sent["body"] = req.full_url, req.headers["Authorization"], json.loads(req.data)
        content = "```json\n" + json.dumps({"status": "questions", "questions": ["¿Escala?"]}) + "\n```"
        return Resp(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode())
    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    r = ai.ask({"provider": "gemini", "model": "m", "api_key": "k"}, img, [{"role": "user", "text": "hola"}])
    assert r["questions"] == ["¿Escala?"] and r["assumptions"] == []
    assert sent["url"].endswith("/v1beta/openai/chat/completions") and sent["auth"] == "Bearer k"
    assert sent["body"]["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with pytest.raises(ai.AIError, match="AI_MODEL"):
        ai.ask({"provider": "openai"}, img, [])
    with pytest.raises(ai.AIError, match="Opciones"):
        ai.ask({"provider": "nada"}, img, [])
