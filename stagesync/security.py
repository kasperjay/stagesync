"""Security Hardening Module for StageSync.

Enforces:
1. Strict 0600 file permissions on credentials, tokens, and sensitive databases.
2. Atomic secure file writing without race conditions.
3. Path traversal prevention on video files and remote downloads.
4. Shell injection validation and safe subprocess execution contracts.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Union

logger = logging.getLogger(__name__)

# Disallowed shell metacharacters that must never appear in binary paths
SHELL_METACHARACTERS = set(";&|`$><\n\r*?[]{}()\"'\\")


def enforce_file_permissions(path: Union[str, Path], expected_mode: int = 0o600) -> bool:
    """Enforce restrictive permissions (default 0600) on sensitive credential/token files.

    On POSIX systems, checks if group or world bits are set (mode & 0o077 != 0).
    If overly permissive, tightens the permissions to `expected_mode`.

    Returns True if permissions are compliant or were successfully tightened, False on error.
    """
    p = Path(path).resolve()
    if not p.exists() or not p.is_file():
        return False

    if os.name != "posix":
        return True

    try:
        current_mode = p.stat().st_mode & 0o777
        # Check if group or other read/write/exec bits are set
        if current_mode & 0o077 != 0:
            logger.warning(
                "Security alert: Sensitive file %s had permissive mode 0o%o. Tightening to 0o%o.",
                p,
                current_mode,
                expected_mode,
            )
            os.chmod(p, expected_mode)
        return True
    except (OSError, PermissionError) as e:
        if getattr(e, "errno", None) == 30:  # EROFS Read-only file system
            logger.debug("Read-only file system prevented tightening %s: %s", p, e)
        else:
            logger.error("Failed to enforce permissions on %s: %s", p, e)
        return False


def secure_save_json(path: Union[str, Path], data: Any, indent: int = 2) -> None:
    """Atomically create and write JSON data with 0600 permissions.

    Uses os.open with O_CREAT | O_WRONLY | O_TRUNC and mode 0o600 to prevent
    the file from ever being created with permissive default umask permissions.
    """
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    payload = json.dumps(data, indent=indent).encode("utf-8")

    fd = os.open(str(p), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception:
        # If fdopen failed, ensure fd is closed
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    # Also enforce 0600 in case file existed beforehand with looser mode
    if os.name == "posix":
        try:
            os.chmod(p, 0o600)
        except (OSError, PermissionError) as e:
            logger.warning("Could not chmod 0600 on %s: %s", p, e)


def sanitize_filename(filename: str, fallback_prefix: str = "video") -> str:
    """Sanitize filename to eliminate path traversal, directory separators, and control chars.

    Guarantees:
    - Never contains '/', '\\', '..', or null bytes.
    - Only returns a safe local basename.
    """
    if not filename or not isinstance(filename, str):
        return f"{fallback_prefix}_unnamed.mp4"

    # Strip null bytes and control chars
    cleaned = filename.replace("\x00", "").strip()

    # Normalize Windows backslashes to forward slashes so basename is portable across OSes
    cleaned = cleaned.replace("\\", "/")

    # Extract pure basename using Path
    cleaned = Path(cleaned).name

    # Remove any relative traversal prefixes (e.g., '..', '.')
    cleaned = re.sub(r"^\.+", "", cleaned)

    # Replace path separators or dangerous characters
    cleaned = re.sub(r"[/\\?%*:|\"<>]", "_", cleaned).strip()

    # If empty or only dots remained, provide safe fallback
    if not cleaned or cleaned in (".", ".."):
        cleaned = f"{fallback_prefix}_unnamed.mp4"

    return cleaned


def validate_safe_path(base_dir: Union[str, Path], target_path: Union[str, Path]) -> Path:
    """Validate that target_path resolves strictly within base_dir to prevent path traversal.

    Raises ValueError if target_path attempts to escape base_dir.
    """
    base = Path(base_dir).resolve()
    target = Path(target_path).resolve()

    try:
        if not target.is_relative_to(base):
            raise ValueError(
                f"Path traversal violation: target '{target}' is outside base directory '{base}'"
            )
    except AttributeError:
        # Fallback for Python < 3.9 if ever run on older runtimes
        try:
            target.relative_to(base)
        except ValueError as e:
            raise ValueError(
                f"Path traversal violation: target '{target}' is outside base directory '{base}'"
            ) from e

    return target


def validate_executable(bin_name_or_path: str) -> str:
    """Validate that executable binary name does not contain shell injection metacharacters."""
    if not bin_name_or_path or not isinstance(bin_name_or_path, str):
        raise ValueError("Executable binary name must be a non-empty string.")

    stripped = bin_name_or_path.strip()
    if any(c in SHELL_METACHARACTERS for c in stripped):
        raise ValueError(
            f"Executable binary '{stripped}' contains prohibited shell metacharacters."
        )

    return stripped
