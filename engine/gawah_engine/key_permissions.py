"""Platform-aware permissions for directories and files containing private keys."""

import os
import re
import stat
import subprocess
from pathlib import Path


def _windows_identity() -> str:
    """Return the current Windows account in a form accepted by ``icacls``."""
    result = subprocess.run(
        ["whoami"], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    identity = result.stdout.strip()
    if not identity:
        raise RuntimeError("could not determine the current Windows account for key ACLs")
    return identity


def _set_windows_acl(path: Path, grant: str) -> None:
    """Remove inherited access and grant the current account exclusive control."""
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"could not secure private-key path {path}") from exc


def secure_private_key_directory(path: Path) -> None:
    """Restrict a directory containing private keys to its current owner."""
    if os.name == "nt":
        _set_windows_acl(path, f"{_windows_identity()}:(OI)(CI)(F)")
    else:
        os.chmod(path, 0o700)


def secure_private_key_file(path: Path) -> None:
    """Restrict a private-key file to its current owner."""
    if os.name == "nt":
        _set_windows_acl(path, f"{_windows_identity()}:(F)")
    else:
        os.chmod(path, 0o600)


def private_key_path_is_secure(path: Path, *, directory: bool = False) -> bool:
    """Return whether a key path has the platform's expected restrictive protection.

    This intentionally checks mode bits only on POSIX. Windows mode bits do not describe
    NTFS DACLs, so it verifies the single explicit ACE installed above instead.
    """
    if os.name != "nt":
        expected = 0o700 if directory else 0o600
        return stat.S_IMODE(path.stat().st_mode) == expected

    try:
        result = subprocess.run(
            ["icacls", str(path)], check=True, capture_output=True, text=True, encoding="utf-8"
        )
        identity = _windows_identity().casefold()
    except (OSError, subprocess.CalledProcessError):
        return False

    path_text = str(path).casefold()
    entries: list[str] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.casefold().startswith(path_text):
            text = text[len(str(path)) :].strip()
        match = re.match(r"(?P<account>.+?):(?:\([^)]*\))+", text)
        if match:
            entries.append(match.group("account").casefold())
    # Windows can add these privileged principals when a directory is used for
    # atomic replacements. They are OS-administration identities, not ordinary
    # users, and do not make a private-key directory broadly readable.
    permitted = {
        identity,
        "nt authority\\system",
        "builtin\\administrators",
        "owner rights",
    }
    return identity in entries and bool(entries) and set(entries).issubset(permitted)
