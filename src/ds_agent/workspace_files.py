"""Private configuration writes and workspace file boundaries."""
from __future__ import annotations
import os
from pathlib import Path

from . import core


def write_private(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data.encode() if isinstance(data, str) else data)


def workspace_path(path: Path, workspace: Path) -> Path | None:
    root = workspace.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        for p in (candidate, candidate.resolve()):
            rel = p.relative_to(root)
            if rel.name in core.WORKSPACE_SECRET_FILES:
                return None
            if any(part in core.WORKSPACE_SECRET_DIRS for part in rel.parts[:-1]):
                return None
        return candidate.resolve()
    except (ValueError, OSError, RuntimeError):
        return None


def artifact_path(path: str, workspace: Path | None) -> Path | None:
    if workspace is None:
        return None
    candidate = workspace_path(Path(path), workspace)
    if candidate is not None and candidate.is_file():
        return candidate
    # Preserve basename recovery for nonexistent model-generated paths only.
    original = Path(path) if Path(path).is_absolute() else workspace / path
    if not original.exists() and not original.is_symlink():
        candidate = workspace_path(Path(Path(path).name), workspace)
        if candidate is not None and candidate.is_file():
            return candidate
    return None
