"""Read a bounded regular file and verify its path and handle across the read."""

import os
import stat
from pathlib import Path


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, "st_file_attributes", 0))


def _opened_signature(info: os.stat_result) -> tuple[int, ...]:
    # Windows path stat adds execute bits from .exe/.com/.bat/.cmd filenames;
    # fstat has no filename. Normalize only this cross-path/handle comparison.
    # Same-path and same-handle before/after comparisons retain the full mode.
    signature = _signature(info)
    if os.name == "nt":
        return (*signature[:2], signature[2] & ~0o111, *signature[3:])
    return signature


def _real(info: os.stat_result, *, directory: bool) -> None:
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("only real directories and regular files are allowed; no link/reparse")


def _ancestors(directory: Path) -> None:
    for path in reversed((directory, *directory.parents)):
        _real(path.lstat(), directory=True)


def _read(path: Path, maximum: int) -> bytes:
    """Measure the opened bytes, checking both the handle and path before/after."""
    _ancestors(path.parent)
    before = path.lstat()
    _real(before, directory=False)
    if before.st_size > maximum:
        raise ValueError("file exceeds byte limit")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _real(opened, directory=False)
        if _opened_signature(before) != _opened_signature(opened):
            raise ValueError("file changed before read")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    _ancestors(path.parent)
    if (len(raw) > maximum or len(raw) != before.st_size
            or _signature(opened) != _signature(after)
            or _signature(before) != _signature(path.lstat())):
        raise ValueError("file changed during read or exceeds byte limit")
    return raw
