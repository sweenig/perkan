from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)

ICON_PATH = STATIC_DIR / "favicon.ico"

SIZE = 256
BG = (0, 0, 0, 0)
BORDER_COLOR = (0, 168, 255, 255)  # vivid azure
PANEL_FILL = (244, 250, 255, 255)
HEADER_FILL = (222, 242, 255, 255)
LINE_COLOR = BORDER_COLOR
PADDING = 28
CORNER_RADIUS = 48
LINE_WIDTH = 18

img = Image.new("RGBA", (SIZE, SIZE), BG)
draw = ImageDraw.Draw(img)

outer = (PADDING, PADDING, SIZE - PADDING, SIZE - PADDING)
draw.rounded_rectangle(outer, radius=CORNER_RADIUS, fill=PANEL_FILL, outline=BORDER_COLOR, width=LINE_WIDTH)

header_bottom = PADDING + (SIZE - 2 * PADDING) * 0.28
header_rect = (PADDING + LINE_WIDTH, PADDING + LINE_WIDTH, SIZE - PADDING - LINE_WIDTH, header_bottom)
draw.rounded_rectangle(header_rect, radius=CORNER_RADIUS // 2, fill=HEADER_FILL)

table_left = PADDING + LINE_WIDTH
table_right = SIZE - PADDING - LINE_WIDTH
table_top = PADDING + LINE_WIDTH
table_bottom = SIZE - PADDING - LINE_WIDTH

for offset_multiplier in (1 / 3, 2 / 3):
    x = table_left + (table_right - table_left) * offset_multiplier
    draw.line(((x, table_top), (x, table_bottom)), fill=LINE_COLOR, width=LINE_WIDTH // 2)

body_top = header_bottom + LINE_WIDTH / 2
draw.line(((table_left, body_top), (table_right, body_top)), fill=LINE_COLOR, width=LINE_WIDTH // 2)

img.save(ICON_PATH, sizes=[(64, 64), (48, 48), (32, 32), (16, 16)])
print(f"favicon saved to {ICON_PATH}")
