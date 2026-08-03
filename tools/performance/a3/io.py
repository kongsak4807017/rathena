"""Deterministic JSON and checksum helpers for A3 artifacts."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def read_json(path: Path) -> Any:
    """Read a UTF-8 JSON document from ``path``."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: Path, value: Any) -> None:
    """Write ``value`` as deterministic UTF-8 JSON, atomically.

    The JSON is serialized to a sibling temporary file, flushed and
    fsynced, then moved into place with :func:`os.replace`. Temporary
    files are removed if any step fails.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path``, streamed in 1 MiB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
