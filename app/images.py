"""Photo uploads. Every file is decoded and re-encoded as JPEG, which
  * rejects anything that is not really an image, whatever its name says,
  * strips EXIF metadata, including the GPS position of the phone that took it,
  * caps the size sent to the language model.
"""
import io
import uuid

from PIL import Image, ImageOps

from . import config

Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse decompression bombs
MAX_SIDE = 1600


class ImageError(ValueError):
    pass


def process_upload(raw: bytes) -> tuple[bytes, int, int]:
    if len(raw) > config.MAX_IMAGE_BYTES:
        raise ImageError("Each photo must be under 10 MB.")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(raw))
        if img.format not in ("JPEG", "PNG", "WEBP"):
            raise ImageError("Photos must be JPEG, PNG or WebP.")
        img = ImageOps.exif_transpose(img)  # keep the orientation, drop the metadata
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    except ImageError:
        raise
    except Exception as exc:
        raise ImageError("One of the files is not a readable image.") from exc
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)  # no exif= argument, so none is written
    return out.getvalue(), img.width, img.height


def save(data: bytes, conn=None) -> str:
    """Write the photo to UPLOAD_DIR and, given a connection, to the database too.
    UPLOAD_DIR is only a cache: on hosts with an ephemeral disk it is wiped on restart."""
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    (config.UPLOAD_DIR / name).write_bytes(data)
    if conn is not None:
        conn.execute("INSERT INTO image_blobs (file_name, data) VALUES (%s, %s)", [name, data])
    return name


def load(name: str, conn) -> bytes | None:
    """The photo's bytes from the disk cache, else from the database (re-filling the cache)."""
    path = config.UPLOAD_DIR / name
    if path.exists():
        return path.read_bytes()
    row = conn.execute("SELECT data FROM image_blobs WHERE file_name = %s", [name]).fetchone()
    if not row:
        return None
    data = bytes(row["data"])
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data
