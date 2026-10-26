"""Unit tests for AudioExtractor subprocess handling, security, and midpoint offsets."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from stagesync.intelligence.audio_extractor import AudioExtractor


class TestAudioExtractor(unittest.TestCase):
    """Test suite for AudioExtractor subprocess calls, security invariants, and offsets."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        self.extractor = AudioExtractor(ffmpeg_bin="ffmpeg")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ffmpeg_bin_validation_rejects_shell_injection(self) -> None:
        malicious_binaries = [
            "ffmpeg; rm -rf /",
            "ffmpeg && id",
            "`whoami`",
            "ffmpeg | bash",
        ]
        for bad in malicious_binaries:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    AudioExtractor(ffmpeg_bin=bad)

    def test_missing_video_raises_file_not_found(self) -> None:
        missing = self.base_path / "nonexistent.mp4"
        with self.assertRaises(FileNotFoundError):
            self.extractor.extract_clip(missing)

    @patch("subprocess.run")
    def test_extract_clip_subprocess_invoked_safely(self, mock_run: MagicMock) -> None:
        video_file = self.base_path / "concert_video.mp4"
        video_file.write_bytes(b"dummy video binary data")

        out_file = self.base_path / "extracted.mp3"

        # Mock successful subprocess execution that creates out_file
        def create_out_file(*args, **kwargs):
            out_file.write_bytes(b"dummy audio binary data")
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

        mock_run.side_effect = create_out_file

        result = self.extractor.extract_clip(
            video_path=video_file,
            output_path=out_file,
            duration_seconds=30.0,
            video_duration=120.0,
        )

        self.assertEqual(result, out_file)
        self.assertTrue(mock_run.called)

        # Inspect subprocess.run arguments
        call_args, call_kwargs = mock_run.call_args
        cmd = call_args[0]

        # Verify command is an argv list
        self.assertIsInstance(cmd, list)
        self.assertTrue(all(isinstance(arg, str) for arg in cmd))
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-y", cmd)
        self.assertIn("-vn", cmd)
        self.assertIn("-ar", cmd)
        self.assertIn("44100", cmd)
        self.assertIn("-ac", cmd)
        self.assertIn("1", cmd)
        self.assertIn(str(video_file.resolve()), cmd)
        self.assertIn(str(out_file.resolve()), cmd)

        # Midpoint offset for 120s video with 30s duration should be (120 - 30) / 2 = 45.0s
        ss_idx = cmd.index("-ss")
        self.assertEqual(cmd[ss_idx + 1], "45.000")

        # Crucial security assertion: shell=False
        self.assertFalse(call_kwargs.get("shell", False))
        self.assertTrue(call_kwargs.get("check"))
        self.assertTrue(call_kwargs.get("capture_output"))
        self.assertTrue(call_kwargs.get("text"))

    @patch("subprocess.run")
    def test_extract_clip_short_video_offset(self, mock_run: MagicMock) -> None:
        video_file = self.base_path / "short_clip.mp4"
        video_file.write_bytes(b"short video")
        out_file = self.base_path / "out.mp3"

        def create_out_file(*args, **kwargs):
            out_file.write_bytes(b"audio")
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

        mock_run.side_effect = create_out_file

        # Video length is 15s (<= 30s duration)
        self.extractor.extract_clip(
            video_path=video_file,
            output_path=out_file,
            duration_seconds=30.0,
            video_duration=15.0,
        )

        cmd = mock_run.call_args[0][0]
        ss_idx = cmd.index("-ss")
        t_idx = cmd.index("-t")
        self.assertEqual(cmd[ss_idx + 1], "0.000")
        self.assertEqual(cmd[t_idx + 1], "15.000")

    @patch("subprocess.run")
    def test_extract_clip_ffmpeg_failure_raises_runtime_error(self, mock_run: MagicMock) -> None:
        video_file = self.base_path / "corrupt.mp4"
        video_file.write_bytes(b"corrupt")

        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["ffmpeg"],
            stderr="Invalid data found when processing input",
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.extractor.extract_clip(video_file)

        self.assertIn("FFmpeg audio extraction failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
