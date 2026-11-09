"""Plan stock cuts from a reviewed image-derived cut list."""
import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

MAX_PIECES = 40
SEARCH_LIMIT = 100_000


class CutRow(BaseModel):
    name: str = Field(min_length=1)
    material: str = Field(min_length=1)
    length_mm: Decimal = Field(gt=0, max_digits=9, decimal_places=3)
    pieces_per_frame: int = Field(gt=0, le=MAX_PIECES)


class Stock(BaseModel):
    length_mm: Decimal = Field(gt=0, max_digits=9, decimal_places=3)
    kerf_per_piece_mm: Decimal = Field(default=Decimal(0), ge=0, max_digits=9, decimal_places=3)
    end_trim_per_bar_mm: Decimal = Field(default=Decimal(0), ge=0, max_digits=9, decimal_places=3)


class PlanRequest(BaseModel):
    frame_quantity: int = Field(gt=0, le=MAX_PIECES)
    cuts: list[CutRow] = Field(min_length=1, max_length=MAX_PIECES)
    stock: dict[str, Stock]


def pack(pieces, capacity, kerf):
    """Minimize stock count, then prefer longer reusable offcuts."""
    # Keep piece indexes with each load so the response retains the cut-row names.
    best = []
    for index, piece in enumerate(pieces):
        size = piece.length_mm + kerf
        for bar, (load, indexes) in enumerate(best):
            if load + size <= capacity:
                best[bar] = (load + size, [*indexes, index])
                break
        else:
            best.append((size, [index]))
    best_key = (len(best), sorted(load for load, _ in best))
    loads, assignments, visited = [], [], set()
    search_complete = True

    def search(index):
        nonlocal best, best_key, search_complete
        state = (index, tuple(sorted(loads)))
        if state in visited:
            return
        if len(visited) >= SEARCH_LIMIT:
            search_complete = False
            return
        visited.add(state)
        if index == len(pieces):
            key = (len(loads), sorted(loads))
            if key < best_key:
                best_key = key
                best = [(load, row[:]) for load, row in zip(loads, assignments)]
            return
        size = pieces[index].length_mm + kerf
        tried = set()
        for bar, load in enumerate(loads):
            if load in tried or load + size > capacity:
                continue
            tried.add(load)
            loads[bar] += size
            assignments[bar].append(index)
            search(index + 1)
            assignments[bar].pop()
            loads[bar] -= size
        if len(loads) < len(best):
            loads.append(size)
            assignments.append([index])
            search(index + 1)
            assignments.pop()
            loads.pop()

    search(0)
    return sorted(best, key=lambda bar: -bar[0]), search_complete


def plan_stock(request: PlanRequest):
    piece_count = sum(row.pieces_per_frame for row in request.cuts) * request.frame_quantity
    if piece_count > MAX_PIECES:
        raise ValueError(f"한 번에 절단 부재 {MAX_PIECES}개까지 계산할 수 있습니다.")

    groups = {}
    for row in request.cuts:
        groups.setdefault(row.material, []).extend([row] * (row.pieces_per_frame * request.frame_quantity))

    results = {}
    for material, rows in groups.items():
        stock = request.stock.get(material)
        if stock is None:
            raise ValueError(f"{material} 원자재 한 본의 길이가 필요합니다.")
        capacity = stock.length_mm - stock.end_trim_per_bar_mm
        rows.sort(key=lambda row: -row.length_mm)
        if rows[0].length_mm + stock.kerf_per_piece_mm > capacity:
            raise ValueError(f"{material} 부재가 원자재 한 본의 사용 가능 길이를 초과합니다.")
        packed, optimal = pack(rows, capacity, stock.kerf_per_piece_mm)
        bars = []
        for load, indexes in packed:
            counts = Counter((rows[i].name, rows[i].length_mm) for i in indexes)
            bars.append({
                "cuts": [
                    {"name": name, "length_mm": length, "quantity": count}
                    for (name, length), count in counts.items()
                ],
                "kerf_mm": len(indexes) * stock.kerf_per_piece_mm,
                "end_trim_mm": stock.end_trim_per_bar_mm,
                "remainder_mm": capacity - load,
            })
        results[material] = {
            "stock_length_mm": stock.length_mm,
            "stock_count": len(bars),
            "optimal": optimal,
            "cut_length_total_mm": sum(row.length_mm for row in rows),
            "remainder_total_mm": sum(bar["remainder_mm"] for bar in bars),
            "bars": bars,
        }
    return {"frame_quantity": request.frame_quantity, "materials": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        request = PlanRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
        result = plan_stock(request)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
