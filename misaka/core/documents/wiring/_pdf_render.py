"""Short-lived PDFium renderer. Invoke by file path to keep child startup small."""
import base64
import json
import sys
from io import BytesIO

# Bound allocation before rendering; the inline image limit is also 2000x2000.
_MAX_RENDER_PX = 2000
_MAX_SCALE = 10.0


def _render_page(pdf_path, page, scale):
    """Render one 1-based page of a PDF to PNG bytes: ``(png, page count)``.

    Runs only in the short-lived rendering child, never in the agent process.
    """
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        count = len(pdf)
        if not 1 <= page <= count:
            return None, count
        pg = pdf[page - 1]
        try:
            width, height = pg.get_size()                 # points; pixels = points * scale
            scale = min(scale, _MAX_SCALE, _MAX_RENDER_PX / max(width, height, 1))
            bitmap = pg.render(scale=max(scale, 1 / _MAX_RENDER_PX))
            try:
                # to_pil() shares the bitmap's buffer, so the PNG has to be written before it goes.
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
    png, count = _render_page(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]))
    print(json.dumps({"pages": count, "png": base64.b64encode(png).decode("ascii") if png else None}))
