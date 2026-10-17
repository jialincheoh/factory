"""Brochure PDFs: text comes out clean and capped, usable pictures come out, junk is refused."""
import io

import pymupdf
import pytest
from PIL import Image

from app import brochure, images


def photo_png(w=800, h=600, colour=(90, 120, 200)) -> bytes:
    buf = io.BytesIO(); Image.new("RGB", (w, h), colour).save(buf, "PNG"); return buf.getvalue()


def make_pdf(text: str, pictures: list[bytes] = (), pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), text, fontsize=11)
        for i, pic in enumerate(pictures):
            page.insert_image(pymupdf.Rect(40, 100 + 220 * i, 400, 300 + 220 * i), stream=pic)
    return doc.tobytes()


def test_text_and_embedded_photos_come_out():
    pdf = make_pdf("Kilang dijual di Senai.\n\n\n\n400A   power,  2 ekar. Call 012-345 6789",
                   [photo_png(), photo_png(1000, 700, (200, 50, 50)), photo_png(64, 64)])  # last one is a logo
    text, pics = brochure.extract(pdf)
    assert "Kilang dijual di Senai." in text and "400A power, 2 ekar." in text
    assert "\n\n\n" not in text  # blank runs collapsed
    assert len(pics) == 2  # the 64 px logo is dropped
    for data in pics:  # every picture must pass the same processing as an upload
        jpeg, w, h = images.process_upload(data)
        assert w >= 800 and jpeg[:2] == b"\xff\xd8"
    assert Image.open(io.BytesIO(pics[0])).size[0] >= Image.open(io.BytesIO(pics[1])).size[0]  # largest first


def test_pages_are_rendered_when_there_are_no_photos():
    text, pics = brochure.extract(make_pdf("Scanned brochure text", pages=3), max_images=2)
    assert text.split("\n\n") == ["Scanned brochure text"] * 3
    assert len(pics) == 2
    assert Image.open(io.BytesIO(pics[0])).size[0] > 800


def test_limits_and_junk():
    assert len(brochure.extract(make_pdf("word " * 3000))[0]) <= brochure.MAX_TEXT
    with pytest.raises(brochure.BrochureError, match="not a readable PDF"):
        brochure.extract(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(brochure.BrochureError, match="pages"):
        brochure.extract(make_pdf("x", pages=brochure.MAX_PAGES + 1))
    with pytest.raises(brochure.BrochureError, match="20 MB"):
        brochure.extract(b"0" * (brochure.MAX_BYTES + 1))
