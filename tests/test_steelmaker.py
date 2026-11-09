import os
from pathlib import Path

import ezdxf
import pytest
from fastapi.testclient import TestClient

from api import app
from steelmaker import UNKNOWN, analyze_dxf


def save(doc, tmp_path):
    path = tmp_path / "drawing.dxf"
    doc.saveas(path)
    return path


def native_drawing():
    doc = ezdxf.new(units=ezdxf.units.MM)
    model = doc.modelspace()
    model.add_mtext(
        "100*50\\P보이는면이 50",
        dxfattribs={"insert": (95, 180), "char_height": 10},
    )
    model.add_line((80, 176), (92, 176))
    model.add_line((25, 150), (80, 176))
    model.add_line((0, 0), (0, 400))
    model.add_line((50, 0), (50, 400))
    dim = model.add_aligned_dim(p1=(0, 0), p2=(0, 400), distance=20)
    dim.render()
    model.add_mtext("=4ea", dxfattribs={"insert": (300, 0)})
    return doc, dim.dimension


def test_native_callout_uses_matching_edge_dimension(tmp_path):
    doc, dimension = native_drawing()
    result = analyze_dxf(save(doc, tmp_path))
    item, = result["items"]
    assert item["material_spec"] == "100×50"
    assert item["visible_face_mm"] == 50
    assert item["length_mm"] == 400
    assert dimension.dxf.handle in item["evidence"]["length_mm"]
    assert [item[key] for key in ("left_angle_deg", "right_angle_deg", "category", "quantity")] == [UNKNOWN] * 4
    assert result["unassigned_quantities"][0]["quantity"] == 4


@pytest.mark.parametrize("change", ["override", "ambiguous_edges", "missing_units"])
def test_native_geometry_with_insufficient_evidence(tmp_path, change):
    doc, dimension = native_drawing()
    if change == "override":
        dimension.dxf.text = "600"
    elif change == "ambiguous_edges":
        doc.modelspace().add_line((50, 10), (50, 390))
    else:
        doc.units = 0
    item, = analyze_dxf(save(doc, tmp_path))["items"]
    assert item["length_mm"] == UNKNOWN
    assert "length_mm" not in item["evidence"]


@pytest.mark.parametrize("category", ["벨트", "옆전"])
def test_explicit_cut_note_and_api(tmp_path, category):
    doc = ezdxf.new(units=ezdxf.units.MM)
    doc.modelspace().add_mtext(
        f"절단\\P규격:100×50\\P길이:600 mm\\P좌 절단각:45°\\P"
        f"우 절단각:90도\\P보이는 면:50\\P구분:{category}\\P수량:3ea"
    )
    path = save(doc, tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.post("/analyze", files={"file": ("drawing.dxf", path.read_bytes())})
        assert response.status_code == 200
        assert response.json() == analyze_dxf(path)
        item, = response.json()["items"]
        assert [item[key] for key in (
            "material_spec", "length_mm", "left_angle_deg", "right_angle_deg",
            "visible_face_mm", "category", "quantity",
        )] == ["100×50", 600, 45, 90, 50, category, 3]


def test_conflicts_and_missing_units(tmp_path):
    doc = ezdxf.new(units=0)
    doc.modelspace().add_mtext("절단\\P길이:300\\P좌 절단각:45\\P좌 절단각:90\\P수량:0")
    item, = analyze_dxf(save(doc, tmp_path))["items"]
    assert item["length_mm"] == item["left_angle_deg"] == item["quantity"] == UNKNOWN
    assert item["evidence"] == {}


@pytest.mark.parametrize(("name", "body", "status"), [
    ("drawing.dwg", b"AC1024", 415),
    ("drawing.dxf", b"invalid", 422),
    ("drawing.dxf", b"0" * (20 * 1024 * 1024 + 1), 413),
])
def test_api_rejects_invalid_input(name, body, status):
    with TestClient(app) as client:
        response = client.post("/analyze", files={"file": (name, body)})
        assert response.status_code == status


def test_supplied_drawing():
    filename = os.environ.get("STEELMAKER_SAMPLE_DXF")
    if not filename:
        pytest.skip("Set STEELMAKER_SAMPLE_DXF to the converted drawing")
    result = analyze_dxf(filename)
    assert result["items"], "Sample should contain supported annotations"
    for item in result["items"]:
        for key, value in item.items():
            if key in ("source_handle", "evidence"):
                continue
            assert (key in item["evidence"]) == (value != UNKNOWN)
    with TestClient(app) as client:
        response = client.post("/analyze", files={"file": ("drawing.dxf", Path(filename).read_bytes())})
        assert response.status_code == 200
        assert response.json() == result


def add_frame(doc, *, second_label=False):
    model = doc.modelspace()
    model.add_line((0, 0), (500, 0))
    model.add_line((500, 0), (500, 400))
    dim = model.add_linear_dim(base=(0, -30), p1=(0, 0), p2=(500, 0))
    dim.render()
    model.query("MTEXT")[-1].dxf.insert = (550, 200)
    if second_label:
        model.add_mtext("=2ea", dxfattribs={"insert": (560, 230)})


@pytest.mark.parametrize("conflict", [False, True])
def test_frame_count_links_only_unique_quantity_note(tmp_path, conflict):
    doc, _ = native_drawing()
    add_frame(doc, second_label=conflict)
    result = analyze_dxf(save(doc, tmp_path))
    item, = result["items"]
    if conflict:
        assert result["frames"] == []
        assert len(result["unassigned_quantities"]) == 2
        assert item["frame_quantity"] == item["quantity"] == UNKNOWN
    else:
        frame, = result["frames"]
        assert frame["quantity"] == item["frame_quantity"] == item["quantity"] == 4
        assert item["quantity_per_frame"] == 1
        assert frame["source_handle"] in item["evidence"]["quantity"]
        assert result["unassigned_quantities"] == []


@pytest.mark.parametrize(("declared", "total"), [("", 8), ("\\P수량:7ea", UNKNOWN)])
def test_frame_quantity_multiplies_explicit_parts_per_frame(tmp_path, declared, total):
    doc, _ = native_drawing()
    add_frame(doc)
    doc.modelspace().query("MTEXT")[0].text = "절단\\P프레임당 수량:2ea" + declared
    item, = analyze_dxf(save(doc, tmp_path))["items"]
    assert item["frame_quantity"] == 4
    assert item["quantity_per_frame"] == 2
    assert item["quantity"] == total
