# StageSync (ConcertVault) - Context Map & Subagent Contract Slice

---

## 1. Domain Types & Interface Contracts

```typescript
// Core Entity Types for Downstream Subagents

export type VideoStatus =
  | 'DISCOVERED'
  | 'DOWNLOADING'
  | 'DOWNLOADED'
  | 'ANALYZING_METADATA'
  | 'CONCERT_MATCHED'
  | 'SONG_IDENTIFIED'
  | 'PENDING_REVIEW'
  | 'APPROVED'
  | 'REJECTED'
  | 'UPLOADING'
  | 'PUBLISHED'
  | 'FAILED';

export type PrivacyStatus = 'private' | 'unlisted' | 'public';

export interface VideoMetadata {
  id: string; // UUIDv4 / UUIDv7
  source_photo_id: string; // Google Photos MediaItem ID
  filename: string;
  file_path?: string;
  duration_seconds: number;
  width: number;
  height: number;
  recorded_at_utc: string; // ISO 8601 UTC
  recorded_at_local?: string;
  timezone_offset_minutes?: number;
  latitude?: number;
  longitude?: number;
  altitude_meters?: number;
  device_make?: string;
  device_model?: string;
  is_concert_candidate: boolean;
  concert_confidence_score: number;
  processing_status: VideoStatus;
}

export interface VenueInfo {
  id: string;
  name: string;
  formatted_address?: string;
  city: string;
  state_province?: string;
  country_code: string;
  latitude: number;
  longitude: number;
  osm_id?: string;
  setlist_fm_venue_id?: string;
}

export interface ConcertInfo {
  id: string;
  venue_id: string;
  headliner_artist: string;
  lineup_artists: string[];
  tour_name?: string;
  event_date: string; // YYYY-MM-DD
  setlist_fm_event_id?: string;
  setlist_data: {
    sets: Array<{
      song: Array<{
        name: string;
        info?: string;
        with?: { name: string };
        cover?: { name: string };
      }>;
      encore?: number;
    }>;
  };
  source_url?: string;
}

export interface SongIdentification {
  id: string;
  video_id: string;
  concert_id?: string;
  detected_song_title: string;
  detected_artist: string;
  album_name?: string;
  match_confidence: number; // 0.0 - 1.0
  fingerprint_service: 'shazamio' | 'gemini_multimodal' | 'audd' | 'manual';
  setlist_verified: boolean;
  track_number_in_setlist?: number;
  identified_lyrics_snippet?: string;
  raw_service_payload?: Record<string, unknown>;
  status: 'CONFIRMED' | 'UNCONFIRMED' | 'MANUAL_OVERRIDE';
}

export interface YouTubeUploadJob {
  id: string;
  video_id: string;
  youtube_video_id?: string;
  youtube_url?: string;
  title: string;
  description: string;
  tags: string[];
  category_id: string; // "10" = Music
  privacy_status: PrivacyStatus;
  upload_status: 'DRAFT' | 'UPLOADING' | 'COMPLETED' | 'FAILED';
  bytes_uploaded: number;
  total_bytes: number;
  uploaded_at?: string;
  error_message?: string;
}
```

---

## 2. Service Endpoints & Integration Matrix

| Integration | Base URL / SDK | Method / Operation | Purpose |
| :--- | :--- | :--- | :--- |
| **Google Photos API** | `https://photoslibrary.googleapis.com/v1` | `POST /mediaItems:search` with `filters.mediaTypeFilter.mediaTypes = ["VIDEO"]` | Retrieve video assets with `baseUrl` |
| **Google Photos Video Stream** | `{baseUrl}=dv` | `GET` with Bearer Token | Download original video container preserving ISO 6709 EXIF |
| **FFprobe** | Local CLI `/usr/bin/ffprobe` | `ffprobe -v quiet -print_format json -show_format -show_streams {file}` | Extract `com.apple.quicktime.location.ISO6709` and creation timestamps |
| **Nominatim / OSM** | `https://nominatim.openstreetmap.org` | `GET /reverse?format=jsonv2&lat={lat}&lon={lon}` | Reverse geocode coordinates to Venue / POI / City |
| **Setlist.fm API v2** | `https://api.setlist.fm/rest/1.0` | `GET /search/setlists?date={DD-MM-YYYY}&lat={lat}&long={lng}&radius=5` | Get headliner, venue, lineup, and exact setlist songs |
| **Shazamio** | Python library `shazamio` | `Shazam().recognize_song(audio_path)` | Primary acoustic fingerprint match |
| **Gemini 2.5 Flash Audio** | Google GenAI SDK | `gemini-2.5-flash.generate_content([audio_part, prompt])` | Secondary setlist-constrained live song identification |
| **YouTube Data API v3** | `https://www.googleapis.com/upload/youtube/v3/videos` | `POST ?uploadType=resumable&part=snippet,status,recordingDetails` | Resumable chunked (8MB) video publication |

---

## 3. Subagent Responsibility Matrix

| Subagent | Role | Target Implementation Files | Key Verification Metric |
| :--- | :--- | :--- | :--- |
| `fullstack-craftsman` (Ingestion) | Build DB models & Google Photos sync | `stagesync/database/*`, `stagesync/ingestion/*` | Unit tests verify ISO 6709 coordinate parsing from QuickTime headers |
| `fullstack-craftsman` (Intelligence) | Setlist.fm & Audio Fingerprinting | `stagesync/intelligence/*` | Mocked audio identifies candidate from setlist with >90% confidence |
| `fullstack-craftsman` (Publisher & CLI) | YouTube chunk uploader & Rich review TUI | `stagesync/publisher/*`, `stagesync/cli.py` | CLI dry-run prints formatted review card with all fields populated |
| `quality-guardian` | Test suite, CI runner & Anti-slop audit | `tests/*`, `scripts/check-anti-slop.js` | 100% test pass rate with zero `TODO` placeholders in codebase |
