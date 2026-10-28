# StageSync (ConcertVault) - System Architecture & Engineering Documentation

---

## 1. System Topology & Architectural Blueprint

StageSync (ConcertVault) is built as a high-throughput, fault-tolerant media ingestion and processing pipeline. It bridges cloud photo storage, geospatial intelligence, concert registries, neural audio recognition, and video distribution channels.

```mermaid
flowchart TD
    subgraph External_Services ["External Cloud Services & APIs"]
        GP[Google Photos Library API]
        OSM[OpenStreetMap / Nominatim]
        SFM[Setlist.fm API v2]
        SHZ[Shazam Acoustic Service]
        GEM[Gemini 2.5 Flash Audio LLM]
        YT[YouTube Data API v3 Resumable]
    end

    subgraph Ingestion_Engine ["Ingestion & Preprocessing Engine"]
        Poller[Google Photos Sync Worker]
        Downloader[Smart Range / Video Fetcher]
        Probe[FFprobe / EXIF Atom Extractor]
        AudioDemux[FFmpeg Audio Normalizer 44.1kHz]
    end

    subgraph Intelligence_Core ["Concert & Song Intelligence Engine"]
        GeoResolver[Geospatial Reverse Geocoder]
        ShowMatcher[Setlist.fm Event Correlator]
        Fingerprinter[Shazamio Acoustic Fingerprinter]
        AIAudioJudge[Setlist-Constrained Audio Reasoning]
    end

    subgraph State_Store ["Persistence & State Layer"]
        DB[(PostgreSQL / SQLite WAL Engine)]
        AuditLog[Pipeline Run Step Auditor]
    end

    subgraph User_Touchpoint ["Human-In-The-Loop Interaction"]
        CLI[Interactive Terminal UI / Rich CLI]
        WebUI[Next.js / Tailwind Review Dashboard]
    end

    subgraph Publisher ["Distribution Worker"]
        ResumableUpload[YouTube Chunked 8MB Uploader]
        MetadataFormatter[Smart Title & Chapter Formatter]
    end

    GP -->|1. List MediaItems type=VIDEO| Poller
    Poller -->|2. Queue Video ID| Downloader
    Downloader -->|3. Save Stream| Probe
    Probe -->|4. Extracted Coordinates & Time| GeoResolver
    GeoResolver -->|5. City & Venue Coordinates| OSM
    OSM -->|6. Venue Candidate| ShowMatcher
    ShowMatcher -->|7. Query Shows on Date| SFM
    SFM -->|8. Setlist & Lineup JSON| ShowMatcher
    
    Downloader -->|9. Extract Audio| AudioDemux
    AudioDemux -->|10. 30s PCM Slice| Fingerprinter
    Fingerprinter -->|11. Query Signature| SHZ
    
    Fingerprinter -.->|12. If Low Confidence| AIAudioJudge
    ShowMatcher -.->|Candidate Setlist| AIAudioJudge
    AIAudioJudge -->|13. Audio + Setlist Analysis| GEM
    
    ShowMatcher --> DB
    Fingerprinter --> DB
    AIAudioJudge --> DB
    
    DB -->|14. Query PENDING_REVIEW| CLI
    DB -->|14. Query PENDING_REVIEW| WebUI
    
    CLI -->|15. Approve / Edit| DB
    WebUI -->|15. Approve / Edit| DB
    
    DB -->|16. Pick Up APPROVED| MetadataFormatter
    MetadataFormatter --> ResumableUpload
    ResumableUpload -->|17. Stream 8MB Chunks| YT
    YT -->|18. Return Video ID & URL| DB
```

---

## 2. Google Photos API Scope Change (2025)

Google deprecated the `photoslibrary.readonly` scope on March 31, 2025. This means the Library API can no longer scan a user's full photo library. The remaining valid scopes are:

- `photoslibrary.readonly.appcreateddata` — only returns media items created by this app
- `photospicker.mediaitems.readonly` — used by the Google Photos Picker API for user-selected videos
- `photoslibrary.appendonly` — for uploading, not discovery

**Recommended ingestion paths**:
1. **Picker flow** (`stagesync scan --picker`) — user selects videos from their library via Google's Picker UI
2. **Local Directory** (`stagesync scan --dir /path/to/concert_videos`) — scan a locally synced folder of concert videos
3. **App-created media only** (Library API `mediaItems:search`) — only returns videos previously uploaded by this app

---

## 3. End-to-End Sequence Lifecycle

The following sequence illustrates the exact execution path of a single concert video from Google Photos discovery through YouTube publication:

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Worker as Pipeline Ingestion Daemon
    participant GP as Google Photos API
    participant FF as Local FFmpeg / Probe
    participant OSM as Nominatim / OSM API
    participant SFM as Setlist.fm API
    participant SHZ as Shazamio Service
    participant GEM as Gemini 2.5 Flash Audio
    participant DB as Local Database (SQLite/Postgres)
    participant UI as CLI / Web Reviewer
    participant YT as YouTube Data API v3

    User->>Worker: Run `stagesync scan --picker` or `--dir /path/to/concert_videos`
    alt Picker flow
        Worker->>GP: POST /v1/sessions (Picker API)
        GP-->>Worker: Return pickerUri
        User->>User: Select concert videos in Google Photos Picker UI
        Worker->>GP: GET /v1/sessions/{id}/mediaItems (poll)
        GP-->>Worker: Return selected items (id, baseUrl, creationTime)
    else Local Directory flow
        Worker->>Worker: Scan local directory for video files (.mp4, .mov)
        Worker->>Worker: Check creation time & GPS via ffprobe
    end
    Worker->>DB: Check if source_photo_id exists
    alt Already Processed
        Worker->>Worker: Skip item
    else New Video
        Worker->>DB: INSERT into videos (status='DISCOVERED')
        Worker->>GP: GET baseUrl + "=dv" (Download Video stream)
        GP-->>Worker: Binary MP4/MOV bytes
        Worker->>FF: Probe metadata (ISO 6709 GPS, local creation timestamp)
        FF-->>Worker: lat: 39.6653, lng: -105.2058, recorded_at: 2024-08-15T21:42:00-06:00
        Worker->>OSM: GET /reverse?lat=39.6653&lon=-105.2058
        OSM-->>Worker: Venue: "Red Rocks Amphitheatre", City: "Morrison", State: "CO"
        Worker->>SFM: GET /search/setlists?date=15-08-2024&lat=39.6653&long=-105.2058
        SFM-->>Worker: Event: "King Gizzard & The Lizard Wizard", Setlist: ["Gila Monster", "Dragon", ...]
        Worker->>FF: Extract 30s audio segment (-ss 00:00:20 -t 30 -ar 44100 -ac 1)
        FF-->>Worker: 30s mono MP3
        Worker->>SHZ: Recognize acoustic signature
        alt Acoustic Match Found
            SHZ-->>Worker: Detected: "Dragon" by "King Gizzard" (Confidence: 0.94)
        else Acoustic Match Fails (Noisy live acoustics)
            Worker->>GEM: Send 30s Audio + Setlist songs prompt
            GEM-->>Worker: Detected: "Dragon" (Confidence: 0.91, Evidence: "Guitar riff and chorus")
        end
        Worker->>DB: UPDATE videos SET status='PENDING_REVIEW'
        Worker->>DB: INSERT into identifications & concerts
    end

    User->>UI: Run `stagesync review`
    UI->>DB: SELECT * FROM videos WHERE processing_status='PENDING_REVIEW'
    DB-->>UI: Display video card (Date, Venue, Artist, Song Title)
    User->>UI: Confirm [Approve]
    UI->>DB: UPDATE videos SET status='APPROVED'

    Worker->>DB: SELECT * FROM videos WHERE processing_status='APPROVED'
    Worker->>YT: POST /upload/youtube/v3/videos?uploadType=resumable (Title, Description, Tags)
    YT-->>Worker: Resumable URI
    loop Chunked Upload (8MB per chunk)
        Worker->>YT: PUT {chunk_bytes} with Content-Range
        YT-->>Worker: 308 Resume Incomplete / 200 OK
    end
    YT-->>Worker: YouTube Video ID (e.g., "dQw4w9WgXcQ")
    Worker->>DB: INSERT into youtube_uploads & UPDATE videos status='PUBLISHED'
    Worker-->>User: Notification: "Published to https://youtu.be/dQw4w9WgXcQ"
```

---

## 4. Directory & Component Boundaries

The project is structured as a high-cohesion, low-coupling modular workspace located in `/home/workspace/stagesync/`:

```
/home/workspace/stagesync/
├── pyproject.toml              # UV / Poetry dependencies (FastAPI, Typer, Shazamio, google-api-python-client)
├── README.md                   # Quickstart documentation
├── .env.example                # Canonical environment variable templates
│
├── stagesync/
│   ├── __init__.py
│   ├── config.py               # Pydantic v2 Settings (environment validation)
│   ├── cli.py                  # Typer / Rich CLI entry points
│   │
│   ├── core/                   # Core business logic & orchestrator
│   │   ├── pipeline.py         # Pipeline coordinator & DAG runner
│   │   ├── state_machine.py    # Transition rules (DISCOVERED -> PUBLISHED)
│   │   └── scheduler.py        # Proactive background cron watcher
│   │
│   ├── database/               # Relational persistence layer
│   │   ├── connection.py       # Engine & session management (SQLite / PostgreSQL)
│   │   ├── models.py           # SQLModel / SQLAlchemy 2.0 ORM definitions
│   │   └── migrations/         # Alembic migration revisions
│   │
│   ├── ingestion/              # Ingestion & container extraction
│   │   ├── google_photos.py    # Google Photos Library API client & OAuth handler
│   │   ├── ffmpeg_probe.py     # FFprobe QuickTime / MP4 atom extractor
│   │   └── audio_extractor.py  # Audio demuxing & loudness normalization
│   │
│   ├── intelligence/           # Show & song correlation
│   │   ├── geocoder.py         # Nominatim & Overpass reverse geocoding
│   │   ├── setlist_fm.py       # Setlist.fm API v2 client with rate limiter
│   │   ├── shazam_matcher.py   # Shazamio async audio fingerprinting
│   │   └── ai_audio_judge.py   # Gemini 2.5 Flash setlist-constrained analyzer
│   │
│   ├── publisher/              # YouTube distribution
│   │   ├── youtube_client.py   # YouTube Data API v3 OAuth & client
│   │   ├── resumable_upload.py # Resumable 8MB chunked streaming uploader
│   │   └── metadata_gen.py     # Title, description, tags, and chapter formatter
│   │
│   └── web/                    # FastAPI backend for Web Dashboard
│       ├── app.py              # FastAPI application server
│       ├── routes/             # REST endpoints (/videos, /pipeline, /status)
│       └── static/             # Built UI distribution or minimal dashboard
│
└── tests/                      # Pytest verification suite
    ├── unit/                   # Unit tests for metadata parsing & formatting
    ├── integration/            # Mocked API integration tests
    └── fixtures/               # Sample MP4 headers, setlist payloads, and audio slices
```

---

## 5. Step-by-Step Setup & Authentication Guide

### 4.1 Prerequisites & Toolchain Verification
Ensure all required system binaries are present:
```bash
which ffmpeg ffprobe python3 sqlite3
```
Install the project dependencies using `uv` or `pip`:
```bash
pip install typer rich requests google-auth google-auth-oauthlib google-api-python-client shazamio pydantic-settings sqlmodel alembic timezonefinder
```

### 4.2 Credentials Configuration (`.env`)
Create `/home/workspace/stagesync/.env`:
```ini
# Database (Defaults to local SQLite)
DATABASE_URL=sqlite:////home/workspace/stagesync/stagesync.db

# Google Photos API Credentials
GOOGLE_PHOTOS_CLIENT_ID=your_google_photos_client_id.apps.googleusercontent.com
GOOGLE_PHOTOS_CLIENT_SECRET=your_google_photos_client_secret
GOOGLE_PHOTOS_REDIRECT_URI=http://localhost:8090/callback

# YouTube Data API v3 Credentials
YOUTUBE_CLIENT_ID=your_youtube_client_id.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=your_youtube_client_secret
YOUTUBE_REDIRECT_URI=http://localhost:8090/youtube_callback

# Music & Concert Intelligence APIs
SETLIST_FM_API_KEY=your_setlist_fm_api_key
GEMINI_API_KEY=your_gemini_api_key

# Pipeline Runtime Policy
STAGESYNC_DEFAULT_PRIVACY=unlisted     # private | unlisted | public
STAGESYNC_AUTO_APPROVE_THRESHOLD=0.92  # Confidence required to bypass manual review
STAGESYNC_MAX_DAILY_UPLOADS=5          # YouTube API quota preservation guardrail
```

### 4.3 Google Cloud Project & API Activation
1. Navigate to [Google Cloud Console](https://console.cloud.google.com).
2. Create or select a project (e.g., `stagesync-concerts`).
3. Enable the following APIs:
   - **Photos Library API**
   - **YouTube Data API v3**
4. Configure the **OAuth Consent Screen**:
   - User Type: **External**
   - Scopes:
     - `https://www.googleapis.com/auth/photoslibrary.readonly`
     - `https://www.googleapis.com/auth/youtube.upload`
   - Add your Google account under **Test Users**.
5. Create OAuth 2.0 Client IDs (Application Type: **Desktop app** or **Web application**).
6. Save Client ID and Client Secret in `.env`.

### 4.4 Setlist.fm API Key
1. Register an account at [Setlist.fm](https://www.setlist.fm).
2. Apply for an API Key at [Setlist.fm API Settings](https://www.setlist.fm/settings/api).
3. Save the generated key as `SETLIST_FM_API_KEY`.

---

## 6. CLI & Web UI Operational Specifications

### 5.1 Command-Line Interface (CLI)

#### 1. Authentication
```bash
# Authenticate Google Photos OAuth
stagesync auth google-photos

# Authenticate YouTube Data API OAuth
stagesync auth youtube

# Verify active credentials and quota status
stagesync auth status
```

#### 2. Discovery & Scanning
```bash
# Scan all videos from the past 30 days
stagesync scan --days 30

# Scan specific album
stagesync scan --album "ALBUM_ID_HERE"

# Dry-run scan (simulate EXIF extraction and concert matching without downloading full files)
stagesync scan --days 60 --dry-run
```

#### 3. Interactive Review & Approval
```bash
stagesync review
```
*TUI Output Layout*:
```text
================================================================================
StageSync Review [Candidate 1 of 4]
================================================================================
File:         PXL_20240815_214201.mp4 (Duration: 01:24 | Size: 184 MB)
Recorded At:  Thursday, August 15, 2024 at 9:42:01 PM MDT
Location:     Red Rocks Amphitheatre (Morrison, Colorado, USA)
[39.6653° N, 105.2058° W]

MATCHED SHOW:
  Artist:     King Gizzard & The Lizard Wizard
  Tour:       North America Tour 2024
  Lineup:     Geese (Opener), King Gizzard & The Lizard Wizard (Headliner)
  Setlist.fm: https://www.setlist.fm/setlist/king-gizzard-...-3bca982c.html

IDENTIFIED TRACK:
  Song:       Dragon (Track #9 of 21)
  Service:    shazamio (Confidence: 94.2%)
  Setlist:    Verified against Setlist.fm roster

TARGET YOUTUBE METADATA:
  Title:      King Gizzard & The Lizard Wizard - Dragon (Live at Red Rocks - Aug 15, 2024)
  Privacy:    unlisted
--------------------------------------------------------------------------------
[A] Approve & Queue Upload   [E] Edit Metadata   [P] Change Privacy   [R] Reject   [S] Skip
Selection: _
```

#### 4. Resumable Upload Dispatcher
```bash
# Upload all approved videos (respecting daily quota limit)
stagesync upload

# Force upload with public privacy override
stagesync upload --limit 3 --privacy public
```

---

## 7. Swarm Multi-Agent Execution Roadmap

To construct StageSync with zero regression and maximum engineering speed, the Swarm Director delegates tasks across specialized agents:

```mermaid
flowchart TD
    Architect[Swarm Architect - System Topology & PRD/ERD]
    
    subgraph Implementation_Phase ["Subagent Fan-Out Execution"]
        Backend[fullstack-craftsman: Ingestion & Pipeline Core]
        Intelligence[fullstack-craftsman: Setlist & Audio AI Engine]
        PublisherAgent[fullstack-craftsman: YouTube Resumable & Review CLI]
    end

    subgraph Quality_Gate ["Quality Guardian & Hardener"]
        Tester[quality-guardian: TDD Debugger & Mock Test Suites]
        Auditor[quality-guardian: Anti-Slop & Security Linter]
    end

    Architect --> Backend
    Architect --> Intelligence
    Architect --> PublisherAgent
    
    Backend --> Tester
    Intelligence --> Tester
    PublisherAgent --> Tester
    
    Tester --> Auditor
    Auditor --> ProductionReady[Production Ready CLI & Pipeline]
```

### Delegation Task Slices:
1. **Subagent `fullstack-craftsman` (Core Engine & Ingestion)**:
   - Scaffold project structure in `/home/workspace/stagesync/`.
   - Implement `database/models.py` and `database/connection.py` using SQLModel / SQLite WAL.
   - Implement `ingestion/google_photos.py` using Google Photos API + credentials at `~/.config/google-photos/credentials.json`.
   - Implement `ingestion/ffmpeg_probe.py` using `ffprobe` to extract ISO 6709 location atoms.
2. **Subagent `fullstack-craftsman` (Concert & Audio Intelligence)**:
   - Implement `intelligence/geocoder.py` (Nominatim reverse geocode with SQLite TTL cache).
   - Implement `intelligence/setlist_fm.py` (Setlist.fm API query with rate limiter).
   - Implement `intelligence/shazam_matcher.py` (Shazamio async client).
   - Implement `intelligence/ai_audio_judge.py` (Gemini 2.5 Flash setlist-constrained prompt).
3. **Subagent `fullstack-craftsman` (YouTube Publisher & Review CLI)**:
   - Implement `publisher/youtube_client.py` and `publisher/resumable_upload.py` (8MB chunk streaming).
   - Implement `publisher/metadata_gen.py` (Title and description formatting).
   - Implement `cli.py` with Typer + Rich for the interactive review workflow.
4. **Subagent `quality-guardian` (Testing, Hardening & Anti-Slop)**:
   - Create mock fixtures for Google Photos responses, Setlist.fm API payloads, and audio snippets.
   - Implement automated pytest suite covering 100% of EXIF parsing, setlist matching, and title templating.
   - Audit code against Anti-Slop 7 Pillars: no placeholder comments (`// TODO`), strict error handling, and type safety.
