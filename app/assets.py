"""Local asset storage for datasheets and images.

Files live under ``settings.asset_dir`` as ``parts/<ipn>/datasheet.pdf`` and
``parts/<ipn>/image.jpg``. The database stores the path relative to that root,
never an absolute path, so the volume can move between mounts and pods.

Writes are atomic: bytes go to ``<name>.tmp`` in the same directory and are
``os.replace``-d into place, so a reader never observes a half-written file.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import settings

_EXTENSION = {"datasheet": "pdf", "image": "jpg"}


def asset_root() -> Path:
    return settings.asset_dir


def rel_path_for(ipn: str, kind: str) -> str:
    """The path (relative to the asset root) for a part's asset of *kind*."""
    ext = _EXTENSION[kind]
    return f"parts/{ipn}/{kind}.{ext}"


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
