"""YouTube Video Metadata Generation Engine.

Formats professional titles, descriptive markdown notes, setlist context,
and SEO tags conforming to YouTube Data API v3 length and formatting invariants.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import List, Optional
from dateutil import parser as dt_parser
from pydantic import BaseModel, Field

from stagesync.database.models import Concert, Identification, Venue, Video

logger = logging.getLogger(__name__)


class FormattedYouTubeMetadata(BaseModel):
    """Complete YouTube publication metadata bundle."""

    title: str
    description: str
    tags: List[str] = Field(default_factory=list)
    category_id: str = "10"  # Music
    recording_date: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


def sanitize_hashtag(name: str) -> str:
    """Strip spaces and special characters for valid YouTube hashtag."""
    return re.sub(r"[^\w]", "", name)


class MetadataGenerator:
    """Formats titles, descriptions, and tags adhering strictly to YouTube limits."""

    MAX_TITLE_LENGTH = 100
    MAX_TAGS_LENGTH = 500

    def format_title(
        self,
        artist: str,
        song_title: Optional[str],
        venue_name: Optional[str],
        city: Optional[str],
        event_date_str: Optional[str],
        clip_index: int = 1,
    ) -> str:
        """Format YouTube video title ensuring length <= 100 characters.

        Format identified:
          '{Artist} - {Song Title} (Live at {Venue}, {City} - {Date:MMM D, YYYY})'
        Format unidentified:
          '{Artist} - Live at {Venue}, {City} ({Date:MMM D, YYYY}) [Clip #{Index}]'
        """
        artist_clean = artist.strip() if artist else "Live Music"

        # Format date nicely
        formatted_date = ""
        short_date = ""
        if event_date_str:
            try:
                dt = dt_parser.parse(event_date_str)
                formatted_date = dt.strftime("%b %-d, %Y")
                short_date = dt.strftime("%Y")
            except Exception:
                formatted_date = event_date_str
                short_date = event_date_str[:4] if len(event_date_str) >= 4 else ""

        loc_parts = []
        if venue_name:
            loc_parts.append(venue_name.strip())
        if city:
            loc_parts.append(city.strip())
        loc_str = ", ".join(loc_parts) if loc_parts else "Concert"

        if song_title and song_title.strip():
            song_clean = song_title.strip()
            # Standard candidate
            if formatted_date:
                title = f"{artist_clean} - {song_clean} (Live at {loc_str} - {formatted_date})"
            else:
                title = f"{artist_clean} - {song_clean} (Live at {loc_str})"

            if len(title) <= self.MAX_TITLE_LENGTH:
                return title

            # Shorten venue if too long
            short_loc = venue_name or city or "Live"
            title = f"{artist_clean} - {song_clean} (Live at {short_loc} - {formatted_date})"
            if len(title) <= self.MAX_TITLE_LENGTH:
                return title

            # Shorten date
            title = f"{artist_clean} - {song_clean} (Live at {short_loc} - {short_date})"
            if len(title) <= self.MAX_TITLE_LENGTH:
                return title

            # Truncate song and/or artist if extremely long
            suffix = f" (Live - {short_date})" if short_date else " (Live)"
            prefix = f"{artist_clean} - "
            available_for_song = self.MAX_TITLE_LENGTH - len(prefix) - len(suffix)
            if available_for_song > 5:
                truncated_song = song_clean[: available_for_song - 3] + "..."
                return f"{prefix}{truncated_song}{suffix}"

            # If even artist + suffix exceeds max length, hard truncate
            full = f"{prefix}{song_clean}{suffix}"
            return full[: self.MAX_TITLE_LENGTH]

        # Unidentified track
        date_part = f" ({formatted_date})" if formatted_date else ""
        title = f"{artist_clean} - Live at {loc_str}{date_part} [Clip #{clip_index}]"
        if len(title) <= self.MAX_TITLE_LENGTH:
            return title

        short_loc = venue_name or city or "Concert"
        title = f"{artist_clean} - Live at {short_loc}{date_part} [#{clip_index}]"
        if len(title) <= self.MAX_TITLE_LENGTH:
            return title

        return title[: self.MAX_TITLE_LENGTH]

    def format_description(
        self,
        video: Video,
        identification: Optional[Identification],
        concert: Optional[Concert],
        venue: Optional[Venue],
    ) -> str:
        """Generate rich YouTube description per PRD specifications."""
        artist = "Unknown Artist"
        song_title = "Live Performance"
        if identification and identification.detected_song_title:
            song_title = identification.detected_song_title
        if identification and identification.detected_artist:
            artist = identification.detected_artist
        elif concert and concert.headliner_artist:
            artist = concert.headliner_artist

        lines = [f'Live performance of "{song_title}" by {artist}.', ""]

        # Venue info
        venue_name = venue.name if venue else (concert.venue_id if concert else "Live Concert")
        city_line = []
        if venue:
            if venue.name:
                city_line.append(venue.name)
            if venue.city:
                city_line.append(venue.city)
            if venue.state_province:
                city_line.append(venue.state_province)
            elif venue.country_code:
                city_line.append(venue.country_code)
        if city_line:
            lines.append(f"📍 Venue: {', '.join(city_line)}")

        # Date info
        date_str = ""
        if concert and concert.event_date:
            date_str = concert.event_date
        elif video.recorded_at_local:
            date_str = video.recorded_at_local[:10]
        elif video.recorded_at_utc:
            date_str = video.recorded_at_utc[:10]

        if date_str:
            try:
                dt = dt_parser.parse(date_str)
                lines.append(f"🗓️ Date: {dt.strftime('%A, %B %-d, %Y')}")
            except Exception:
                lines.append(f"🗓️ Date: {date_str}")

        # Tour
        if concert and concert.tour_name:
            lines.append(f"🎸 Tour: {concert.tour_name}")

        # Setlist Context
        if identification and identification.track_number_in_setlist:
            total_tracks = ""
            if concert and concert.setlist_data:
                songs = concert.setlist_data.get("sets", {}).get("set", [])
                total_cnt = sum(len(s.get("song", [])) for s in songs)
                if total_cnt > 0:
                    total_tracks = f" of {total_cnt}"
            lines.append(f"🎵 Setlist Context: Track #{identification.track_number_in_setlist}{total_tracks}")

        # Device
        device_parts = []
        if video.device_make:
            device_parts.append(video.device_make)
        if video.device_model:
            device_parts.append(video.device_model)
        if device_parts:
            lines.append(f"\nRecorded on {' '.join(device_parts)}.")

        # Setlist.fm URL
        if concert and concert.source_url:
            lines.append(f"Concert details verified via Setlist.fm: {concert.source_url}")

        # Setlist Lineup if available
        if concert and concert.setlist_data:
            sets = concert.setlist_data.get("sets", {}).get("set", [])
            tracklist = []
            for s in sets:
                for song_item in s.get("song", []):
                    s_name = song_item.get("name")
                    if s_name:
                        tracklist.append(s_name)
            if tracklist:
                lines.append("\nSetlist:")
                for idx, t in enumerate(tracklist[:25]):  # Up to 25 songs
                    marker = " ◄" if (song_title and song_title.lower() == t.lower()) else ""
                    lines.append(f"  {idx + 1}. {t}{marker}")

        # Hashtags
        hashtags = ["#LiveMusic", "#Concert"]
        if artist and artist != "Unknown Artist":
            hashtags.append(f"#{sanitize_hashtag(artist)}")
        if venue and venue.name:
            hashtags.append(f"#{sanitize_hashtag(venue.name)}")
        if venue and venue.city:
            hashtags.append(f"#{sanitize_hashtag(venue.city)}")

        lines.append("\n" + " ".join(hashtags))
        return "\n".join(lines).strip()

    def generate_tags(
        self,
        artist: str,
        song_title: Optional[str],
        venue_name: Optional[str],
        city: Optional[str],
        event_date_str: Optional[str],
    ) -> List[str]:
        """Generate deduplicated list of YouTube tags fitting within 500 characters."""
        raw_tags = [
            artist,
            "Live",
            "Concert",
            "Live Music",
        ]
        if song_title:
            raw_tags.append(song_title)
            raw_tags.append(f"{artist} {song_title}")
        if venue_name:
            raw_tags.append(venue_name)
        if city:
            raw_tags.append(city)
        if event_date_str:
            try:
                year = str(dt_parser.parse(event_date_str).year)
                raw_tags.append(year)
                raw_tags.append(f"{artist} {year}")
            except Exception as err:
                logger.debug("Failed parsing event_date_str for year tag: %s", err)

        # Deduplicate while preserving order
        seen = set()
        unique_tags = []
        total_len = 0

        for t in raw_tags:
            cleaned = t.strip()
            if cleaned and cleaned.lower() not in seen:
                # Tags string length in YouTube is comma-separated
                if total_len + len(cleaned) + 1 <= self.MAX_TAGS_LENGTH:
                    seen.add(cleaned.lower())
                    unique_tags.append(cleaned)
                    total_len += len(cleaned) + 1

        return unique_tags

    def generate_metadata(
        self,
        video: Video,
        identification: Optional[Identification] = None,
        concert: Optional[Concert] = None,
        venue: Optional[Venue] = None,
        clip_index: int = 1,
    ) -> FormattedYouTubeMetadata:
        """Create complete formatted YouTube metadata for a video entity."""
        artist = ""
        song_title = None

        if identification:
            artist = identification.detected_artist
            song_title = identification.detected_song_title
        if not artist and concert:
            artist = concert.headliner_artist
        if not artist:
            artist = "Live Concert"

        venue_name = venue.name if venue else (concert.venue_id if concert else None)
        city = venue.city if venue else None
        event_date = concert.event_date if concert else video.recorded_at_utc

        title = self.format_title(
            artist=artist,
            song_title=song_title,
            venue_name=venue_name,
            city=city,
            event_date_str=event_date,
            clip_index=clip_index,
        )

        description = self.format_description(video, identification, concert, venue)
        tags = self.generate_tags(artist, song_title, venue_name, city, event_date)

        return FormattedYouTubeMetadata(
            title=title,
            description=description,
            tags=tags,
            category_id="10",
            recording_date=video.recorded_at_utc or video.recorded_at_local,
            latitude=video.latitude,
            longitude=video.longitude,
        )
