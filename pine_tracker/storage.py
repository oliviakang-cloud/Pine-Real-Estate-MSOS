"""Where uploaded photos live.

Railway rebuilds the container on every deploy, so anything written to the
normal filesystem disappears. Files go to MEDIA_DIR instead, which in
production is a mounted Railway volume that survives deploys.

Phone photos arrive at 3-6 MB each. They are downscaled on the way in, which
keeps the volume small and makes pages load over a weak signal in a garage.
The original is not kept: for receipts and repair photos the resized version
is the record, and 2000px is far more detail than either use needs.

If this ever outgrows a volume, the only thing to change is save_upload and
open_file: keys are opaque strings, so moving to S3 or Cloudflare R2 does not
touch the models or the views.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path
from typing import BinaryIO, Optional

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".pdf"}
MAX_BYTES = 12 * 1024 * 1024  # generous for a phone photo, small enough to reject video
MAX_PIXELS = 2000             # longest edge after downscaling


class UploadError(ValueError):
    """Raised for anything the user can fix by picking a different file."""


def media_root() -> Path:
    root = Path(os.environ.get("MEDIA_DIR", "media"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise UploadError(
            "That file type isn't supported. Use a photo (JPG, PNG, HEIC, WEBP) or a PDF."
        )
    return ".jpg" if suffix in {".jpeg", ".heic"} else suffix


def new_key(prefix: str, filename: str) -> str:
    """Build an opaque storage key: prefix/YYYY/MM/random.ext.

    Dated folders keep any one directory small and make manual cleanup
    possible. The random component means two photos taken in the same second
    never collide, and a key can't be guessed from another one.
    """
    today = dt.date.today()
    return f"{prefix}/{today.year}/{today.month:02d}/{secrets.token_urlsafe(12)}{_safe_suffix(filename)}"


def resolve(key: str) -> Path:
    """Turn a stored key into a path, refusing anything that escapes the root."""
    root = media_root().resolve()
    target = (root / key).resolve()
    if not target.is_relative_to(root):
        raise UploadError("Invalid file reference.")
    return target


def save_upload(stream: BinaryIO, filename: str, prefix: str) -> tuple[str, int]:
    """Store an uploaded file and return (key, bytes_written).

    Raises UploadError for an empty file, an oversized one, or a type we
    don't accept.
    """
    key = new_key(prefix, filename)
    target = resolve(key)
    target.parent.mkdir(parents=True, exist_ok=True)

    data = stream.read(MAX_BYTES + 1)
    if not data:
        raise UploadError("That file came through empty. Try taking the photo again.")
    if len(data) > MAX_BYTES:
        raise UploadError("That file is larger than 12 MB. A photo, not a video, please.")

    if target.suffix != ".pdf":
        data = _downscale(data, target.suffix)

    target.write_bytes(data)
    return key, len(data)


def _downscale(data: bytes, suffix: str) -> bytes:
    """Shrink an image to MAX_PIXELS on its longest edge.

    Pillow is optional on purpose: if it isn't installed the upload still
    works, it just stores the original bytes. A missing optional dependency
    shouldn't stop someone logging a repair.
    """
    try:
        import io

        from PIL import Image, ImageOps
    except ImportError:
        return data

    try:
        with Image.open(io.BytesIO(data)) as image:
            # Phone photos carry rotation in EXIF; bake it in or everything
            # shot in portrait displays sideways.
            image = ImageOps.exif_transpose(image)
            if max(image.size) > MAX_PIXELS:
                image.thumbnail((MAX_PIXELS, MAX_PIXELS), Image.LANCZOS)
            buffer = io.BytesIO()
            if suffix == ".png":
                image.save(buffer, format="PNG", optimize=True)
            else:
                image.convert("RGB").save(
                    buffer, format="JPEG", quality=85, optimize=True
                )
            return buffer.getvalue()
    except Exception:
        # An unreadable or exotic image still gets stored as-is rather than
        # losing the user's upload.
        return data


def delete_file(key: Optional[str]) -> None:
    if not key:
        return
    try:
        resolve(key).unlink(missing_ok=True)
    except (UploadError, OSError):
        pass
