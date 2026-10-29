"""Audio Extraction and Normalization Module.

Demuxes a normalized 30-60 second mono 44.1kHz audio snippet from
a video file using an ffmpeg subprocess.
"""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from typing import List, Optional, Union

from stagesync.security import validate_executable

logger = logging.getLogger(__name__)


class AudioExtractor:
    """Extracts normalized audio clips from video files via ffmpeg."""

    def __init__(self, ffmpeg_bin: str = "ffmpeg") -> None:
        self.ffmpeg_bin = validate_executable(ffmpeg_bin)

    def extract_clip(
        self,
        video_path: Union[str, Path],
        output_path: Optional[Union[str, Path]] = None,
        duration_seconds: float = 30.0,
        offset_seconds: Optional[float] = None,
        video_duration: Optional[float] = None,
    ) -> Path:
        """Extract a 44.1kHz mono audio snippet centered at video midpoint.

        Parameters:
            video_path: Absolute or relative path to source video
            output_path: Destination path for extracted audio (defaults to .mp3 alongside video)
            duration_seconds: Length of audio segment to extract (default 30.0s)
            offset_seconds: Starting timestamp (if None, auto-calculated from midpoint)
            video_duration: Known video length to calculate midpoint
        """
        source = Path(video_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Video file does not exist: {source}")

        if output_path is None:
            out_file = source.with_suffix(".audio_clip.mp3")
        else:
            out_file = Path(output_path).resolve()

        out_file.parent.mkdir(parents=True, exist_ok=True)

        # Calculate midpoint offset if not explicitly provided
        if offset_seconds is None:
            if video_duration and video_duration > duration_seconds:
                offset_seconds = max(0.0, (video_duration - duration_seconds) / 2.0)
            elif video_duration and video_duration > 10.0:
                offset_seconds = 0.0
                duration_seconds = min(duration_seconds, video_duration)
            else:
                offset_seconds = 0.0

        cmd = [
            self.ffmpeg_bin,
            "-y",  # Overwrite output without asking
            "-ss",
            f"{offset_seconds:.3f}",
            "-t",
            f"{duration_seconds:.3f}",
            "-i",
            str(source),
            "-vn",  # Discard video stream
            "-ar",
            "44100",  # 44.1kHz sample rate
            "-ac",
            "1",  # Downmix to mono
            "-b:a",
            "128k",
            str(out_file),
        ]

        logger.debug("Executing ffmpeg: %s", " ".join(cmd))
        try:
            res = subprocess.run(cmd, shell=False, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            logger.error("ffmpeg extraction failed on %s: %s", source, e.stderr)
            raise RuntimeError(f"FFmpeg audio extraction failed: {e.stderr}") from e

        if not out_file.exists() or out_file.stat().st_size == 0:
            raise RuntimeError(f"Extracted audio file is empty or missing: {out_file}")

        return out_file

    def extract_clips(
        self,
        video_path: Union[str, Path],
        video_duration: Optional[float] = None,
        duration_seconds: float = 15.0,
        max_windows: int = 4,
        output_prefix: Optional[Union[str, Path]] = None,
    ) -> List[Path]:
        """Extract evenly spaced audio windows for resilient recognition."""
        if max_windows < 1:
            raise ValueError("max_windows must be at least 1")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")

        source = Path(video_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Video file does not exist: {source}")

        if not video_duration or video_duration <= duration_seconds:
            offsets = [0.0]
            window_duration = min(duration_seconds, video_duration) if video_duration else duration_seconds
        else:
            window_duration = duration_seconds
            latest_offset = max(0.0, video_duration - window_duration)
            window_count = min(max_windows, max(1, int(video_duration // window_duration)))
            if window_count == 1:
                offsets = [latest_offset / 2.0]
            else:
                offsets = [latest_offset * index / (window_count - 1) for index in range(window_count)]

        prefix = Path(output_prefix).resolve() if output_prefix else source.with_suffix("")
        clips: List[Path] = []
        for index, offset in enumerate(offsets, start=1):
            output_path = prefix.parent / f"{prefix.name}.audio_clip_{index:02d}.mp3"
            clips.append(
                self.extract_clip(
                    video_path=source,
                    output_path=output_path,
                    duration_seconds=window_duration,
                    offset_seconds=offset,
                    video_duration=video_duration,
                )
            )
        return clips
