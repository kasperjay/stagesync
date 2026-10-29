"""Video Still / Thumbnail Extraction Module.

Extracts normalized 16:9 JPEG preview frames from video files using ffmpeg
subprocesses for the Web Dashboard and User-Assist Review drawers.
"""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from typing import Optional, Union

from stagesync.config import get_settings
from stagesync.security import validate_executable

logger = logging.getLogger(__name__)

FALLBACK_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 854 480" width="100%" height="100%">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#090d16" />
      <stop offset="100%" stop-color="#111726" />
    </linearGradient>
    <linearGradient id="accentGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#6366f1" />
      <stop offset="100%" stop-color="#4f46e5" />
    </linearGradient>
  </defs>
  <rect width="854" height="480" fill="url(#bgGrad)" />
  <rect x="2" y="2" width="850" height="476" rx="12" fill="none" stroke="#1e293b" stroke-width="2" />
  <g transform="translate(427, 210)" text-anchor="middle">
    <circle cx="0" cy="0" r="44" fill="#172033" stroke="#334155" stroke-width="2" />
    <path d="M-14 -16 L-14 16 L16 0 Z" fill="url(#accentGrad)" />
    <text y="70" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="16" font-weight="600" fill="#94a3b8">Video Preview Frame</text>
    <text y="95" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" fill="#64748b">Frame extraction available on downloaded videos</text>
  </g>
</svg>"""


class ThumbnailExtractor:
    """Extracts high quality video still frames via ffmpeg."""

    def __init__(
        self,
        ffmpeg_bin: str = "ffmpeg",
        cache_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.ffmpeg_bin = validate_executable(ffmpeg_bin)
        settings = get_settings()
        self.cache_dir = Path(cache_dir or (settings.cache_dir / "thumbnails"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def get_fallback_svg() -> bytes:
        """Return fallback SVG image bytes when video still cannot be rendered."""
        return FALLBACK_SVG

    def get_cached_path(self, video_id: str, frame_index: int = 0) -> Path:
        """Return destination path for a cached thumbnail frame."""
        if frame_index <= 0:
            return self.cache_dir / f"{video_id}.jpg"
        return self.cache_dir / f"{video_id}-frame-{frame_index}.jpg"

    def extract_thumbnail(
        self,
        video_path: Union[str, Path],
        output_path: Optional[Union[str, Path]] = None,
        offset_seconds: float = 1.0,
        width: int = 854,
    ) -> Path:
        """Extract a single frame from video at specified offset.

        Parameters:
            video_path: Path to source video file.
            output_path: Target destination path for JPEG image.
            offset_seconds: Timestamp to seek in seconds (default 1.0s).
            width: Scaled width in pixels (maintains aspect ratio, default 854px).
        """
        source = Path(video_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Source video file not found: {source}")

        if output_path is None:
            out_file = self.cache_dir / f"{source.stem}_thumb.jpg"
        else:
            out_file = Path(output_path).resolve()

        out_file.parent.mkdir(parents=True, exist_ok=True)

        # Scale filter maintaining aspect ratio with even dimensions (-2)
        scale_filter = f"scale={width}:-2"

        def _run_ffmpeg(seek_sec: float) -> bool:
            cmd = [
                self.ffmpeg_bin,
                "-y",
                "-ss",
                f"{seek_sec:.3f}",
                "-i",
                str(source),
                "-vf",
                scale_filter,
                "-frames:v",
                "1",
                "-update",
                "1",
                "-q:v",
                "3",
                str(out_file),
            ]
            try:
                res = subprocess.run(
                    cmd,
                    shell=False,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return res.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0
            except Exception as e:
                logger.warning("ffmpeg thumbnail execution error: %s", e)
                return False

        # Attempt extraction at requested offset
        success = _run_ffmpeg(offset_seconds)
        if not success and offset_seconds > 0.0:
            # Fallback to beginning of video (frame 0)
            logger.debug("Retrying frame extraction at offset 0.0s for %s", source)
            success = _run_ffmpeg(0.0)

        if not success or not out_file.exists() or out_file.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg thumbnail extraction failed for: {source}")

        return out_file
