"""Where the empty area of the first-person view lies, for a native renderer (cos20 #799).

The terminal dashboard keeps an area empty and writes its place into a small file; a separate
program draws the picture there in its own window. Stdlib only, nothing here sends an escape
sequence to the terminal, and nothing here can raise into the dashboard.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

VERSION = 1
_DIR = "cosmergon"
_FILE = "pov-place.json"

# What was written last: the path and the text. The widget may call often; equal text is skipped.
_last: tuple[Path, str] | None = None


def _uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def terminal_fd() -> int | None:
    """The terminal the dashboard draws on: stderr, else stdout, else stdin, whichever is a tty.

    Textual's Linux driver writes to ``sys.__stderr__`` (textual/drivers/linux_driver.py,
    ``self._file``), so that is asked first; stdout may be a pipe.
    """
    for stream in (sys.__stderr__, sys.__stdout__, sys.__stdin__):
        try:
            if stream is not None and stream.isatty():
                return stream.fileno()
        except (OSError, ValueError):
            continue
    return None


def cell_pixels(fd: int | None) -> tuple[int, int, int, int] | None:
    """``(cols, rows, text_w_px, text_h_px)`` of the terminal behind ``fd``, or None.

    None when the ioctl fails, the platform has no ``fcntl``/``termios``, or a number is 0
    (many terminals leave the pixel fields 0).
    """
    if fd is None:
        return None
    try:
        import fcntl
        import struct
        import termios
    except ImportError:
        return None
    try:
        raw = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, width, height = struct.unpack("HHHH", raw)
    except (OSError, ValueError, TypeError, struct.error):
        return None
    if 0 in (cols, rows, width, height):
        return None
    return cols, rows, width, height


def place_path() -> Path | None:
    """``$XDG_RUNTIME_DIR/cosmergon/pov-place.json``; None unless that directory is ours.

    No fallback location: without a private runtime directory there is no file.
    """
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    uid = _uid()
    if not base or not os.path.isabs(base) or uid is None:
        return None
    try:
        info = os.stat(base)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        return None
    return Path(base) / _DIR / _FILE


def build_place(
    visible: bool,
    region: tuple[int, int, int, int],
    terminal: tuple[int, int, int, int],
) -> dict[str, Any]:
    """The file's content from numbers: ``region`` = col, row, cols, rows of the empty area in
    cells; ``terminal`` = cols, rows, text width and height in pixels."""
    col, row, cols, rows = region
    term_cols, term_rows, text_w, text_h = terminal
    return {
        "v": VERSION,
        "pid": os.getpid(),
        "visible": bool(visible),
        "col": col,
        "row": row,
        "cols": cols,
        "rows": rows,
        "cell_w": text_w // term_cols,
        "cell_h": text_h // term_rows,
        "term_cols": term_cols,
        "term_rows": term_rows,
        "text_w": text_w,
        "text_h": text_h,
    }


def _own_dir(path: Path) -> bool:
    """The directory exists (made 0700 if missing), is no symlink and belongs to us."""
    try:
        os.mkdir(path.parent, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path.parent)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == _uid()


def _replace_atomic(path: Path, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    try:  # a leftover of a process that had our number: the directory is ours alone
        os.unlink(tmp)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_place(place: Any) -> bool:
    """Write ``place`` (a dict or a dataclass) atomically; False on any refusal or OSError."""
    global _last
    path = place_path()
    if path is None:
        return False
    is_instance = dataclasses.is_dataclass(place) and not isinstance(place, type)
    data = dataclasses.asdict(place) if is_instance else dict(place)
    text = json.dumps(data, separators=(",", ":")) + "\n"
    if _last == (path, text) and os.path.lexists(path):
        return True
    try:
        if not _own_dir(path):
            return False
        if os.path.lexists(path) and stat.S_ISLNK(os.lstat(path).st_mode):
            return False
        _replace_atomic(path, text)
    except OSError:
        return False
    _last = (path, text)
    return True


def remove_place() -> None:
    """Remove the file (never the directory); nothing happens when there is none."""
    global _last
    _last = None
    path = place_path()
    if path is None:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
