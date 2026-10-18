"""Shared text reads. A null byte means the file is binary."""

from pathlib import Path


def read_text_file(path: Path) -> str:
    """Read text. A null byte means the file is binary."""
    data = path.read_bytes()
    if b"\0" in data:
        raise ValueError(f"{path} looks like a binary file")
    return data.decode("utf-8", errors="replace")
