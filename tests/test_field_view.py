"""Unit tests for Field-View rendering helpers (_fv_* functions).

All functions are pure (no Textual, no network) and fully deterministic.
Tests use plain characters (no Rich markup) to keep assertions readable.
"""

from __future__ import annotations

import pytest

from cosmergon_agent.dashboard import (
    _fv_centroid,
    _fv_parse_cells,
    _fv_render_minimap,
    _fv_render_zoom1,
    _fv_render_zoom2,
)

# ---------------------------------------------------------------------------
# _fv_parse_cells
# ---------------------------------------------------------------------------


def test_parse_cells_basic() -> None:
    raw = {"10,20": 1, "11,20": 1, "10,21": 1}
    result = _fv_parse_cells(raw)
    assert result == {(10, 20), (11, 20), (10, 21)}


def test_parse_cells_empty() -> None:
    assert _fv_parse_cells({}) == set()


def test_parse_cells_drops_malformed_keys() -> None:
    """Bad keys must be silently ignored, valid keys still parsed."""
    raw = {"10,20": 1, "bad": 1, "also,bad,key": 1, "5,5": 1}
    result = _fv_parse_cells(raw)
    assert result == {(10, 20), (5, 5)}


def test_parse_cells_negative_coords() -> None:
    """Negative coordinates are valid (outside-boundary cells from the API)."""
    raw = {"-1,0": 1, "0,-1": 1}
    result = _fv_parse_cells(raw)
    assert result == {(-1, 0), (0, -1)}


# ---------------------------------------------------------------------------
# _fv_centroid
# ---------------------------------------------------------------------------


def test_centroid_single_cell() -> None:
    assert _fv_centroid({(10, 20)}, 128, 128) == (10, 20)


def test_centroid_symmetric() -> None:
    cells = {(0, 0), (10, 0), (0, 10), (10, 10)}
    assert _fv_centroid(cells, 128, 128) == (5, 5)


def test_centroid_empty_returns_field_center() -> None:
    assert _fv_centroid(set(), 128, 128) == (64, 64)


def test_centroid_empty_non_square_field() -> None:
    assert _fv_centroid(set(), 64, 32) == (32, 16)


# ---------------------------------------------------------------------------
# _fv_render_zoom1
# ---------------------------------------------------------------------------


def test_zoom1_dimensions() -> None:
    """Output must have exactly vp_h rows, each vp_w characters wide."""
    cells: set[tuple[int, int]] = set()
    rows = _fv_render_zoom1(cells, 0, 0, 10, 5, 128, 128)
    assert len(rows) == 5
    assert all(len(r) == 10 for r in rows)


def test_zoom1_alive_cell_renders_block() -> None:
    cells = {(2, 1)}
    rows = _fv_render_zoom1(cells, 0, 0, 5, 3, 128, 128, alive_char="█", dead_char="·")
    assert rows[1][2] == "█"


def test_zoom1_dead_cell_renders_dot() -> None:
    cells: set[tuple[int, int]] = set()
    rows = _fv_render_zoom1(cells, 0, 0, 5, 3, 128, 128, alive_char="█", dead_char="·")
    assert all(ch == "·" for row in rows for ch in row)


def test_zoom1_outside_field_boundary() -> None:
    """Cells with coords outside [0, field_w) × [0, field_h) use outside_char."""
    cells: set[tuple[int, int]] = set()
    # Viewport starts at x=126, showing cols 126–130 of a 128-wide field
    rows = _fv_render_zoom1(
        cells, 126, 0, 5, 1, 128, 128,
        alive_char="█", dead_char="·", outside_char=" "
    )
    # cols 126,127 are inside; 128,129,130 are outside
    assert rows[0] == "··   "


def test_zoom1_viewport_offset() -> None:
    """Viewport correctly maps (vp_x, vp_y) offset to (col, row) in cells."""
    cells = {(50, 60)}
    # Viewport at (49, 59), size 3×3
    rows = _fv_render_zoom1(cells, 49, 59, 3, 3, 128, 128, alive_char="X", dead_char=".")
    assert rows[1][1] == "X"    # (50-49=1, 60-59=1)
    assert rows[0][0] == "."    # (49,59) — dead


# ---------------------------------------------------------------------------
# _fv_render_zoom2
# ---------------------------------------------------------------------------


def test_zoom2_dimensions() -> None:
    cells: set[tuple[int, int]] = set()
    rows = _fv_render_zoom2(cells, 128, 128, 32, 32)
    assert len(rows) == 32
    assert all(len(r) == 32 for r in rows)


def test_zoom2_empty_field_all_dead() -> None:
    cells: set[tuple[int, int]] = set()
    rows = _fv_render_zoom2(cells, 128, 128, 32, 32, alive_char="▓", dead_char="░")
    assert all(ch == "░" for row in rows for ch in row)


def test_zoom2_alive_cell_in_block() -> None:
    """A single alive cell makes its enclosing block show alive_char."""
    # Block (0,0) covers cells (0–3, 0–3) for 128/32=4 cells per block
    cells = {(1, 1)}
    rows = _fv_render_zoom2(cells, 128, 128, 32, 32, alive_char="▓", dead_char="░")
    assert rows[0][0] == "▓"


def test_zoom2_alive_cell_in_correct_block() -> None:
    """Cell at (64, 64) falls into the center block of a 32×32 output."""
    cells = {(64, 64)}
    rows = _fv_render_zoom2(cells, 128, 128, 32, 32, alive_char="▓", dead_char="░")
    # block_idx = floor(64 / 4) = 16
    assert rows[16][16] == "▓"
    # Adjacent block must be dead
    assert rows[15][15] == "░"


def test_zoom2_adaptive_size() -> None:
    """Output dimensions match out_w/out_h regardless of field size."""
    cells: set[tuple[int, int]] = set()
    rows = _fv_render_zoom2(cells, 128, 128, 20, 12)
    assert len(rows) == 12
    assert all(len(r) == 20 for r in rows)


# ---------------------------------------------------------------------------
# _fv_render_minimap
# ---------------------------------------------------------------------------


def test_minimap_line_count() -> None:
    """Returns map_h + 1 lines (header + map_h data rows)."""
    cells: set[tuple[int, int]] = set()
    lines = _fv_render_minimap(cells, 0, 0, 10, 5, 128, 128, map_w=16, map_h=8)
    assert len(lines) == 9  # 1 header + 8 rows


def test_minimap_header_starts_with_double_dash() -> None:
    cells: set[tuple[int, int]] = set()
    lines = _fv_render_minimap(cells, 0, 0, 10, 5, 128, 128)
    assert lines[0].startswith("═")


def test_minimap_data_row_width() -> None:
    cells: set[tuple[int, int]] = set()
    lines = _fv_render_minimap(cells, 0, 0, 10, 5, 128, 128, map_w=16, map_h=8)
    for row in lines[1:]:
        assert len(row) == 16


def test_minimap_viewport_marker() -> None:
    """Cells within the viewport rect use vp_char."""
    cells: set[tuple[int, int]] = set()
    # Full-field viewport → every minimap cell should be vp_char
    lines = _fv_render_minimap(
        cells, 0, 0, 128, 128, 128, 128,
        map_w=4, map_h=4,
        alive_char="▓", dead_char="·", vp_char="▒",
    )
    for row in lines[1:]:
        assert all(ch == "▒" for ch in row)


def test_minimap_alive_cell_shown() -> None:
    """Alive cells outside the viewport show alive_char."""
    cells = {(64, 64)}
    # Viewport in top-left corner (0–9, 0–9), cell at (64,64) is outside
    lines = _fv_render_minimap(
        cells, 0, 0, 10, 10, 128, 128,
        map_w=16, map_h=8,
        alive_char="A", dead_char=".", vp_char="V",
    )
    # Block for (64,64): bw=8, bh=16 → mx=8, my=4
    assert lines[5][8] == "A"   # lines[0] is header, so data row 4 is lines[5]


def test_minimap_viewport_takes_priority_over_alive() -> None:
    """Viewport marker overwrites alive_char even when cells are alive."""
    cells = {(5, 5)}  # inside viewport (0–9, 0–9)
    lines = _fv_render_minimap(
        cells, 0, 0, 10, 10, 128, 128,
        map_w=16, map_h=8,
        alive_char="A", dead_char=".", vp_char="V",
    )
    # Block (0,0) covers cells (0–7, 0–15); (5,5) is inside → vp_char wins
    assert lines[1][0] == "V"
