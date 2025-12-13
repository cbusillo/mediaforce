from html import escape
from typing import Iterable, Sequence


def sparkline_svg(
    values: Sequence[float],
    *,
    width: int = 260,
    height: int = 70,
    stroke: str = "#22c55e",
    stroke_width: int = 2,
    fill: str = "rgba(34, 197, 94, 0.15)",
) -> str:
    if not values:
        return ""

    max_val = max(values)
    min_val = min(values)
    span = max(max_val - min_val, 1e-9)

    pad_x = 4
    pad_y = 6
    inner_w = max(width - pad_x * 2, 1)
    inner_h = max(height - pad_y * 2, 1)

    def point(i: int, v: float) -> tuple[float, float]:
        x = pad_x + (inner_w * i / max(len(values) - 1, 1))
        normalized = (v - min_val) / span
        y = pad_y + (1 - normalized) * inner_h
        return x, y

    pts = [point(i, float(v)) for i, v in enumerate(values)]
    poly_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    area_points = f"{pad_x},{height - pad_y} " + poly_points + f" {width - pad_x},{height - pad_y}"

    return (
        f"<svg width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" "
        f"xmlns=\"http://www.w3.org/2000/svg\" role=\"img\" aria-label=\"sparkline\">"
        f"<polyline fill=\"{escape(fill)}\" stroke=\"none\" points=\"{escape(area_points)}\" />"
        f"<polyline fill=\"none\" stroke=\"{escape(stroke)}\" stroke-width=\"{stroke_width}\" "
        f"stroke-linecap=\"round\" stroke-linejoin=\"round\" points=\"{escape(poly_points)}\" />"
        f"</svg>"
    )


def normalize_series(values: Iterable[float]) -> list[float]:
    series = [float(v) for v in values]
    if not series:
        return []
    max_val = max(series)
    if max_val <= 0:
        return [0.0 for _ in series]
    return [v / max_val for v in series]
