# Product Requirements Document (PRD)
## StageSync (ConcertVault): Intelligent Live Concert Video Ingestion & YouTube Archival Pipeline

---

## 1. Executive Summary & Vision

### 1.1 Problem Statement
Music enthusiasts and concertgoers capture dozens of video clips at live shows on smartphones. These clips automatically back up to cloud storage (predominantly **Google Photos**), where they become lost in chronological obscurity. Video filenames (`IMG_4821.MOV`, `PXL_20240815_214512.mp4`) contain no semantic musical context:
- No artist name
- No tour name
- No venue name or physical location
- No song title or track number
- No context regarding whether the video captured an opening act, headliner, or encore

Manually organizing, identifying, trimming, tagging, and uploading these videos to YouTube is tedious, error-prone, and rarely done. Consequently, irreplaceable live music memories remain inaccessible, unindexed, and unshared.

### 1.2 Product Vision: StageSync (ConcertVault)
**StageSync** is an autonomous, intelligent multi-stage pipeline that:
1. **Discovers & Filters**: Sifts through a user's Google Photos library to isolate video clips captured during live music performances.
2. **Extracts Spatial-Temporal Metadata**: Extracts embedded QuickTime/EXIF metadata (sub-second timestamp, ISO 6709 GPS coordinates, device profile) from the video container.
3. **Matches Shows & Lineups**: Queries geospatial indexes (OpenStreetMap / Overpass) and historical live music registries (**Setlist.fm API**, **MusicBrainz**) to determine the exact venue, headlining artist, tour, and supporting lineup.
4. **Identifies Live Tracks**: Employs a hybrid identification architecture—coupling acoustic fingerprinting (**Shazamio / ACRCloud**) with **Setlist-Constrained Multimodal Audio AI** (gemini-2.5-flash / audio embeddings against the known setlist)—to accurately identify live song titles even amidst crowd roar, acoustics distortion, and tempo variations.
5. **Human-In-The-Loop Approval**: Provides an interactive CLI and Web Review UI for dry-runs, manual verification, and metadata editing.
6. **Publishes to YouTube**: Orchestrates chunked resumable uploads via the **YouTube Data API v3**, formatting professional titles, descriptive setlist notes, tour tags, and geo-recording metadata under user-configured privacy policies (`private`, `unlisted`, `public`).

---

## 2. User Personas & Core Journeys

### 2.1 User Personas
| Persona | Profile | Core Need | Key Frustration |
| :--- | :--- | :--- | :--- |
| **Alex (The Festival & Gig Enthusiast)** | Attends 20+ concerts and 3 festivals annually. Has ~800 phone videos in Google Photos. | Automatic discovery of concert videos and one-click bulk upload to YouTube. | Cannot remember which song was played at 11:15 PM at an outdoor festival stage 2 years ago. |
| **Sam (The Live Music Archivist)** | Collects rare bootlegs and live performance clips. Values high metadata fidelity. | Accurate venue records, exact setlist position, tour names, and customizable upload privacy. | AI hallucinations inventing wrong song titles; automated systems uploading duplicates without consent. |
| **Jordan (The Casual Concertgoer)** | Records 2-3 clips per gig on an iPhone. Wants friends and family to see their concert moments. | Zero-friction sync with friendly YouTube titles: `"Radiohead - Karma Police (Live at Madison Square Garden, NYC - Aug 10, 2024)"`. | Video sits hidden in Google Photos storage taking up cloud quota without being enjoyed. |

### 2.2 Core User Journeys

#### Journey 1: Autonomous Batch Ingestion & Discovery
```
[User initiates CLI / Scheduled Watcher]
       │
       ▼
[Google Photos API: List Videos with filter mediaType=VIDEO]
       │
       ▼
[Download stream / video probe via baseUrl + "=dv"]
       │
       ▼
[Extract EXIF/QuickTime: Timestamp + GPS Coordinates + Duration]
       │
       ▼
[Filter: Duration between 10s-900s & Time of Day between 18:00-03:00]
       │
       ▼
[Reverse Geocode Coordinates -> POI & Venue Check]
       │
       ▼
[Setlist.fm Query -> Confirmed Live Show Event]
       │
       ▼
[Extracted 44.1kHz Audio Track -> Shazamio + Setlist-Guided AI -> Song Identified]
       │
       ▼
[Persist Record to Database -> Status: PENDING_REVIEW]
```

#### Journey 2: Human-in-the-Loop Review & Approval (Dry-Run Mode)
```
[User runs `stagesync review` or visits Web Dashboard]
       │
       ▼
[System displays pending candidate:
 - Thumbnail & 10s audio/video preview
 - Date: 2024-08-15 21:42 EDT | Location: Red Rocks Amphitheatre, Morrison, CO
 - Matched Show: King Gizzard & The Lizard Wizard (World Tour 2024)
 - Identified Song: "Dragon" (Track #9 of 21) | Confidence: 94%
 - Draft YouTube Title: "King Gizzard & The Lizard Wizard - Dragon (Live at Red Rocks - Aug 15, 2024)"
 - Target Privacy: unlisted
]
       │
       ├──► [Option A: Approve as-is] ──► Status marked `APPROVED`
       ├──► [Option B: Edit Metadata] ──► Override song title / select from setlist dropdown ──► Status `APPROVED`
       └──► [Option C: Reject / Skip] ──► Video marked `REJECTED` (not uploaded)
```

#### Journey 3: Resumable YouTube Publication
```
[Worker selects records with Status = APPROVED]
       │
       ▼
[Acquire YouTube Data API v3 OAuth2 Token (Scope: youtube.upload)]
       │
       ▼
[Initiate Resumable Upload Session (POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable)]
       │
       ▼
[Stream Video File in 8MB Chunks with Exponential Backoff]
       │
       ▼
[Attach Metadata: Title, Description with Setlist timestamps, Tags, Recording Location/Date]
       │
       ▼
[Upload Completes -> Capture YouTube Video ID & Public URL]
       │
       ▼
[Update Database: Status = PUBLISHED -> Emit Completion Notification]
```

---

## 3. Phased Roadmap & Scope

| Phase | Milestone | Scope & Deliverables | Exit Criteria |
| :--- | :--- | :--- | :--- |
| **Phase 1** | **Ingestion & Metadata Foundation (MVP)** | • Google Photos OAuth2 & API client (`mediaItems:search` for videos).<br>• Local caching engine & file integrity verification (SHA-256).<br>• Container inspection: `ffprobe` + QuickTime ISO 6709 GPS / creation timestamp extraction.<br>• SQLite local database schema & DDL execution.<br>• Nominatim / OpenStreetMap reverse geocoding with local cache. | Download 5 sample concert videos, extract exact lat/long and timestamp, resolve city/venue name with 100% accuracy. |
| **Phase 2** | **Concert & Song Identification Engine** | • Setlist.fm API v2 integration (geo + date search, lineup & full setlist parsing).<br>• Audio demuxing & normalization via `ffmpeg` (44.1kHz mono PCM/MP3).<br>• Dual-layer audio identification:<br>  - Layer 1: `shazamio` acoustic fingerprinting.<br>  - Layer 2: Gemini 2.5 Flash setlist-constrained multimodal audio analysis.<br>• Concert candidate classifier (time-of-day, audio SPL, venue radius). | Achieve ≥ 90% song identification accuracy across a benchmark of 20 live concert clips. |
| **Phase 3** | **YouTube Publishing & Review CLI** | • YouTube Data API v3 OAuth2 integration.<br>• 8MB chunked resumable upload client with network retry and progress bar.<br>• Smart titling & description templating engine.<br>• Interactive CLI Reviewer (`rich` / `curses`) with dry-run mode.<br>• State machine orchestration: `DISCOVERED` -> `PENDING_REVIEW` -> `APPROVED` -> `PUBLISHED`. | Successfully upload an approved concert video to YouTube with title, description, tags, and private/unlisted status. |
| **Phase 4** | **Web Dashboard & Swarm Automation** | • Modern Next.js / Tailwind CSS web dashboard for visual video review.<br>• Background scheduler / cron watcher polling Google Photos for new concert clips.<br>• Audio stem separation (Demucs / Spleeter) for crowd-noise suppression prior to identification.<br>• Multi-user support with PostgreSQL + Row Level Security (RLS). | End-to-end zero-touch discovery through web-based one-click approval and publication. |

---

## 4. Functional Specifications

### 4.1 Ingestion & Discovery (Google Photos Library API)
- **API Base**: `https://photoslibrary.googleapis.com/v1`
- **Authentication**: OAuth 2.0 with scopes:
  - `https://www.googleapis.com/auth/photoslibrary.readonly`
  - `https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata`
- **Discovery Strategy**:
  - Endpoint: `POST /v1/mediaItems:search`
  - Request Payload:
    ```json
    {
      "pageSize": 50,
      "filters": {
        "mediaTypeFilter": {
          "mediaTypes": ["VIDEO"]
        },
        "dateFilter": {
          "ranges": [
            {
              "startDate": { "year": 2022, "month": 1, "day": 1 },
              "endDate": { "year": 2026, "month": 12, "day": 31 }
            }
          ]
        }
      }
    }
    ```
- **Video Download Mechanics**:
  - The API returns a `baseUrl`. For videos, appending `=dv` initiates the original video binary stream (preserving QuickTime atoms and EXIF tags).
  - Rate Limiting & Quota: 10,000 read requests per day. The client must implement adaptive backoff with local SQLite caching of `mediaItemId` to prevent redundant downloads.
- **Candidate Filtering Heuristics**:
  1. **Duration Filter**: `10 seconds <= duration <= 900 seconds` (concert snippets or full songs; discards <10s pocket taps and >15m continuous recordings).
  2. **Temporal Filter**: Recorded between 17:30 and 02:30 local time (prime concert performance hours; configurable for daytime festivals).
  3. **Acoustic Energy Probe**: Rapid FFmpeg `ebur128` scan for high integrated loudness (Live concerts: `I > -16 LUFS`, high dynamic range).

### 4.2 Metadata & Geolocation Extraction
- **Container Parser Stack**:
  1. Primary: `ffprobe` (JSON stream format inspection)
  2. Fallback: ExifTool / Pymediainfo
- **QuickTime / MP4 Atoms Targeted**:
  - Location: `com.apple.quicktime.location.ISO6709` or `moov/udta/meta/ilst/©xyz`
    - Format: `+37.7749-122.4194+012.000/` -> Parsed to `latitude = 37.7749`, `longitude = -122.4194`, `altitude = 12.0m`.
  - Timestamp: `com.apple.quicktime.creationdate` (contains ISO-8601 with timezone offset, e.g., `2024-08-15T21:42:10-04:00`). If missing timezone, cross-reference UTC `creation_time` with the extracted GPS timezone via `timezonefinder`.
- **Reverse Geocoding & POI Resolution**:
  - Provider: OpenStreetMap / Nominatim API (`https://nominatim.openstreetmap.org/reverse`)
  - Policy: Compliant with OSM usage policy (max 1 req/sec, custom `User-Agent: StageSync/1.0`).
  - Fallback / Venue Query: Overpass API querying `amenity=music_venue`, `leisure=stadium`, `amenity=theatre`, or `amenity=nightclub` within a 350-meter radius of the coordinate.

### 4.3 Concert & Show Matching (Setlist.fm & MusicBrainz)
- **Primary Source: Setlist.fm API v2**:
  - Base URL: `https://api.setlist.fm/rest/1.0`
  - Headers: `x-api-key: {SETLIST_FM_API_KEY}`, `Accept: application/json`
  - Query Pattern:
    ```http
    GET /search/setlists?date=15-08-2024&lat=39.6653&long=-105.2058&radius=5 HTTP/1.1
    ```
  - Response Ingestion:
    - `artist.name`: Headlining artist (and supporting acts)
    - `venue.name`: Official venue name (e.g., "Red Rocks Amphitheatre")
    - `venue.city.name`, `venue.city.state`, `venue.city.country`
    - `tour.name`: Associated tour (e.g., "North America 2024 Tour")
    - `sets.set[].song[]`: Array of all tracks performed, including `name`, `info`, `with` (guest artists), and `cover` flags.
- **Secondary Fallback: MusicBrainz & Bandsintown**:
  - In cases where Setlist.fm has no entry (e.g., local indie shows), query MusicBrainz `event` entity by date and geographical area.
  - If no show registry matches, label as `"Live at {Venue Name}"` with `CONCERT_MATCH_CONFIDENCE = LOW` and prompt human review.

### 4.4 Live Song Identification & Audio Analysis
Live recordings suffer from acoustic echo, altered tempos, guitar solos, crowd singing, and audience cheers. Standard acoustic fingerprinting fails on ~40% of phone concert videos. StageSync solves this with a two-tier hybrid engine:

#### Tier 1: Acoustic Fingerprinting (`shazamio`)
- Demux a 30-second audio slice from the middle of the video:
  ```bash
  ffmpeg -ss {midpoint - 15} -t 30 -i input.mp4 -vn -ar 44100 -ac 1 -b:a 128k sample.mp3
  ```
- Send to `shazamio` async client.
- If match found AND detected artist matches Setlist.fm artist:
  - Confidence = **0.95**. Mark song as verified against the setlist.

#### Tier 2: Setlist-Constrained Multimodal Audio AI (Gemini 2.5 Flash / Audio LLM)
- When acoustic fingerprinting returns no match (or low confidence):
- Construct a constrained prompt passing:
  1. The 30-second audio file.
  2. The known Setlist.fm song roster for that specific night (15-25 candidate songs).
  3. The artist's known discography styles.
- **Prompt Specification**:
  ```text
  You are an expert musicologist and live concert sound engineer.
  Attached is a 30-second live audio recording taken at [Venue] on [Date].
  The artist performing is [Artist].
  Their exact setlist for this concert was:
  1. [Song A]
  2. [Song B]
  ...
  N. [Song N]

  Listen to the audio snippet (vocals, guitar riffs, drum beats, lyrics sung, bassline).
  Identify which of the setlist songs (or encore/tape intro) is being performed.
  Output JSON format strictly conforming to:
  {
    "song_title": string,
    "confidence": float (0.0 to 1.0),
    "setlist_index": integer,
    "lyrics_detected": string,
    "reasoning": string
  }
  ```
- This reduces search space from 100,000,000 global songs to ~25 candidate songs, yielding an empirical live accuracy exceeding **92%**.

### 4.5 YouTube Data API v3 Ingestion & Upload
- **API Base**: `https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable`
- **Authentication**: OAuth 2.0 with scope `https://www.googleapis.com/auth/youtube.upload`
- **Resumable Upload Protocol**:
  1. **Session Initiation**:
     - `POST /upload/youtube/v3/videos?uploadType=resumable&part=snippet,status,recordingDetails`
     - Header: `X-Upload-Content-Type: video/mp4`, `X-Upload-Content-Length: {file_size}`
     - Body: Video resource JSON with title, description, tags, category (`10` = Music), privacy status, and recording date/location.
     - Response: `Location` header containing the resumable upload URL.
  2. **Chunked Streaming**:
     - Stream file in `8,388,608` byte (8MB) chunks using HTTP PUT.
     - Header: `Content-Range: bytes {start}-{end}/{total}`
     - Handles network interruptions: on disconnect, sends `PUT` with `Content-Range: bytes */{total}` to query upload status, then resumes from last acknowledged byte.
- **Title Formatting Engine**:
  - Standard format:
    ```text
    {Artist} - {Song Title} (Live at {Venue}, {City} - {Date:MMM D, YYYY})
    ```
  - If song unidentified:
    ```text
    {Artist} - Live at {Venue}, {City} ({Date:MMM D, YYYY}) [Clip #{Index}]
    ```
- **Description Templating Engine**:
  ```text
  Live performance of "{Song Title}" by {Artist}.
  
  📍 Venue: {Venue Name}, {City}, {State/Country}
  🗓️ Date: {Date:dddd, MMMM D, YYYY}
  🎸 Tour: {Tour Name}
  🎵 Setlist Context: Track #{Setlist Index} of {Total Tracks}
  
  Recorded on {Device Make} {Device Model}.
  Concert details verified via Setlist.fm: {Setlist.fm Event URL}
  
  Setlist Lineup:
  {Setlist Listing}
  
  #LiveMusic #{ArtistClean} #{VenueClean} #Concert
  ```
- **Recording Details Mapping**:
  - `recordingDetails.recordingDate`: ISO-8601 recorded timestamp.
  - `recordingDetails.location`: Latitude & Longitude from video EXIF.

### 4.6 Human-in-the-Loop Review Mode
- **Dry-Run Mode (`--dry-run`)**:
  - Executes full discovery, EXIF extraction, concert matching, audio identification, and metadata generation without making mutations to YouTube or downloading final multi-gigabyte video files.
  - Outputs a structured JSON report or rich CLI table.
- **Interactive Review CLI (`stagesync review`)**:
  - Lists pending matches in order of recording date.
  - Interactive actions:
    - `[a]` Approve for upload
    - `[e]` Edit song title / select from setlist candidates
    - `[p]` Toggle privacy status (`private` / `unlisted` / `public`)
    - `[r]` Reject / Ignore video
    - `[s]` Skip to next video
- **Batch Auto-Approve Threshold**:
  - Users can configure an auto-approve confidence threshold in config (e.g., `auto_approve_threshold: 0.95`). Records meeting or exceeding this score bypass manual review and schedule for upload.

---

## 5. Non-Functional Requirements (NFRs) & Operational Budgets

### 5.1 Latency & Performance Budgets
| Operation | Target Budget | Hard Timeout | Strategy |
| :--- | :--- | :--- | :--- |
| **Google Photos Video Query** | < 800ms / page | 5,000ms | Indexed search with page token caching |
| **Video Header / EXIF Probe** | < 1,500ms | 10,000ms | HTTP Range request for first 15MB or streaming `ffprobe` |
| **Reverse Geocoding** | < 250ms (cached) | 2,000ms | SQLite SQLite/Geo hash cache; 1 req/sec rate limit |
| **Setlist.fm Search** | < 600ms | 3,000ms | Query with 5km bounding box and exact date |
| **Audio Fingerprint (Shazamio)**| < 2,000ms | 6,000ms | 30s 64kbps mono snippet extraction |
| **Gemini Audio Identification** | < 4,000ms | 15,000ms | Compressed 16kHz audio payload to Gemini 2.5 Flash |
| **YouTube 8MB Chunk Upload** | < 1,200ms / chunk | 30,000ms | Concurrent streaming with 3 retry exponential backoff |

### 5.2 Quotas & Rate Limits Management
1. **Google Photos Library API**:
   - Quota: 10,000 read requests per project per day.
   - Strategy: Persist `mediaItemId` in local database. Once cataloged, never query again unless explicit `--rescan` is requested.
2. **Setlist.fm API**:
   - Quota: 2 requests per second per IP/API key.
   - Strategy: Rate limiter queue with token bucket (`rate = 1.8 req/sec`).
3. **YouTube Data API v3**:
   - Quota: 10,000 units per day default.
   - Cost per operation: `videos.insert` costs **1,600 units**.
   - **Critical Architectural Invariant**: On default quota, a user can upload a maximum of **6 videos per 24-hour window**.
   - **Mitigation**: StageSync enforces a daily upload queue manager (`MAX_DAILY_UPLOADS = 5`). If queue exceeds 5, videos remain in `APPROVED` state and upload automatically the following day at 00:00 UTC (or prompt user to request quota increase from Google Cloud Console).
4. **OpenStreetMap / Nominatim**:
   - Quota: Maximum 1 request per second.
   - Strategy: Cache coordinate lookups truncated to 3 decimal places (~110 meters) in local database.

### 5.3 Security, Privacy & Compliance
- **Token Vault**: All OAuth2 tokens (`google_photos_tokens.json`, `youtube_tokens.json`) stored with restrictive filesystem permissions (`0600`) in `~/.config/stagesync/` or AES-GCM encrypted.
- **Zero Raw Credential Leakage**: No API keys or tokens committed to git or printed in logs.
- **Privacy First**: YouTube uploads default to `unlisted` or `private` until the user explicitly selects `public`.
- **GDPR / Data Retention**: Temporary video files downloaded to `/tmp/stagesync/` are verified and purged immediately upon successful YouTube upload or rejection.

---

## 6. System Boundary & Interface Contracts

### 6.1 CLI Interface Contract
```bash
# Authentication
stagesync auth google-photos     # Initiates Google Photos OAuth PKCE flow
stagesync auth youtube           # Initiates YouTube Data API OAuth flow
stagesync auth status            # Validates all access tokens and quotas

# Pipeline Operations
stagesync scan [--days N] [--album ID] [--dry-run]
stagesync match [--video-id ID] [--force]
stagesync identify [--use-ai]
stagesync review [--interactive]
stagesync upload [--limit N] [--privacy {private,unlisted,public}]

# All-in-one Autonomous Run
stagesync run --auto-approve-threshold 0.90 --privacy unlisted
```

### 6.2 REST API Endpoints (For Web UI Dashboard)
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/v1/videos` | Query cataloged videos with filter by status (`DISCOVERED`, `PENDING_REVIEW`, etc.) |
| `GET` | `/api/v1/videos/:id` | Get full video details, EXIF, concert match, and audio identification |
| `PATCH` | `/api/v1/videos/:id` | Update metadata (song title, artist, venue, privacy level) |
| `POST` | `/api/v1/videos/:id/approve`| Mark candidate as approved for upload |
| `POST` | `/api/v1/videos/:id/reject` | Mark candidate as rejected |
| `POST` | `/api/v1/pipeline/scan` | Trigger background discovery job |
| `POST` | `/api/v1/pipeline/upload` | Trigger background YouTube uploader worker |
| `GET` | `/api/v1/system/status` | Quota status for Google Photos, YouTube, and Setlist.fm |
