from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import app
from stock_plan import PlanRequest, plan_stock

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "frame-2500x1200.json"
MATERIAL = "50×50 각파이프"


def example():
    return PlanRequest.model_validate_json(EXAMPLE.read_text(encoding="utf-8"))


def test_reviewed_image_example():
    result = plan_stock(example())["materials"][MATERIAL]
    assert result["stock_count"] == 2
    assert result["optimal"]
    assert result["cut_length_total_mm"] == 8500
    assert result["remainder_total_mm"] == 3500
    assert [bar["remainder_mm"] for bar in result["bars"]] == [0, 3500]


def test_kerf_replans_exact_fit():
    request = example()
    request.stock[MATERIAL].kerf_per_piece_mm = 3
    result = plan_stock(request)["materials"][MATERIAL]
    assert result["stock_count"] == 2
    assert result["remainder_total_mm"] == 3485
    assert all(bar["remainder_mm"] > 0 for bar in result["bars"])


def test_frame_quantity_and_end_trim():
    request = example()
    request.frame_quantity = 2
    request.stock[MATERIAL].end_trim_per_bar_mm = 10
    result = plan_stock(request)["materials"][MATERIAL]
    assert result["stock_count"] == 4
    assert result["cut_length_total_mm"] == 17000
    assert result["remainder_total_mm"] == 6960


def test_rejects_missing_or_short_stock():
    request = example()
    request.stock[MATERIAL].length_mm = 2499
    with pytest.raises(ValueError, match="초과"):
        plan_stock(request)
    request.stock.clear()
    with pytest.raises(ValueError, match="길이가 필요"):
        plan_stock(request)


def test_stock_plan_api():
    with TestClient(app) as client:
        response = client.post("/stock-plan", json=example().model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["materials"][MATERIAL]["remainder_total_mm"] == "3500"
