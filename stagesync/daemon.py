"""StageSync Background Watcher Daemon.

Periodically queries Google Photos for new concert videos, executes the
intelligence pipeline, stages matches for review, and optionally auto-dispatches
approved uploads within daily quota limits.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Any, Dict, Optional

from stagesync.config import Settings, get_settings
from stagesync.database.connection import Database, get_db
from stagesync.pipeline import PipelineCoordinator

logger = logging.getLogger(__name__)


class StageSyncDaemon:
    """Continuous background watcher for StageSync."""

    def __init__(
        self,
        coordinator: Optional[PipelineCoordinator] = None,
        db: Optional[Database] = None,
        settings: Optional[Settings] = None,
        interval_minutes: int = 60,
        auto_process: bool = True,
        auto_upload: bool = False,
        scan_limit: int = 50,
        process_limit: int = 25,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db or get_db()
        self.coordinator = coordinator or PipelineCoordinator(db=self.db, settings=self.settings)
        self.interval_seconds = max(60, interval_minutes * 60)
        self.auto_process = auto_process
        self.auto_upload = auto_upload
        self.scan_limit = scan_limit
        self.process_limit = process_limit
        self._running = False

    def stop(self, *args: Any) -> None:
        """Signal the daemon to stop gracefully."""
        logger.info("Shutdown signal received. Stopping StageSync daemon...")
        self._running = False

    def run_cycle(self) -> Dict[str, Any]:
        """Execute a single scan, intelligence, and upload maintenance cycle."""
        stats: Dict[str, Any] = {
            "scanned": 0,
            "processed": 0,
            "uploaded": 0,
            "errors": [],
        }

        logger.info("Executing StageSync daemon cycle...")

        # 1. Ingest / Discovery
        try:
            if self.coordinator.photos_client.is_authenticated():
                discovered = self.coordinator.scan_google_photos(limit=self.scan_limit)
                stats["scanned"] = len(discovered)
                logger.info("Daemon scanned %d new video(s) from Google Photos.", len(discovered))
            else:
                logger.warning("Google Photos is not authenticated. Skipping remote scan.")
        except Exception as e:
            logger.error("Error scanning Google Photos: %s", e)
            stats["errors"].append(f"Scan error: {e}")

        # 2. Intelligence Processing
        if self.auto_process:
            try:
                processed = self.coordinator.process_pending_videos(limit=self.process_limit)
                stats["processed"] = len(processed)
                logger.info("Daemon processed %d candidate video(s).", len(processed))
            except Exception as e:
                logger.error("Error processing pending videos: %s", e)
                stats["errors"].append(f"Process error: {e}")

        # 3. Upload Dispatch (if auto_upload enabled)
        if self.auto_upload:
            try:
                results = self.coordinator.upload_approved_videos()
                successful = [r for r in results if r.success]
                stats["uploaded"] = len(successful)
                logger.info("Daemon uploaded %d approved video(s) to YouTube.", len(successful))
            except Exception as e:
                logger.error("Error uploading approved videos: %s", e)
                stats["errors"].append(f"Upload error: {e}")

        return stats

    def start(self, run_once: bool = False) -> None:
        """Run the daemon loop until interrupted."""
        self._running = True

        # Register signal handlers for clean shutdown if running in main thread
        try:
            signal.signal(signal.SIGINT, self.stop)
            signal.signal(signal.SIGTERM, self.stop)
        except (ValueError, AttributeError):
            pass

        logger.info(
            "StageSync Daemon started. Interval: %d seconds. Auto-process: %s. Auto-upload: %s.",
            self.interval_seconds,
            self.auto_process,
            self.auto_upload,
        )

        while self._running:
            start_time = time.time()
            self.run_cycle()

            if run_once:
                logger.info("Run-once flag specified. Exiting daemon loop.")
                break

            # Sleep in small increments to respond quickly to shutdown signal
            elapsed = time.time() - start_time
            remaining_sleep = max(0.0, self.interval_seconds - elapsed)
            logger.info("Cycle finished. Next check in %.1f seconds.", remaining_sleep)

            while self._running and remaining_sleep > 0:
                step = min(1.0, remaining_sleep)
                time.sleep(step)
                remaining_sleep -= step

        logger.info("StageSync daemon loop terminated.")
