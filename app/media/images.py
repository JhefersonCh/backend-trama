"""Image intake: validate by content (not by the client's mime), drop EXIF/metadata, normalize, re-encode."""

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PX = 2048
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
Image.MAX_IMAGE_PIXELS = 60_000_000  # decompression-bomb guard (Pillow raises above 2x this)


class ImageError(ValueError):
    pass


@dataclass(frozen=True)
class CleanImage:
    data: bytes
    width: int
    height: int
    mime: str = "image/jpeg"


def _encode(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)  # no exif=/icc_profile= -> nothing is carried over
    return buf.getvalue()


def clean_image(raw: bytes) -> CleanImage:
    """Decode, fix orientation from EXIF, flatten alpha, cap at MAX_PX and re-encode as a metadata-free JPEG."""
    try:
        with Image.open(io.BytesIO(raw)) as src:
            if src.format not in ALLOWED_FORMATS:
                raise ImageError("unsupported image format (use JPG, PNG or WebP)")
            im = ImageOps.exif_transpose(src)  # bake the rotation in, then the tag is irrelevant
            im.load()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError, ValueError) as e:
        raise ImageError("the file is not a valid image") from e
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > MAX_PX:
        im.thumbnail((MAX_PX, MAX_PX), Image.Resampling.LANCZOS)
    return CleanImage(_encode(im, 88), im.width, im.height)


def shrink_for_model(jpeg: bytes, max_px: int) -> bytes:
    """Smaller copy for the vision model (fewer image tokens -> faster on CPU)."""
    with Image.open(io.BytesIO(jpeg)) as im:
        if max(im.size) <= max_px:
            return jpeg
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        return _encode(im, 85)
