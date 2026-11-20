"""Croquis de la foto en 3D (mm): línea nueva + montaje de instrumentación a 1400 mm."""
P = lambda x, y=0, z=0: {"x": x, "y": y, "z": z}
SAMPLE = {
    "title": "Croquis tuberia", "view_hint": "iso",
    "pipes": [
        {"points": [P(0, 0, 3600), P(0, 4200, 3600), P(0, 4200, 0)], "role": "nueva", "diameter": '2"'},
        {"points": [P(0, 0, 3300), P(0, 0, 1400)], "role": "nueva", "diameter": '1"'},
        {"points": [P(0, 0, 1400), P(2400, 0, 1400), P(2400, 0, 100)], "role": "instrumentacion", "diameter": '1"'},
        {"points": [P(0, 0, 1400), P(0, 0, 0)], "role": "instrumentacion", "diameter": '1"'},
    ],
    "symbols": [
        {"type": "cross_x", "x": 0, "y": 0, "z": 3600, "axis": "y", "role": "demolicion", "label": "TOMA EXISTENTE"},
        {"type": "cross_x", "x": 0, "y": 0, "z": 3300, "axis": "y", "role": "demolicion", "label": ""},
        {"type": "valve", "x": 0, "y": 0, "z": 2400, "axis": "z", "role": "nueva", "label": "V1"},
        {"type": "valve", "x": 0, "y": 4200, "z": 1900, "axis": "z", "role": "nueva", "label": "V2"},
        {"type": "valve", "x": 700, "y": 0, "z": 1400, "axis": "x", "role": "instrumentacion", "label": ""},
        {"type": "flowmeter", "x": 1550, "y": 0, "z": 1400, "axis": "x", "role": "instrumentacion", "label": "CAUDALIMETRO"},
        {"type": "gauge", "x": 1550, "y": 0, "z": 1550, "axis": "x", "role": "instrumentacion", "label": "PI"},
        {"type": "measure_point", "x": 1100, "y": 0, "z": 1400, "axis": "x", "role": "medida", "label": ""},
        {"type": "measure_point", "x": 2000, "y": 0, "z": 1400, "axis": "x", "role": "medida", "label": ""},
    ],
    "texts": [{"x": 900, "y": 0, "z": 700, "text": "5xD (D=32) = 160 mm [CONFIRMAR]"}],
    "dims": [{"p1": P(0, 0, 0), "p2": P(0, 0, 1400), "text": "", "offset": -400}],
}
