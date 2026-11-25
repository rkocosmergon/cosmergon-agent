"""Install the 3D picture of the first-person view: ``cosmergon --install-view`` (cos20 #853).

The dashboard draws the view through a marauder's eyes in characters on its own. With the
program ``cosmergon-pov`` (GPL-2.0, built from github.com/rkocosmergon/cosmergon-pov) it is a
real picture. The guided installation asks for it; this module is the one way it is fetched,
there and later, when someone said no at first and wants it now.

What it does, and nothing else:
  * fetches the built program for this system from the release named below, and Freedoom (the
    game data, BSD licence), over https only, each with a limit of time and size;
  * compares each download with the SHA-256 written in this file, before anything is unpacked;
  * takes from the archives only the files named below, as plain files (no path from an
    archive is ever used, no link is followed);
  * puts them into ``$XDG_DATA_HOME/cosmergon-pov`` (else ``~/.local/share/cosmergon-pov``), the
    folder the dashboard starts the program from (``pov_bridge.program_path``): the program
    0755, the rest 0644, the folder closed to writes by others. No sudo, nothing outside your
    home directory.

Standard library only.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import IO

from cosmergon_agent import pov_bridge

POV_TAG = "v0.1.0-pre.1"
POV_BASE = f"https://github.com/rkocosmergon/cosmergon-pov/releases/download/{POV_TAG}"
POV_BUILDS = {  # (system, machine) -> (archive name, SHA-256)
    ("Linux", "x86_64"): (
        "cosmergon-pov-linux-x86_64",
        "cf6e29d030141ad95e9c9d33bb800cdca102324da8f6ab7c080a557a03c03f57",
    ),
    ("Darwin", "arm64"): (
        "cosmergon-pov-macos-arm64",
        "999e3f04db2b5a0d8dcf01732ca0b736972bece0f126ff30ecf5fb9240f3c4ab",
    ),
    ("Darwin", "x86_64"): (
        "cosmergon-pov-macos-x86_64",
        "aafa1c6687d9aa0f089b012b1264d2e7accdd8a2b12f3e3569fdccf0afd592eb",
    ),
}
POV_FILES = {  # file in the archive's folder -> mode
    "cosmergon-pov": 0o755,
    "capabilities": 0o644,
    "cosmergon.wad": 0o644,
    "LICENSE": 0o644,
    "LICENSE-SDL2.txt": 0o644,
    "SOURCE.txt": 0o644,
}
FREEDOOM_VERSION = "0.13.0"
FREEDOOM_URL = (
    "https://github.com/freedoom/freedoom/releases/download/"
    f"v{FREEDOOM_VERSION}/freedoom-{FREEDOOM_VERSION}.zip"
)
FREEDOOM_SHA256 = "3f9b264f3e3ce503b4fb7f6bdcb1f419d93c7b546f4df3e874dd878db9688f59"
FREEDOOM_FILES = {  # member of the zip -> file in the folder
    f"freedoom-{FREEDOOM_VERSION}/freedoom1.wad": "freedoom1.wad",
    f"freedoom-{FREEDOOM_VERSION}/COPYING.txt": "COPYING-freedoom.txt",
}
POV_MOST = 16 * 1024 * 1024  # the archives are 1 to 4 MB
FREEDOOM_MOST = 48 * 1024 * 1024  # the zip is 24 MB
SECONDS = 120.0  # the socket's limit for connecting and for each read
_CHUNK = 64 * 1024

HINT = "3D picture: cosmergon --install-view"

Opener = Callable[[str], IO[bytes]]


class InstallError(Exception):
    """Said to the user in one sentence; nothing half installed stays behind."""


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect (GitHub sends one to its file host) only to another https address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not str(newurl).startswith("https://"):
            raise InstallError(f"a download was sent to a non-https address: {str(newurl)[:80]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str) -> IO[bytes]:
    """The response to ``url`` (https only). Replaced in tests."""
    if not url.startswith("https://"):
        raise InstallError(f"not an https address: {url[:80]}")
    opener = urllib.request.build_opener(_HttpsOnly())
    request = urllib.request.Request(url)  # noqa: S310 - https checked above, redirects too
    return opener.open(request, timeout=SECONDS)  # type: ignore[no-any-return]


def fetch(url: str, sha256: str, most: int, opener: Opener | None = None) -> bytes:
    """Return the bytes at ``url``.

    Raises:
        InstallError: the download failed, is larger than ``most`` or does not match ``sha256``.
    """
    digest, parts, size = hashlib.sha256(), [], 0
    try:
        with (opener or _open)(url) as answer:
            while chunk := answer.read(_CHUNK):
                size += len(chunk)
                if size > most:
                    raise InstallError(f"the file from {url} is larger than {most} bytes")
                digest.update(chunk)
                parts.append(chunk)
    except InstallError:
        raise
    except Exception as exc:  # network, TLS, HTTP status: one sentence
        raise InstallError(f"could not fetch {url} ({type(exc).__name__})") from exc
    if digest.hexdigest() != sha256:
        raise InstallError(f"the file from {url} does not match its checksum")
    return b"".join(parts)


def _from_tar(data: bytes, folder: str) -> dict[str, bytes]:
    """The named files of the archive's folder, each a plain file; anything else raises."""
    found: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name in POV_FILES:
            try:
                member = archive.getmember(f"{folder}/{name}")
            except KeyError:
                raise InstallError(f"the archive lacks {name}") from None
            if not member.isreg():
                raise InstallError(f"{name} in the archive is not a plain file")
            stream = archive.extractfile(member)
            if stream is None:
                raise InstallError(f"{name} in the archive cannot be read")
            found[name] = stream.read()
    return found


def _from_zip(data: bytes) -> dict[str, bytes]:
    """The named members of the game data, each a plain file; anything else raises."""
    found: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member, name in FREEDOOM_FILES.items():
            try:
                info = archive.getinfo(member)
            except KeyError:
                raise InstallError(f"the game data lacks {member}") from None
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:  # a link
                raise InstallError(f"{member} in the game data is not a plain file")
            found[name] = archive.read(info)
    return found


def target_folder() -> Path:
    """Return ``$XDG_DATA_HOME/cosmergon-pov``, else ``~/.local/share/cosmergon-pov``."""
    base = os.environ.get("XDG_DATA_HOME", "")
    if not base or not os.path.isabs(base):  # the XDG rule, as pov_bridge reads it
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "cosmergon-pov"


def _place(folder: Path, files: dict[str, tuple[bytes, int]]) -> None:
    """Write every file next to its place first, then move each in (``os.replace``)."""
    folder.mkdir(mode=0o755, parents=True, exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise InstallError(f"{folder} is not a folder")
    # Closed to writes by group and others, as the dashboard demands; a private folder stays so.
    os.chmod(folder, stat.S_IMODE(folder.stat().st_mode) & ~0o022)
    staged: list[Path] = []
    try:
        for name, (data, mode) in files.items():
            fd, raw = tempfile.mkstemp(dir=folder, prefix=f".{name}.")
            staged.append(Path(raw))
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.chmod(raw, mode)
        for name, path in zip(files, staged, strict=True):
            os.replace(path, folder / name)
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def build_for_this_system() -> tuple[str, str] | None:
    """Return (archive name, SHA-256) of the built program for this system, or None."""
    return POV_BUILDS.get((platform.system(), platform.machine()))


def install(opener: Opener | None = None) -> Path:
    """Fetch, check and place the program and the game data; return their folder.

    Raises:
        InstallError: no build for this system, or a download or an archive is not as written.
    """
    build = build_for_this_system()
    if build is None:
        raise InstallError(
            f"there is no built program for {platform.system()} {platform.machine()} yet"
        )
    name, sha256 = build
    program = _from_tar(fetch(f"{POV_BASE}/{name}.tar.gz", sha256, POV_MOST, opener), name)
    game = _from_zip(fetch(FREEDOOM_URL, FREEDOOM_SHA256, FREEDOOM_MOST, opener))
    files = {n: (data, POV_FILES[n]) for n, data in program.items()}
    files.update({n: (data, 0o644) for n, data in game.items()})
    folder = target_folder()
    _place(folder, files)
    return folder


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    """Install the 3D picture; ``--if-missing`` does nothing when a program is found already."""
    args = sys.argv[1:] if argv is None else argv
    if "--if-missing" in args and pov_bridge.program_path() is not None:
        _say("The 3D picture is already installed.")
        return 0
    _say(f"Fetching the 3D picture ({POV_TAG}) and its game data (Freedoom {FREEDOOM_VERSION}).")
    try:
        folder = install()
    except InstallError as exc:
        _say(f"Not installed: {exc}. The view stays in characters.")
        return 1
    _say(f"Installed in {folder}. Press y in the dashboard to see it.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
