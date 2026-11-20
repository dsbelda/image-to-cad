# ImageToCad

> De una foto o un croquis a un plano CAD (DXF/DWG) en minutos.

ImageToCad recibe una imagen (un croquis a mano, un plano ortogonal o una vista isométrica de una instalación de tuberías), la interpreta con un modelo de visión, **pregunta lo que no tiene claro** y genera un modelo 3D en milímetros. Desde ahí produce la vista isométrica, el alzado y la planta, con previsualización y descarga en **DXF** o **DWG**.

## Funcionalidades

- **Entrada flexible**: arrastrar y soltar una foto, escaneo o captura; las notas se pueden **dictar por voz**.
- **Diálogo con la IA**: si faltan cotas o algo es ambiguo, la app pregunta antes de dibujar.
- **Control de calidad**: valida el dibujo y busca incoherencias geométricas (tramos oblicuos, símbolos fuera de la tubería…). Si encuentra alguna, pide a la IA que lo revise una vez y, si persiste, avisa al usuario.
- **Un modelo, varias vistas**: cambiar entre isométrica, alzado y planta no vuelve a llamar a la IA. La composición coloca iconos, etiquetas y cotas sin solapes.
- **Exportación CAD**: DXF con [ezdxf](https://ezdxf.mozman.at/) y DWG mediante ODA File Converter (opcional).
- **Modos de análisis**: normal, exhaustivo y ultra, cada uno con su modelo, su esfuerzo y su cupo.
- **Proveedor de IA configurable**: Anthropic, OpenAI, Gemini, OpenRouter, Mistral, Groq, Ollama (modelos locales) o cualquier API compatible con OpenAI.
- **Listo para servidor**: login con bloqueo tras intentos fallidos, análisis en segundo plano para no chocar con los cortes del proxy, cupos de uso por modo (5 h / 24 h) que se devuelven si el análisis falla, y registro interno del consumo de tokens.
- **CLI en una burbuja**: el proveedor `claude_cli` se ejecuta dentro de [bubblewrap](https://github.com/containers/bubblewrap), sin acceso a `/home`, a los `.env` ni al resto del servidor, y con un entorno mínimo.
- Interfaz con pasos, temporizador y modo oscuro, en HTML y JavaScript sin dependencias.

## Stack

| Capa | Tecnología |
|---|---|
| Backend | Python 3, Flask, hilos para trabajos en segundo plano, SQLite (cupos) |
| CAD | ezdxf, Matplotlib (previsualización), ODA File Converter (DWG) |
| IA | SDK de Anthropic o cualquier API compatible con OpenAI (stdlib, sin SDK) |
| Seguridad | Sesión con cookie firmada, hashes de Werkzeug, bubblewrap |
| Frontend | HTML + JavaScript sin dependencias, Web Speech API |
| Tests | pytest (31 tests, sin llamadas reales a la IA) |

## Arquitectura

```
navegador ──POST /api/analyze──► app.py ──► hilo de trabajo ──► ai.py ──► proveedor IA
    │                              │                               │
    └──GET /api/job/<id> (sondeo)──┘          cad.validate + cad.problems ◄── JSON del modelo 3D
                                                       │      (reintento si hay incoherencias)
               /api/preview/<id>.png ◄── render_png ───┤
               /api/download/<id>.dxf|dwg ◄── save ────┘
```

| Endpoint | Qué hace |
|---|---|
| `POST /api/login` · `POST /api/logout` | Sesión (solo si hay `AUTH_USER`) |
| `POST /api/analyze` | Sube la imagen y arranca el análisis en el modo elegido |
| `POST /api/answer` | Responde a las preguntas de la IA y continúa |
| `GET /api/job/<id>` | Estado del trabajo: preguntas, dibujo o error |
| `GET /api/uso` | Cupo restante por modo |
| `GET /api/preview/<id>.png` | Previsualización de la vista elegida |
| `GET /api/download/<id>.<ext>` | Descarga en `dxf` o `dwg` |

## Puesta en marcha

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # elige AI_PROVIDER y rellena AI_API_KEY y AI_MODEL
.venv/bin/python app.py       # http://127.0.0.1:5055
```

Ejemplo con un modelo local en [Ollama](https://ollama.com), sin coste y sin que la imagen salga del equipo:

```bash
AI_PROVIDER=ollama
AI_MODEL=qwen2.5vl
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

Cubren la validación y la geometría del dibujo, el flujo de preguntas, los reintentos, los cupos, el login y los proveedores.

## Configuración

Todas las variables, comentadas, están en [`.env.example`](.env.example). Las principales:

| Variable | Por defecto | Uso |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | `anthropic`, `openai`, `gemini`, `openrouter`, `mistral`, `groq`, `ollama` o `claude_cli` |
| `AI_MODEL` | — | Modelo con visión; obligatorio salvo en `anthropic` y `claude_cli` |
| `AI_API_KEY` | — | Clave del proveedor (`ollama` no la necesita) |
| `AI_BASE_URL` | — | Otro servidor compatible con OpenAI (LM Studio, vLLM, Azure…) |
| `AI_MODEL_EXHAUSTIVO` / `AI_MODEL_ULTRA` | modelo grande o `AI_MODEL` | Modelo de cada modo |
| `LIMIT_5H_*` / `LIMIT_24H_*` | 15/4/1 · 40/10/1 | Análisis máximos por modo y ventana |
| `AUTH_USER` / `AUTH_PASSWORD_HASH` | — | Activan el login; sin ellos la app es de uso local |
| `ODA_PATH` | — | Ruta de ODA File Converter para exportar DWG |

Con login y sin HTTPS, pon `COOKIE_SECURE=0`: si no, el navegador no guarda la sesión y vuelve siempre al login.

Añadir otro proveedor compatible con OpenAI es una línea en `OPENAI_COMPATIBLE` (`ai.py`).

## Hoja de ruta

- [ ] Más primitivas (chaflanes, roscas, taladros pasantes)
- [ ] Exportación STEP para modelado 3D
- [ ] Sesiones persistentes en disco
- [ ] Imagen Docker

## Licencia

[MIT](LICENSE)
