"""Generate placeholder golden images for Luanti visual oracles.

The Luanti visual oracle requires a rendered client frame captured under xvfb +
software GL (see warptest.luanti_runtime). That capture cannot run on this
(macOS / no-X11) host, so these committed goldens are deterministic placeholders
that satisfy spec schema validation and the visual-judge prompt wiring. Regenerate
them from a real `warptest run ... --headed`/bench capture on a Linux+xvfb host
before relying on visual-judge verdicts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

NAMES = [
    "set-node", "dig-node", "fill-floor", "replace-node", "build-column",
	"water-source", "stone-platform", "tree-grove", "chest-inventory",
	"chest-multi-item", "player-teleport", "player-hp", "player-inventory",
	"player-pos-and-hp", "player-give-stack", "save-restore", "save-restore-chest",
	"complex-house-workshop", "irrigated-farmstead", "storage-workshop",
	"mine-bridge-outpost", "homestead-save-restore",
]

OUT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1280, 720


def palette(name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    digest = hashlib.sha256(name.encode()).digest()
    sky = (110 + digest[0] % 60, 150 + digest[1] % 60, 200 + digest[2] % 40)
    block = (90 + digest[3] % 80, 80 + digest[4] % 80, 70 + digest[5] % 60)
    return sky, block


def main() -> None:
    for name in NAMES:
        sky, block = palette(name)
        image = Image.new("RGB", (WIDTH, HEIGHT), sky)
        draw = ImageDraw.Draw(image)
        # Ground band.
        draw.rectangle([0, HEIGHT * 2 // 3, WIDTH, HEIGHT], fill=(70, 110, 60))
        # A few voxel-ish blocks so the frame is non-blank/informative.
        for i in range(6):
            x = 200 + i * 150
            y = HEIGHT * 2 // 3 - 80 - (i % 3) * 60
            shade = (block[0], block[1], min(255, block[2] + i * 8))
            draw.rectangle([x, y, x + 90, y + 90], fill=shade, outline=(30, 30, 30), width=3)
        draw.text((40, 40), f"WARPTEST PLACEHOLDER\nluanti / {name}", fill=(255, 255, 255))
        draw.text((40, HEIGHT - 60), "Regenerate from xvfb client capture", fill=(240, 240, 240))
        path = OUT / f"{name}.golden.png"
        image.save(path, "PNG")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
