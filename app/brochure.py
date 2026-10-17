"""Brochure PDFs. A real estate agent can upload the developer's or owner's
brochure instead of (or as well as) typing a description and choosing photos.

The text becomes part of the description, so it goes through the same contact
masking as typed text before reaching the model. Embedded photos become listing
photos and go through images.process_upload like any upload (re-encoded, no
metadata). A brochure without usable embedded images is rendered page by page,
so a scanned brochure still gives the model something to look at.
"""
import io

import pymupdf

from . import config

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 30
MAX_TEXT = 5000       # characters kept from the brochure
MIN_IMAGE_SIDE = 300  # smaller embedded images are logos and icons
RENDER_DPI = 110      # about 1200 px across an A4 page


class BrochureError(ValueError):
    pass


def extract(raw: bytes, max_images: int = config.MAX_IMAGES) -> tuple[str, list[bytes]]:
    """Return (text, images) from a PDF; images are PNG/JPEG bytes, largest first, at most max_images."""
    if len(raw) > MAX_BYTES:
        raise BrochureError("The brochure must be under 20 MB.")
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise BrochureError("The brochure is not a readable PDF.") from exc
    with doc:
        if doc.is_encrypted and not doc.authenticate(""):
            raise BrochureError("The brochure is password-protected.")
        if doc.page_count > MAX_PAGES:
            raise BrochureError(f"The brochure has more than {MAX_PAGES} pages.")
        pages = list(doc)
        text = _clean("\n".join(page.get_text("text") for page in pages))
        images = _embedded_images(doc, pages, max_images) or _rendered_pages(pages, max_images)
    return text[:MAX_TEXT], images


def _clean(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:  # collapse runs of blank lines
        if line:
            out.append(line); blank = False
        elif not blank:
            out.append(""); blank = True
    return "\n".join(out).strip()


def _embedded_images(doc, pages, limit: int) -> list[bytes]:
    found, seen = [], set()
    for page in pages:
        for info in page.get_images(full=True):
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                pix = pymupdf.Pixmap(doc, xref)
                if pix.width < MIN_IMAGE_SIDE or pix.height < MIN_IMAGE_SIDE:
                    continue
                if pix.n - pix.alpha >= 4 or pix.alpha:  # CMYK or transparency: normalise to RGB
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                found.append((pix.width * pix.height, pix.tobytes("png")))
            except Exception:
                continue  # one odd image must not sink the whole brochure
    found.sort(key=lambda t: -t[0])
    return [data for _, data in found[:limit]]


def _rendered_pages(pages, limit: int) -> list[bytes]:
    out = []
    for page in pages[:limit]:
        pix = page.get_pixmap(dpi=RENDER_DPI, colorspace=pymupdf.csRGB, alpha=False)
        out.append(pix.tobytes("jpeg"))
    return out
