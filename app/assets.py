"""Local asset storage for datasheets and images.

Files live under ``settings.asset_dir`` as ``parts/<ipn>/datasheet.<ext>`` and
``parts/<ipn>/image.<ext>``. The database stores the path relative to that root,
never an absolute path, so the volume can move between mounts and pods.

Writes are atomic: bytes go to ``<name>.tmp`` in the same directory and are
``os.replace``-d into place, so a reader never observes a half-written file.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import settings

_EXTENSION = {"datasheet": "pdf", "image": "jpg"}

DATASHEET_MAX_BYTES = 25 * 1024 * 1024
IMAGE_MAX_BYTES = 10 * 1024 * 1024

_PDF_SIGNATURE = b"%PDF-"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def asset_root() -> Path:
    return settings.asset_dir


def rel_path_for(ipn: str, kind: str, ext: str | None = None) -> str:
    """The path (relative to the asset root) for a part's asset of *kind*.

    *ext* is the real file extension (``pdf``, ``jpg``, ``png`` or ``webp``);
    when omitted the per-kind default is used.
    """
    if ext is None:
        ext = _EXTENSION[kind]
    return f"parts/{ipn}/{kind}.{ext}"


def content_type_for(kind: str, head: bytes) -> tuple[str, str] | None:
    """Return ``(content_type, extension)`` for *kind* when *head* matches.

    *head* is the leading bytes of an upload; the browser's ``Content-Type``
    and the filename are advisory, so only the magic bytes decide. Returns
    ``None`` when *kind* is unknown or the bytes do not match what it expects.
    """
    if kind == "datasheet":
        if head.startswith(_PDF_SIGNATURE):
            return ("application/pdf", "pdf")
        return None
    if kind == "image":
        if head.startswith(_JPEG_SIGNATURE):
            return ("image/jpeg", "jpg")
        if head.startswith(_PNG_SIGNATURE):
            return ("image/png", "png")
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ("image/webp", "webp")
        return None
    return None


def absolute(rel_path: str) -> Path:
    return asset_root() / rel_path


def tmp_path(rel_path: str) -> Path:
    target = absolute(rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.parent / (target.name + ".tmp")


def commit(tmp: Path, rel_path: str) -> None:
    """Atomically move a fully written temp file into place."""
    os.replace(tmp, absolute(rel_path))


def resolve(rel_path: str) -> Path | None:
    """Resolve a stored relative path under the root, or return None.

    Rejects absolute paths, ``..`` escapes and symlinks that leave the root.
    ``Path.resolve()`` follows symlinks, so a symlink pointing outside the root
    resolves outside it and is refused here.
    """
    if not rel_path or "\x00" in rel_path:
        return None
    path = Path(rel_path)
    if path.is_absolute():
        return None
    root = asset_root().resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate
