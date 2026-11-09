"""Conservative cut-list extraction from millimetre DXF model space."""
import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf.math import Vec3

UNKNOWN = "미확인"
TOLERANCE = 0.01
NUMBER = r"[0-9]+(?:\.[0-9]+)?"
PROFILE = rf"{NUMBER}(?:\s*[xX×*]\s*{NUMBER}){{1,2}}"
PROFILE_NOTE = re.compile(rf"({PROFILE})\s*\n\s*보이는\s*면(?:이|\s*[:=])?\s*({NUMBER})")
FIELDS = {
    "규격": ("material_spec", PROFILE, str),
    "길이": ("length_mm", rf"({NUMBER})\s*(?:mm)?", float),
    "좌절단각": ("left_angle_deg", rf"({NUMBER})\s*(?:°|도)?", float),
    "우절단각": ("right_angle_deg", rf"({NUMBER})\s*(?:°|도)?", float),
    "보이는면": ("visible_face_mm", rf"({NUMBER})\s*(?:mm)?", float),
    "구분": ("category", r"벨트|옆전", str),
    "수량": ("quantity", r"([0-9]+)\s*(?:ea|개)?", int),
}


def near(a, b):
    return abs(a - b) <= TOLERANCE


@dataclass(frozen=True)
class Segment:
    handle: str
    start: Vec3
    end: Vec3

    def axis(self):
        if not near(self.start.z, 0) or not near(self.end.z, 0):
            return None
        if near(self.start.y, self.end.y) and not near(self.start.x, self.end.x):
            return 0
        if near(self.start.x, self.end.x) and not near(self.start.y, self.end.y):
            return 1
        return None

    def span(self, axis):
        return sorted((self.start[axis], self.end[axis]))


def same_point(a, b):
    return a.distance(b) <= TOLERANCE


def profile(value):
    return re.sub(r"\s*[xX×*]\s*", "×", value)


def explicit_fields(lines, metric):
    candidates = {}
    for line in lines:
        match = re.fullmatch(r"([^:=]+)[:=]\s*(.+)", line)
        if not match:
            continue
        label, raw = match.groups()
        field = FIELDS.get("".join(label.split()))
        if not field:
            continue
        key, pattern, convert = field
        value = re.fullmatch(pattern, raw.strip(), re.IGNORECASE)
        if not value:
            candidates.setdefault(key, set()).add(UNKNOWN)
            continue
        parsed = convert(value[1] if value.groups() else value[0])
        if key == "material_spec":
            parsed = profile(parsed)
        if isinstance(parsed, (float, int)):
            if key.endswith("_deg"):
                valid = 0 <= parsed <= 180
            else:
                valid = parsed > 0 and (isinstance(parsed, int) or math.isfinite(parsed))
            if not valid:
                parsed = UNKNOWN
        if key.endswith("_mm") and not metric and not raw.lower().endswith("mm"):
            parsed = UNKNOWN
        candidates.setdefault(key, set()).add(parsed)
    return {
        key: next(iter(values)) if len(values) == 1 else UNKNOWN
        for key, values in candidates.items()
    }


def leader_tip(note, segments):
    """Resolve a unique two-line callout with a horizontal landing left of the note."""
    if note.dxf.attachment_point != 1 or not near(note.get_rotation(), 0):
        return None
    anchor = note.dxf.insert
    height = note.dxf.char_height
    candidates = []
    for landing in segments:
        if landing.axis() != 0:
            continue
        left, right = sorted((landing.start, landing.end), key=lambda point: point.x)
        if not (0 <= anchor.x - right.x <= height / 2
                and 0 <= anchor.y - right.y <= height / 2):
            continue
        for pointer in segments:
            if pointer.axis() is not None:
                continue
            for joint, tip in ((pointer.start, pointer.end), (pointer.end, pointer.start)):
                if same_point(left, joint) and tip.x < joint.x and near(tip.z, 0):
                    candidates.append((tip, landing.handle, pointer.handle))
    return candidates[0] if len(candidates) == 1 else None


def member_edges(tip, face, segments):
    candidates = []
    for axis in (0, 1):
        cross = 1 - axis
        edges = [
            edge for edge in segments
            if edge.axis() == axis
            and edge.span(axis)[0] < tip[axis] < edge.span(axis)[1]
        ]
        for lower in edges:
            if lower.start[cross] >= tip[cross]:
                continue
            for upper in edges:
                if upper.start[cross] <= tip[cross]:
                    continue
                if near(upper.start[cross] - lower.start[cross], face):
                    candidates.append((axis, lower, upper))
    return candidates[0] if len(candidates) == 1 else None


def member_dimensions(axis, edges, dimensions):
    """Require dimension extension origins at the endpoints of a member edge."""
    matches = []
    for dimension in dimensions:
        if dimension.dimtype not in (0, 1) or dimension.dxf.text.strip() not in ("", "<>"):
            continue
        measurement = dimension.get_measurement()
        if not isinstance(measurement, (int, float)) or not math.isfinite(measurement):
            continue
        a, b = dimension.dxf.defpoint2, dimension.dxf.defpoint3
        if not near(abs(a[axis] - b[axis]), measurement) or measurement <= 0:
            continue
        if any(
            (same_point(a, edge.start) and same_point(b, edge.end))
            or (same_point(b, edge.start) and same_point(a, edge.end))
            for edge in edges
        ):
            matches.append((round(measurement, 6), dimension.dxf.handle))
    return matches


def native_length(note, face, segments, dimensions):
    if note.dxftype() != "MTEXT":
        return None
    linked = leader_tip(note, segments)
    if not linked:
        return None
    tip, landing, pointer = linked
    member = member_edges(tip, face, segments)
    if not member:
        return None
    axis, lower, upper = member
    # Unequal edge spans leave the cut-length reference ambiguous.
    if not all(near(a, b) for a, b in zip(lower.span(axis), upper.span(axis))):
        return None
    matches = member_dimensions(axis, (lower, upper), dimensions)
    if not matches or len({length for length, _ in matches}) != 1:
        return None
    return matches[0][0], [
        note.dxf.handle, landing, pointer, lower.handle, upper.handle,
        *(handle for _, handle in matches),
    ]


def extract_cut_list(doc):
    model = doc.modelspace()
    metric = doc.units == ezdxf.units.MM
    segments = [
        Segment(line.dxf.handle, line.dxf.start, line.dxf.end)
        for line in model.query("LINE")
    ]
    dimensions = list(model.query("DIMENSION"))
    items, unassigned = [], []
    for note in model.query("TEXT MTEXT"):
        raw = note.plain_text().strip()
        qty = re.fullmatch(r"=?\s*([1-9][0-9]*)\s*(?:ea|개)", raw, re.IGNORECASE)
        if qty:
            unassigned.append({"quantity": int(qty[1]), "source_handle": note.dxf.handle})
            continue
        native = PROFILE_NOTE.fullmatch(raw)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not native and lines[:1] != ["절단"]:
            continue
        values = {key: UNKNOWN for key, _, _ in FIELDS.values()}
        evidence = {}
        if native:
            values["material_spec"] = profile(native[1])
            evidence["material_spec"] = [note.dxf.handle]
            if metric:
                values["visible_face_mm"] = float(native[2])
                evidence["visible_face_mm"] = [note.dxf.handle]
                length = native_length(note, values["visible_face_mm"], segments, dimensions)
                if length:
                    values["length_mm"], evidence["length_mm"] = length
        else:
            for key, value in explicit_fields(lines[1:], metric).items():
                values[key] = value
                if value != UNKNOWN:
                    evidence[key] = [note.dxf.handle]
        items.append({"source_handle": note.dxf.handle, **values, "evidence": evidence})
    return {"items": items, "unassigned_quantities": unassigned, "dxf_units": doc.units}


def analyze_dxf(path):
    return extract_cut_list(ezdxf.readfile(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze_dxf(args.dxf), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
