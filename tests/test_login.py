import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import pytest
from werkzeug.security import generate_password_hash
import app as appmod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(appmod.AUTH, "user", "admin")
    monkeypatch.setitem(appmod.AUTH, "hash", generate_password_hash("clave-de-prueba"))
    monkeypatch.setattr(appmod.time, "sleep", lambda s: None)
    appmod.fails.clear()
    appmod.app.config.update(SESSION_COOKIE_SECURE=False)
    return appmod.app.test_client()


def test_sin_sesion_todo_pide_login(client):
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert client.post("/api/analyze").status_code == 401
    assert client.get("/api/preview/x.png").status_code == 401
    assert client.get("/login").status_code == 200


def test_login_correcto_y_logout(client):
    assert client.post("/api/login", json={"user": "admin", "password": "clave-de-prueba"}).status_code == 200
    assert client.get("/").status_code == 200
    client.post("/api/logout")
    assert client.get("/").status_code == 302


def test_login_incorrecto_y_bloqueo(client):
    for _ in range(appmod.MAX_FAILS):
        assert client.post("/api/login", json={"user": "admin", "password": "mala"}).status_code == 401
    # bloqueado: ni con la clave buena
    assert client.post("/api/login", json={"user": "admin", "password": "clave-de-prueba"}).status_code == 429
    assert client.get("/").status_code == 302


def test_usuario_inexistente_igual_que_clave_mala(client):
    r = client.post("/api/login", json={"user": "otro", "password": "clave-de-prueba"})
    assert r.status_code == 401 and "incorrectos" in r.get_json()["error"]
