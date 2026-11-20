import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import ezdxf, pytest
import cad
from tests.sample import SAMPLE


@pytest.mark.parametrize("view", cad.VIEWS)
def test_build_and_render(tmp_path, view):
    doc = cad.build(SAMPLE, view)
    cad.save(doc, tmp_path / "o.dxf")
    assert len(ezdxf.readfile(tmp_path / "o.dxf").modelspace()) > 20
    cad.render_png(doc, tmp_path / "o.png")
    assert (tmp_path / "o.png").stat().st_size > 5000


def test_iso_projection():
    assert cad.project("iso", (0, 0, 1000)) == (0, 1000)
    assert cad.project("alzado", (5, 7, 9)) == (5, 9)


def test_validate_rejects_garbage():
    with pytest.raises(ValueError):
        cad.validate({"pipes": [], "symbols": []})
    with pytest.raises(ValueError):
        cad.validate({"pipes": [{"points": [{"x": "a"}, {"x": 1}]}]})
    d = cad.validate({"symbols": [{"type": "inventado", "x": 1, "y": 2, "z": 3}]})
    assert d["symbols"][0]["type"] == "generic"


def test_snap_alinea_tramos_casi_rectos_y_quita_repetidos():
    d = cad.validate({"pipes": [{"points": [{"x": 0, "y": 0, "z": 0}, {"x": 3, "y": 0, "z": 3000}, {"x": 3, "y": 0, "z": 3000}]}]})
    assert d["pipes"][0]["points"] == [(0.0, 0.0, 0.0), (0.0, 0.0, 3000.0)]  # 3 mm de ruido alineados con z; el punto repetido desaparece


def test_el_dibujo_de_ejemplo_no_da_incoherencias():
    assert cad.problems(cad.validate(SAMPLE)) == []


def test_problems_detecta_simbolo_fuera_de_tuberia_y_tramo_oblicuo():
    ok = cad.validate({"pipes": [{"points": [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 3000}]}],
                       "symbols": [{"type": "valve", "x": 0, "y": 0, "z": 1500, "label": "V1"}]})
    assert cad.problems(ok) == []
    lejos = cad.validate({"pipes": [{"points": [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 3000}]}],
                          "symbols": [{"type": "valve", "x": 900, "y": 0, "z": 1500, "label": "V1"}]})
    assert "V1" in cad.problems(lejos)[0] and "sobre ella" in cad.problems(lejos)[0]
    diag = cad.validate({"pipes": [{"points": [{"x": 0, "y": 0, "z": 0}, {"x": 2000, "y": 0, "z": 2000}]}]})
    assert "ningún eje" in cad.problems(diag)[0]


P3 = lambda x, y=0, z=0: {"x": x, "y": y, "z": z}
DENSO = {"title": "Denso", "view_hint": "iso",
         "pipes": [{"points": [P3(0, 0, 0), P3(0, 0, 2500), P3(3000, 0, 2500)], "role": "nueva", "diameter": '2"'},
                   {"points": [P3(0, 0, 1500), P3(0, 1800, 1500)], "role": "instrumentacion", "diameter": '1"'}],
         "symbols": [{"type": t, "x": x, "y": y, "z": z, "axis": ax, "role": "nueva", "label": lab} for t, x, y, z, ax, lab in [
             ("valve", 0, 0, 600, "z", "V1 BOLA DN50"), ("check_valve", 0, 0, 900, "z", "ANTIRRETORNO"), ("flange", 0, 0, 1100, "z", "BR1"),
             ("gauge", 0, 0, 1500, "z", "PI-101"), ("transmitter", 0, 600, 1500, "y", "PT-101 TRANSMISOR"),
             ("flowmeter", 1200, 0, 2500, "x", "FT-201 CAUDALIMETRO"), ("valve", 1500, 0, 2500, "x", "V2"),
             ("measure_point", 1650, 0, 2500, "x", "M1"), ("reducer", 2400, 0, 2500, "x", '2" x 1"')]],
         "texts": [{"x": 300, "y": 0, "z": 1500, "text": "TOMA EXISTENTE A ANULAR [CONFIRMAR]"}],
         "dims": [{"p1": P3(0, 0, 0), "p2": P3(0, 0, 2500), "text": "", "offset": 100}, {"p1": P3(0, 0, 2500), "p2": P3(3000, 0, 2500), "text": "", "offset": 100},
                  {"p1": P3(0, 0, 0), "p2": P3(0, 0, 1500), "text": "", "offset": 100}]}


@pytest.mark.parametrize("view", ["iso", "alzado", "planta"])
@pytest.mark.parametrize("dibujo", [SAMPLE, DENSO], ids=["ejemplo", "denso"])
def test_etiquetas_no_se_pisan_entre_si_ni_con_los_iconos(dibujo, view):
    d = cad.validate(dibujo)
    lay = cad.layout(d, view)
    rects = [(t["xy"][0], t["xy"][1], t["xy"][0] + len(t["text"]) * t["height"] * cad.ANCHO_CAR, t["xy"][1] + t["height"] * 1.15) for t in lay["texts"]]
    for i, r in enumerate(rects):
        assert not any(cad._rect_hit(r, q) for q in rects[i + 1:]), f"textos solapados en {view}: {lay['texts'][i]['text']}"
        assert not any(cad._rect_hit(r, q) for q in lay["icon_rects"]), f"texto sobre un icono en {view}: {lay['texts'][i]['text']}"


def test_los_iconos_se_reducen_si_estan_muy_juntos_y_el_dibujo_se_construye():
    d = cad.validate(DENSO)
    lay = cad.layout(d, "alzado")
    assert min(lay["sizes"]) < lay["S0"] * 0.8 and min(lay["sizes"]) >= lay["S0"] * 0.3
    for v in ("iso", "alzado", "planta"):
        assert len(list(cad.build(DENSO, v).modelspace())) > 20
