#!/usr/bin/env python3
"""Draw the test image the image tests read: a red circle on white, 200x200.

Translated from pi's ``packages/ai/scripts/generate-test-image.ts`` (v1.1.0). MISAKA fork: Pillow draws it where pi uses
node-canvas, so the edge pixels and the PNG bytes differ from pi's ``test/data/red-circle.png``; the picture is the same.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

__dirname = Path(__file__).resolve().parent


def main() -> int:
    # Create a 200x200 canvas
    # Fill background with white
    canvas = Image.new("RGB", (200, 200), "white")
    ctx = ImageDraw.Draw(canvas)

    # Draw a red circle in the center
    ctx.ellipse((100 - 50, 100 - 50, 100 + 50, 100 + 50), fill="red")

    # Save the image
    outputPath = __dirname / ".." / "tests" / "data" / "red-circle.png"

    # Ensure the directory exists
    os.makedirs(__dirname / ".." / "tests" / "data", exist_ok=True)

    canvas.save(outputPath, format="PNG")
    print(f"Generated test image at: {os.path.normpath(outputPath)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
