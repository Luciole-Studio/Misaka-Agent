"""Short-lived PDFium renderer. Invoke by file path to keep child startup small."""
import base64
import json
import sys
from io import BytesIO

_MAX_RENDER_PX = 2000
_MAX_SCALE = 10.0


def _render_page(pdf_path, page, scale, box=None):
    """Render one 1-based page of a PDF to PNG bytes: ``(png, page count)``.

    ``box`` -- ``[x0, y0, x1, y1]`` in points from the crop box's lower-left corner -- renders that
    part of the page alone, at the scale that fits it, rather than the whole page. Runs only in
    the short-lived rendering child, never in the agent process.
    """
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        count = len(pdf)
        if not 1 <= page <= count:
            return None, count
        pg = pdf[page - 1]
        try:
            width, height = pg.get_size()
            crop = (0, 0, 0, 0)
            if box:
                x0, y0, x1, y1 = (max(0.0, box[0]), max(0.0, box[1]), min(width, box[2]), min(height, box[3]))
                crop = (x0, y0, width - x1, height - y1)
                width, height = x1 - x0, y1 - y0
            scale = min(scale, _MAX_SCALE, _MAX_RENDER_PX / max(width, height, 1))
            bitmap = pg.render(scale=max(scale, 1 / _MAX_RENDER_PX), crop=crop)
            try:
                image = bitmap.to_pil()
                try:
                    buffer = BytesIO()
                    image.save(buffer, format="PNG")
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            pg.close()
        return buffer.getvalue(), count
    finally:
        pdf.close()


if __name__ == "__main__":
    png, count = _render_page(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]),
                              json.loads(sys.argv[4]) if len(sys.argv) > 4 else None)
    print(json.dumps({"pages": count, "png": base64.b64encode(png).decode("ascii") if png else None}))
