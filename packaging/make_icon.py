#!/usr/bin/env python3
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
"""Generate the application icon from the standard library alone.

    python packaging/make_icon.py

Writes packaging/kramer.ico (for the Windows build) and packaging/kramer.png
(handy for a Tk window icon or a README). Both are committed, so a build never
has to run this - it exists so the icon has a source rather than being a binary
blob of unknown provenance, and so it can be regenerated if the mark changes.

The artwork is the same 2x2 grid the web page carries as an inline SVG: a rounded
square in the accent green with four white rounded squares on it. No SVG parsing
is involved and no imaging library is needed, because the shapes are four rounded
rectangles - the whole renderer below is a coverage test per pixel.
"""

import struct
import zlib
from pathlib import Path

ACCENT = (0x1F, 0x7A, 0x4D)          # the accent green used throughout the UI
MARK = (0xFF, 0xFF, 0xFF)

# Geometry on a 32x32 grid, matching web/index.html.
CANVAS = 32.0
BG_RADIUS = 6.0
CELLS = [(7, 7), (17, 7), (7, 17), (17, 17)]
CELL = 8.0
CELL_RADIUS = 2.0

SIZES = (16, 32, 48, 64, 256)
SUPERSAMPLE = 4                      # 4x4 samples per pixel, enough for this


def rounded_rect_covers(x, y, left, top, width, height, radius):
    """Whether the point (x, y) falls inside a rounded rectangle.

    Straightforward: outside the bounding box is out, inside the inner cross is
    in, and the remaining four corner squares are decided by distance from the
    corresponding centre."""
    right, bottom = left + width, top + height
    if not (left <= x <= right and top <= y <= bottom):
        return False
    cx = left + radius if x < left + radius else (
        right - radius if x > right - radius else x)
    cy = top + radius if y < top + radius else (
        bottom - radius if y > bottom - radius else y)
    if cx == x and cy == y:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def render(size):
    """One size of the icon, as RGBA rows."""
    scale = size / CANVAS
    step = 1.0 / SUPERSAMPLE
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            inside_bg = 0
            inside_mark = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    # Sample at the centre of each sub-pixel, in canvas units.
                    x = (px + (sx + 0.5) * step) / scale
                    y = (py + (sy + 0.5) * step) / scale
                    if not rounded_rect_covers(x, y, 0, 0, CANVAS, CANVAS,
                                               BG_RADIUS):
                        continue
                    inside_bg += 1
                    for left, top in CELLS:
                        if rounded_rect_covers(x, y, left, top, CELL, CELL,
                                               CELL_RADIUS):
                            inside_mark += 1
                            break
            total = SUPERSAMPLE * SUPERSAMPLE
            if not inside_bg:
                row += bytes((0, 0, 0, 0))
                continue
            # Blend the mark over the background by how much of the pixel it
            # covers, then let the background coverage drive the alpha so the
            # rounded outer corners are smooth.
            mark_share = inside_mark / inside_bg
            colour = tuple(round(ACCENT[i] + (MARK[i] - ACCENT[i]) * mark_share)
                           for i in range(3))
            row += bytes(colour) + bytes((round(255 * inside_bg / total),))
        rows.append(bytes(row))
    return rows


def png_bytes(size, rows):
    """A minimal RGBA PNG. Filter type 0 on every row, one IDAT, no frills."""
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico_bytes(images):
    """An ICO containing PNG payloads for every size.

    PNG rather than BMP for all of them, deliberately: one code path instead of
    two, no "double the DIB height" trap to get wrong, and a 256x256 BMP entry
    alone would be 256 KB. PNG-in-ICO has been understood since Windows Vista,
    so the compatibility argument for BMP is theoretical now."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, payloads = b"", b""
    for size, data in images:
        entries += struct.pack("<BBBBHHII",
                               0 if size >= 256 else size,   # 0 means 256
                               0 if size >= 256 else size,
                               0, 0, 1, 32, len(data), offset)
        payloads += data
        offset += len(data)
    return header + entries + payloads


def main():
    here = Path(__file__).resolve().parent
    images = []
    for size in SIZES:
        data = png_bytes(size, render(size))
        images.append((size, data))
        print(f"  rendered {size}x{size}  {len(data)} bytes")

    ico = here / "kramer.ico"
    ico.write_bytes(ico_bytes(images))
    print(f"wrote {ico}  ({ico.stat().st_size} bytes, sizes {list(SIZES)})")

    png = here / "kramer.png"
    png.write_bytes(dict(images)[256])
    print(f"wrote {png}  ({png.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
