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
        self.extractor = AudioExtractor(ffmpeg_bin="ffmpeg", cache_dir=self.base_path / "playback_audio")

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

    @patch("subprocess.run")
    def test_extract_clips_uses_four_evenly_spaced_windows(self, mock_run: MagicMock) -> None:
        video_file = self.base_path / "long_clip.mp4"
        video_file.write_bytes(b"video")

        def create_out_file(*args, **kwargs):
            Path(args[0][-1]).write_bytes(b"audio")
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

        mock_run.side_effect = create_out_file

        clips = self.extractor.extract_clips(video_file, video_duration=120.0)

        self.assertEqual(len(clips), 4)
        offsets = [call.args[0][call.args[0].index("-ss") + 1] for call in mock_run.call_args_list]
        self.assertEqual(offsets, ["0.000", "35.000", "70.000", "105.000"])
        self.assertEqual([clip.name for clip in clips], [
            "long_clip.audio_clip_01.mp3",
            "long_clip.audio_clip_02.mp3",
            "long_clip.audio_clip_03.mp3",
            "long_clip.audio_clip_04.mp3",
        ])

    @patch("subprocess.run")
    def test_extract_full_audio_fast_remux_and_aac_fallback(self, mock_run: MagicMock) -> None:
        video_file = self.base_path / "giant_4k.mov"
        video_file.write_bytes(b"fake mov container")
        target_m4a = self.extractor.get_cached_playback_path("vid_123")

        # 1) Fast stream-copy path succeeds in a single ffmpeg pass
        def copy_pass(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"faststart aac m4a")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = copy_pass
        out = self.extractor.extract_full_audio(video_file, target_m4a)
        self.assertEqual(out, target_m4a)
        self.assertEqual(target_m4a.read_bytes(), b"faststart aac m4a")
        self.assertEqual(mock_run.call_count, 1)
        first_cmd = mock_run.call_args[0][0]
        self.assertIn("-vn", first_cmd)
        self.assertIn("copy", first_cmd)
        self.assertIn("+faststart", first_cmd)

        # 2) Fallback transcode path when stream-copy fails (e.g. pcm_s16le)
        target_pcm = self.extractor.get_cached_playback_path("vid_pcm")
        mock_run.reset_mock()

        def copy_fail_then_aac(cmd, **kwargs):
            if "copy" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="codec not supported in mp4")
            Path(cmd[-1]).write_bytes(b"transcoded aac m4a")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = copy_fail_then_aac
        out2 = self.extractor.extract_full_audio(video_file, target_pcm)
        self.assertEqual(out2, target_pcm)
        self.assertEqual(target_pcm.read_bytes(), b"transcoded aac m4a")
        self.assertEqual(mock_run.call_count, 2)
        second_cmd = mock_run.call_args_list[1][0][0]
        self.assertIn("aac", second_cmd)
        self.assertIn("128k", second_cmd)


if __name__ == "__main__":
    unittest.main()
