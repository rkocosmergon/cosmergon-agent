"""Install the 3D picture of the first-person view: ``cosmergon --install-view`` (cos20 #853).

The dashboard draws the view through a marauder's eyes in characters on its own. With the
program ``cosmergon-pov`` (GPL-2.0, built from github.com/rkocosmergon/cosmergon-pov) it is a
real picture. The guided installation asks for it; this module is the one way it is fetched,
there and later, when someone said no at first and wants it now.

What it does, and nothing else:
  * fetches the built program for this system from the release named below, and Freedoom (the
    game data, BSD licence), over https only, each with a limit of size and of time for the whole
    download; a redirect is followed only to https, at most three times, and its body is never read;
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
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import IO

from cosmergon_agent import pov_bridge

POV_TAG = "v0.1.0-pre.2"
POV_BASE = f"https://github.com/rkocosmergon/cosmergon-pov/releases/download/{POV_TAG}"
POV_BUILDS = {  # (system, machine) -> (archive name, SHA-256)
    ("Linux", "x86_64"): (
        "cosmergon-pov-linux-x86_64",
        "257505a9ad5d433e2915c1099db0c28e0f288db4f686ba2d65e63dca4c7cf0bd",
    ),
    ("Darwin", "arm64"): (
        "cosmergon-pov-macos-arm64",
        "338e657ece568288435ef0de2ec8ae9f43147c4f448e3fdc675af178567b0234",
    ),
    ("Darwin", "x86_64"): (
        "cosmergon-pov-macos-x86_64",
        "07298c6bbb0fe0885b3e8512c5caa1cfb717d49e7fa791d5bcf84d7416aea60b",
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
SECONDS = 30.0  # the socket's limit for connecting and for each read
TOTAL_SECONDS = 600.0  # the whole download, redirects included; over by at most one read (SECONDS)
REDIRECTS = 3  # GitHub sends one, to its file host
_CHUNK = 64 * 1024

HINT = "3D picture: cosmergon --install-view"

Opener = Callable[[str], IO[bytes]]


class InstallError(Exception):
    """Said to the user in one sentence; nothing half installed stays behind."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follows no redirect itself: urllib would read the redirect's body first, without a limit.

    Returning None makes urllib raise the redirect as an HTTPError, whose body nobody reads;
    ``_open`` follows the address in its header.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _once(url: str) -> IO[bytes]:
    """One request to ``url``, no redirect followed. Replaced in tests."""
    request = urllib.request.Request(url)  # noqa: S310 - only https reaches here (_open)
    return _OPENER.open(request, timeout=SECONDS)  # type: ignore[no-any-return]


def _open(url: str) -> IO[bytes]:
    """The response to ``url``: https only, redirects to https only, at most ``REDIRECTS``.

    The body of a redirect or of an error is never read. Replaced in tests.
    """
    for _ in range(REDIRECTS + 1):
        if not url.startswith("https://"):
            raise InstallError(f"not an https address: {url[:80]}")
        try:
            return _once(url)
        except urllib.error.HTTPError as exc:
            moved = exc.code in (301, 302, 303, 307, 308)
            location = exc.headers.get("Location") if moved else None
            exc.close()
            if not location:
                raise InstallError(f"{url[:80]} answered {exc.code}") from None
            url = urllib.parse.urljoin(url, location)
    raise InstallError(f"more than {REDIRECTS} redirects")


def fetch(url: str, sha256: str, most: int, opener: Opener | None = None) -> bytes:
    """Return the bytes at ``url``.

    Raises:
        InstallError: the download failed, is larger than ``most`` or does not match ``sha256``.
    """
    digest, parts, size = hashlib.sha256(), [], 0
    until = time.monotonic() + TOTAL_SECONDS
    try:
        with (opener or _open)(url) as answer:
            while chunk := answer.read(_CHUNK):
                if time.monotonic() > until:
                    limit = f"{TOTAL_SECONDS:.0f} s"
                    raise InstallError(f"the file from {url} took longer than {limit}")
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
