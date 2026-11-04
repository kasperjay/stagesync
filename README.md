# StageSync (ConcertVault)

> **Autonomous Live Concert Video Ingestion & YouTube Archival Pipeline**

StageSync connects your Google Photos cloud storage directly to YouTube, transforming disorganized phone concert clips (`file IMG_4821.MOV`, `file PXL_20240815.mp4`) into professionally tagged, setlist-verified live music video archives.

---

## Features

- **Google Photos Ingestion**: Autonomous discovery of concert videos with selective time and duration filtering (`=dv` download preserves QuickTime atoms).
- **ISO 6709 & EXIF Extraction**: Native `ffprobe` subprocess extraction of precise coordinates (`+39.6653-105.2058/`) and local recorded timestamps.
- **Geocoding & POI Resolution**: OpenStreetMap Nominatim reverse geocoding with local SQLite TTL caching and rate limiting.
- **Concert & Lineup Matching**: Setlist.fm API v2 integration to automatically fetch show headliners, supporting acts, tour names, and full setlists.
- **Hybrid Audio Identification**:
  - Acoustic fingerprinting via `shazamio`.
  - Setlist-constrained AI audio matcher (Gemini 2.5 Flash) to identify live tracks even amidst crowd roar, altered tempos, and noisy live acoustics.
- **Interactive Review CLI**: Rich terminal TUI for verifying, editing, or rejecting candidates before upload.
- **Manual Setlist.fm Show Search**: When automatic matching misses, search by date, artist, venue, city, or tour and attach the exact concert to a clip from the web review drawer.
- **Resumable 8MB YouTube Uploads**: Resilient chunked uploads to YouTube Data API v3 with automatic retry backoff and daily quota preservation guardrails.
- **SQLite WAL Engine**: Zero-setup local SQLite database with Write-Ahead Logging and audit logging for pipeline runs.

---

## Directory Structure

```markdown
stagesync/
├── stagesync/
│   ├── config.py              # Environment configuration & credential paths
│   ├── database/
│   │   ├── connection.py      # SQLite WAL connection & thread-safe queries
│   │   └── models.py          # Pydantic domain models matching ERD
│   ├── ingestion/
│   │   ├── google_photos.py   # Google Photos API client & video downloader
│   │   └── metadata_parser.py # FFprobe ISO 6709 location & timestamp parser
│   ├── intelligence/
│   │   ├── audio_extractor.py # FFmpeg 30s-60s audio clip demuxer
│   │   ├── geocoder.py        # Nominatim reverse geocode with TTL cache
│   │   ├── setlist_fm.py      # Setlist.fm API v2 client & rate limiter
│   │   ├── shazam_matcher.py  # Shazamio acoustic fingerprint matcher
│   │   └── ai_audio_judge.py  # Setlist-constrained AI audio matcher
│   ├── publisher/
│   │   ├── metadata_gen.py    # Title, description, tags, and chapter formatter
│   │   ├── youtube_auth.py    # YouTube OAuth2 token storage
│   │   └── resumable_upload.py# Resumable chunked upload engine
│   ├── pipeline.py            # Orchestration coordinator
│   └── cli.py                 # Rich CLI interface (scan, review, process, upload, status)
└── tests/                     # Unit and integration test suite
```

---

## Installation & Setup

### Prerequisites

- Python 3.11+
- `ffmpeg` and `ffprobe` installed on system path

```bash
# Clone and setup environment
cd /home/workspace/stagesync
cp .env.example .env

# Install dependencies
pip install -r requirements.txt
```

### Authentication Setup

1. **Google Photos API**:

   - Create OAuth 2.0 Client credentials in Google Cloud Console with `https://www.googleapis.com/auth/photoslibrary.readonly` scope.
   - Save client secrets to `file ~/.config/google-photos/credentials.json` or `.env`.

2. **YouTube Data API v3**:

   - Enable YouTube Data API v3 in Google Cloud Console.
   - Save OAuth client secret to `file ~/.config/stagesync/youtube_credentials.json` or `.env`.

3. **Setlist.fm API**:

   - Obtain an API key from [Setlist.fm API Settings](https://www.setlist.fm/settings/api).
   - Set `SETLIST_FM_API_KEY=your_key` in `.env`.

---

## CLI Usage

### Launch Web Dashboard

```bash
# Start local dark-mode Web Dashboard on port 8765
/home/workspace/stagesync/bin/stagesync serve --port 8765

# Or via Python module
python3 -m stagesync.cli serve
```

### Background Watcher Daemon

```bash
# Run continuous watcher polling Google Photos every 60 minutes
/home/workspace/stagesync/bin/stagesync daemon --interval 60

# Single run-through cycle
/home/workspace/stagesync/bin/stagesync daemon --run-once
```

### Check System & Authentication Status

```bash
python3 -m stagesync.cli status
```

### Scan & Discover Videos

```bash
# Scan past 30 days from Google Photos
python3 -m stagesync.cli scan --days 30

# Scan local directory directly
python3 -m stagesync.cli scan --dir /path/to/videos

# Dry run simulation without downloading
python3 -m stagesync.cli scan --days 60 --dry-run
```

### Process Candidate Intelligence

```bash
python3 -m stagesync.cli process
```

### Review Queue (Interactive TUI)

```bash
python3 -m stagesync.cli review
```

### Dispatch Explicitly Queued Publications

```bash
# Dispatch publication jobs explicitly queued in Publish (respecting quota)
python3 -m stagesync.cli upload

# Limit the number of queued jobs processed
python3 -m stagesync.cli upload --limit 3
```

---

## Testing

Run the test suite:

```bash
python3 -m unittest discover tests
```

## Approval and publishing

The live aiohttp homepage serves `stagesync/web/index.html`, whose navigation opens
the working `dashboard.html` interface at `/workspace`. Review confirms **Venue → Show → Artist → Song**.
Suggested identities require confirmation. Unknown tracks, improvisations and covers
are explicit resolutions; a manual confirmation is not an acoustic confidence score.

Approval verifies identity and may create a draft. It does not queue or upload media.
Use Publish to edit title, description, tags, privacy, thumbnail frame and destinations,
then explicitly queue Upload Now or a future scheduled release. Schedules use
America/Chicago for display and UTC for persistence; scheduled YouTube releases require
private uploads. Uploaded-private, scheduled and publicly published are distinct states.

Identity changes invalidate dependent confirmations and block stale publication jobs.
Draft text is preserved and marked for review; regeneration is explicit. Existing
legacy queued uploads require fresh publication review. Published history and source
media are retained. CLI and daemon dispatch only explicitly queued publication jobs.

Authenticated channels are resolved from YouTube rather than placeholder account names.
Additional channel tokens live in the `channels` directory beside the configured
YouTube token. Each clip/channel pair has its own persisted job, resumable session,
result URL and error. Thumbnail failures are retried separately from successful uploads.
