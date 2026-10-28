"""StageSync Rich Command-Line Interface.

Provides CLI commands for:
  - scan: Discover videos from Google Photos or local directory
  - process: Run intelligence pipeline on cataloged candidates
  - review: Interactive TUI review queue for approving/editing live videos
  - upload: Chunked resumable YouTube publication
  - status: System health, database records, and quota telemetry
  - auth: Manage Google Photos and YouTube OAuth tokens
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from stagesync.config import get_settings
from stagesync.database.connection import Database, get_db
from stagesync.database.models import PrivacyStatus, VideoStatus
from stagesync.ingestion.google_drive import extract_folder_id
from stagesync.ingestion.google_photos import GooglePhotosClient
from stagesync.pipeline import PipelineCoordinator
from stagesync.publisher.youtube_auth import YouTubeAuth

console = Console()


def format_duration(seconds: Optional[float]) -> str:
    """Format duration in MM:SS or HH:MM:SS."""
    if not seconds:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_file_size(size_bytes: Optional[int]) -> str:
    """Format byte size into human readable string."""
    if not size_bytes:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def cmd_status(args: argparse.Namespace) -> None:
    """Display system status, database statistics, quotas, and credentials."""
    settings = get_settings()
    db = get_db()
    photos_client = GooglePhotosClient()
    yt_auth = YouTubeAuth()

    console.print()
    console.print(
        Panel(
            "[bold cyan]StageSync (ConcertVault) - System & Pipeline Status[/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )

    # 1. Database Statistics Table
    db_table = Table(title="Database Inventory", box=box.SIMPLE_HEAVY)
    db_table.add_column("Status Category", style="cyan", no_wrap=True)
    db_table.add_column("Count", justify="right", style="bold green")

    all_videos = db.list_videos(limit=1000)
    status_counts = {}
    for v in all_videos:
        st = v.processing_status.value
        status_counts[st] = status_counts.get(st, 0) + 1

    db_table.add_row("Total Cataloged Videos", str(len(all_videos)))
    for st_enum in VideoStatus:
        cnt = status_counts.get(st_enum.value, 0)
        db_table.add_row(f"  • {st_enum.value}", str(cnt))

    console.print(db_table)
    console.print()

    # 2. Integration & Quota Table
    quota_table = Table(title="Integrations & Quota Status", box=box.SIMPLE_HEAVY)
    quota_table.add_column("Service", style="yellow")
    quota_table.add_column("Auth Status", style="bold")
    quota_table.add_column("Quota / Limits", style="white")

    # Google Photos
    gp_auth = "[green]Authenticated[/green]" if photos_client.is_authenticated() else "[red]Not Configured[/red]"
    quota_table.add_row("Google Photos Library API", gp_auth, "10,000 read requests / day")

    # Google Drive
    from stagesync.ingestion.google_drive import GoogleDriveClient
    drive_client = GoogleDriveClient()
    gd_auth = "[green]Authenticated[/green]" if drive_client.is_authenticated() else "[red]Not Configured[/red]"
    quota_table.add_row("Google Drive API v3", gd_auth, "1,000,000 queries / day (Full video metadata & files.list)")

    # YouTube Data API
    yt_status = "[green]Authenticated[/green]" if yt_auth.is_authenticated() else "[red]Not Configured[/red]"
    uploads_today = db.get_todays_upload_count()
    quota_table.add_row(
        "YouTube Data API v3",
        yt_status,
        f"{uploads_today}/{settings.max_daily_uploads} videos uploaded today (Safety guardrail)",
    )

    # Setlist.fm
    sfm_status = "[green]API Key Configured[/green]" if settings.setlist_fm_api_key else "[yellow]Missing Key[/yellow]"
    quota_table.add_row("Setlist.fm API v2", sfm_status, "2 requests / second limit (Token bucket active)")

    # Gemini Flash Audio
    gem_status = "[green]API Key Configured[/green]" if settings.gemini_api_key else "[yellow]Missing Key[/yellow]"
    quota_table.add_row("Gemini 2.5 Flash Audio", gem_status, "Setlist-constrained live reasoning")

    console.print(quota_table)
    console.print()


def cmd_scan(args: argparse.Namespace) -> None:
    """Discover videos from Google Photos or local directory."""
    coordinator = PipelineCoordinator()

    if getattr(args, "dir", None):
        dir_path = Path(args.dir)
        console.print(f"[bold cyan]Scanning local directory:[/bold cyan] {dir_path}")
        with console.status("[bold green]Ingesting local media files..."):
            videos = coordinator.scan_local_directory(dir_path)
        console.print(f"[bold green]Discovered {len(videos)} video files.[/bold green]")
        if videos:
            table = Table(title=f"Discovered Local Videos ({len(videos)})", box=box.ROUNDED)
            table.add_column("ID", style="dim", max_width=12)
            table.add_column("Filename", style="cyan")
            table.add_column("Size", justify="right")
            table.add_column("Status", style="bold green")
            for v in videos:
                sz = format_file_size(v.file_size_bytes)
                table.add_row(v.id[:8] + "...", v.filename, sz, v.processing_status.value)
            console.print(table)
        return

    if getattr(args, "picker", False):
        console.print("[bold cyan]Creating Google Photos Picker session...[/bold cyan]")
        try:
            session = coordinator.photos_client.create_picker_session()
            picker_uri = session["pickerUri"]
            session_id = session["id"]
            console.print("\n[bold green]Please open this URL to select your concert videos:[/bold green]")
            console.print(f"[link={picker_uri}]{picker_uri}[/link]\n")
            console.print("Waiting for photo selection to complete in browser (Ctrl+C to abort)...")
            import time
            while True:
                if coordinator.photos_client.poll_picker_session(session_id):
                    console.print("[bold green]✓ Selection confirmed by Google Photos![/bold green]")
                    break
                time.sleep(4)
            items = coordinator.photos_client.get_picker_items(session_id)
            videos = coordinator.ingest_picker_items(items, dry_run=args.dry_run)
            coordinator.photos_client.delete_picker_session(session_id)
            if not videos:
                console.print("[yellow]No new videos ingested from picker session.[/yellow]")
                return
            table = Table(title=f"Picker Results ({len(videos)} candidates)", box=box.ROUNDED)
            table.add_column("ID", style="dim", max_width=12)
            table.add_column("Filename", style="cyan")
            table.add_column("Dimensions", justify="center")
            table.add_column("Status", style="bold green")
            for v in videos:
                dim = f"{v.width}x{v.height}" if v.width and v.height else "N/A"
                table.add_row(v.id[:8] + "...", v.filename, dim, v.processing_status.value)
            console.print(table)
        except Exception as e:
            console.print(f"\n[bold red]✗ Picker Error:[/bold red] {e}")
        return

    if getattr(args, "drive", False):
        clean_folder = extract_folder_id(getattr(args, "drive_folder", None))
        date_info = []
        if args.start_date:
            date_info.append(f"from {args.start_date}")
        if args.end_date:
            date_info.append(f"to {args.end_date}")
        if not date_info and args.days:
            date_info.append(f"past {args.days} days")
        folder_desc = f" in folder {clean_folder}" if clean_folder else ""
        date_desc = " ".join(date_info) if date_info else "all dates"

        console.print(f"[bold cyan]Scanning Google Drive ({date_desc}{folder_desc})...[/bold cyan]")
        if args.dry_run:
            console.print("[yellow]Running in DRY-RUN mode. Videos will be cataloged without downloading binaries.[/yellow]")

        try:
            with console.status("[bold green]Querying Google Drive API..."):
                videos = coordinator.scan_google_drive(
                    start_date=args.start_date,
                    end_date=args.end_date,
                    days=args.days if (not args.start_date and not args.end_date) else None,
                    folder_id=clean_folder,
                    dry_run=args.dry_run,
                    limit=args.limit,
                )
        except Exception as e:
            console.print(f"\n[bold red]✗ Google Drive Query Failed:[/bold red] {e}\n")
            if "unauthenticated" in str(e).lower() or "permissionerror" in str(e).lower():
                console.print("[yellow]Run 'stagesync auth google-drive' to authenticate with Google Drive.[/yellow]\n")
            return

        if not videos:
            console.print("[yellow]No new videos found on Google Drive for the specified criteria.[/yellow]")
            return

        table = Table(title=f"Google Drive Results ({len(videos)} candidates)", box=box.ROUNDED)
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Filename", style="cyan")
        table.add_column("Size", justify="right")
        table.add_column("Duration", justify="center")
        table.add_column("Status", style="bold green")

        for v in videos:
            sz = format_file_size(v.file_size_bytes)
            dur = format_duration(v.duration_seconds)
            table.add_row(v.id[:8] + "...", v.filename, sz, dur, v.processing_status.value)

        console.print(table)
        return

    # Google Photos Background Scan
    date_info = []
    if args.start_date:
        date_info.append(f"from {args.start_date}")
    if args.end_date:
        date_info.append(f"to {args.end_date}")
    if not date_info:
        days_val = args.days if args.days is not None else 30
        date_info.append(f"past {days_val} days")
    date_desc = " ".join(date_info)

    console.print(f"[bold cyan]Scanning Google Photos ({date_desc})...[/bold cyan]")
    if args.dry_run:
        console.print("[yellow]Running in DRY-RUN mode. Videos will be cataloged without downloading binaries.[/yellow]")

    try:
        with console.status("[bold green]Querying Google Photos Library API..."):
            videos = coordinator.scan_google_photos(
                start_date=args.start_date,
                end_date=args.end_date,
                days=args.days if (not args.start_date and not args.end_date) else None,
                album_id=args.album,
                dry_run=args.dry_run,
                limit=args.limit,
            )
    except Exception as e:
        console.print(f"\n[bold red]✗ Google Photos Query Failed:[/bold red] {e}\n")
        err_str = str(e).lower()
        if "403" in err_str or "insufficient" in err_str or "scope" in err_str:
            console.print(
                Panel(
                    "[bold yellow]Google Photos Scope Deprecation Notice (2025/2026)[/bold yellow]\n\n"
                    "Google has restricted broad automated background scanning via `photoslibrary.readonly` (HTTP 403).\n\n"
                    "[bold green]Immediate Recommended Options:[/bold green]\n"
                    "1. Scan a local directory of downloaded/synced concert videos:\n"
                    "   [bold cyan]stagesync scan --dir /path/to/concert_videos[/bold cyan]\n\n"
                    "2. Select videos via Google Photos Picker:\n"
                    "   [bold cyan]stagesync scan --picker[/bold cyan]\n\n"
                    "3. Open Web Dashboard at [cyan]http://localhost:8765[/cyan] to use Local Directory or Picker tabs.",
                    border_style="yellow",
                    box=box.ROUNDED,
                )
            )
        return

    if not videos:
        console.print("[yellow]No new videos found for the specified criteria.[/yellow]")
        return

    table = Table(title=f"Scan Results ({len(videos)} candidates)", box=box.ROUNDED)
    table.add_column("ID", style="dim", max_width=12)
    table.add_column("Filename", style="cyan")
    table.add_column("Dimensions", justify="center")
    table.add_column("Status", style="bold green")

    for v in videos:
        dim = f"{v.width}x{v.height}" if v.width and v.height else "N/A"
        table.add_row(v.id[:8] + "...", v.filename, dim, v.processing_status.value)

    console.print(table)


def cmd_process(args: argparse.Namespace) -> None:
    """Run intelligence pipeline on newly cataloged video assets."""
    coordinator = PipelineCoordinator()
    reproc_desc = " [reprocess mode]" if getattr(args, "reprocess", False) else ""
    status_desc = f" [status={args.status}]" if getattr(args, "status", None) else ""
    console.print(f"[bold cyan]Processing pending videos (Limit: {args.limit}){status_desc}{reproc_desc}...[/bold cyan]")

    with console.status("[bold green]Running metadata extraction & concert intelligence..."):
        processed = coordinator.process_pending_videos(
            limit=args.limit,
            status=getattr(args, "status", None),
            reprocess=getattr(args, "reprocess", False),
        )

    if not processed:
        console.print("[yellow]No pending videos found to process.[/yellow]")
        return

    table = Table(title=f"Processed Candidates ({len(processed)})", box=box.ROUNDED)
    table.add_column("Filename", style="cyan")
    table.add_column("Duration", justify="center")
    table.add_column("Location", style="yellow")
    table.add_column("Candidate Score", justify="right")
    table.add_column("Final Status", style="bold")

    for v in processed:
        loc = f"{v.latitude:.2f}, {v.longitude:.2f}" if v.latitude and v.longitude else "No GPS"
        dur = format_duration(v.duration_seconds)
        st_style = "green" if v.processing_status == VideoStatus.APPROVED else ("yellow" if v.processing_status == VideoStatus.PENDING_REVIEW else "red")
        table.add_row(
            v.filename,
            dur,
            loc,
            f"{v.concert_confidence_score:.2f}",
            f"[{st_style}]{v.processing_status.value}[/{st_style}]",
        )

    console.print(table)


def cmd_review(args: argparse.Namespace) -> None:
    """Interactive TUI Review Queue."""
    db = get_db()
    coordinator = PipelineCoordinator()
    queue = db.get_review_queue()

    if not queue:
        console.print("[bold green]Review queue is empty! No videos pending review.[/bold green]")
        return

    console.print(f"[bold cyan]Found {len(queue)} candidate video(s) pending review.[/bold cyan]\n")

    # Non-interactive mode fallback
    if getattr(args, "non_interactive", False) or not sys.stdin.isatty():
        table = Table(title="Pending Review Queue", box=box.ROUNDED)
        table.add_column("Video ID", style="dim")
        table.add_column("Filename", style="cyan")
        table.add_column("Artist", style="yellow")
        table.add_column("Song", style="bold green")
        table.add_column("Confidence", justify="right")

        for vid, ident, concert, venue in queue:
            artist = (ident.detected_artist if ident else (concert.headliner_artist if concert else "Unknown"))
            song = (ident.detected_song_title if ident else "Live Performance")
            conf = f"{ident.match_confidence * 100:.1f}%" if ident else "0.0%"
            table.add_row(vid.id[:8], vid.filename, artist, song, conf)

        console.print(table)
        return

    # Interactive Review TUI
    for idx, (vid, ident, concert, venue) in enumerate(queue):
        console.clear()
        console.print("=" * 80)
        console.print(f"[bold cyan]StageSync Review [Candidate {idx + 1} of {len(queue)}][/bold cyan]")
        console.print("=" * 80)

        # File & Temporal Details
        file_info = f"{vid.filename} (Duration: {format_duration(vid.duration_seconds)} | Size: {format_file_size(vid.file_size_bytes)})"
        rec_time = vid.recorded_at_local or vid.recorded_at_utc or "Unknown Time"
        loc_str = "Unknown Location"
        if venue:
            loc_str = f"{venue.name} ({venue.city}, {venue.state_province or venue.country_code})"
            if vid.latitude and vid.longitude:
                loc_str += f"\n              [{vid.latitude:.4f}° N, {vid.longitude:.4f}° W]"
        elif vid.latitude and vid.longitude:
            loc_str = f"[{vid.latitude:.4f}° N, {vid.longitude:.4f}° W]"

        console.print(f"File:         [bold white]{file_info}[/bold white]")
        console.print(f"Recorded At:  [bold white]{rec_time}[/bold white]")
        console.print(f"Location:     [bold yellow]{loc_str}[/bold yellow]\n")

        # Matched Show
        if concert:
            console.print("[bold green]MATCHED SHOW:[/bold green]")
            console.print(f"  Artist:     [bold]{concert.headliner_artist}[/bold]")
            if concert.tour_name:
                console.print(f"  Tour:       {concert.tour_name}")
            if concert.lineup_artists:
                console.print(f"  Lineup:     {', '.join(concert.lineup_artists)}")
            if concert.source_url:
                console.print(f"  Setlist.fm: [link={concert.source_url}]{concert.source_url}[/link]")
            console.print()
        else:
            console.print("[yellow]MATCHED SHOW: None correlated[/yellow]\n")

        # Identified Track
        if ident:
            console.print("[bold green]IDENTIFIED TRACK:[/bold green]")
            tr_num = f" (Track #{ident.track_number_in_setlist})" if ident.track_number_in_setlist else ""
            console.print(f"  Song:       [bold]{ident.detected_song_title}{tr_num}[/bold]")
            console.print(f"  Service:    {ident.fingerprint_service} (Confidence: {ident.match_confidence * 100:.1f}%)")
            v_text = "Verified against Setlist.fm roster" if ident.setlist_verified else "Acoustic match only"
            console.print(f"  Setlist:    {v_text}")
            if ident.identified_lyrics_snippet:
                console.print(f"  Lyrics:     \"{ident.identified_lyrics_snippet}\"")
            console.print()
        else:
            console.print("[yellow]IDENTIFIED TRACK: None[/yellow]\n")

        # YouTube Draft Metadata
        upload_job = db.get_youtube_upload_by_video_id(vid.id)
        if upload_job:
            console.print("[bold green]TARGET YOUTUBE METADATA:[/bold green]")
            console.print(f"  Title:      [bold cyan]{upload_job.title}[/bold cyan]")
            console.print(f"  Privacy:    [bold magenta]{upload_job.privacy_status.value}[/bold magenta]")
        console.print("-" * 80)

        # Action Prompt
        song_title = (ident.detected_song_title if ident else "").strip().lower()
        is_unconfirmed_song = not song_title or song_title in ["live performance", "unidentified clip", "unidentified song", "unknown"] or (ident and ident.match_confidence < 0.50)
        default_choice = "b" if (concert and is_unconfirmed_song) else "a"

        if concert and is_unconfirmed_song:
            console.print("[bold yellow]🎸 LINEUP ACTION: Concert correlated, but performing band & song are unconfirmed.[/bold yellow]")
            console.print("[bold yellow]   Press [B] to select who performed in this clip from the show's lineup roster.[/bold yellow]\n")

        console.print("[bold white][A][/bold white] Approve & Queue Upload   [bold white][B][/bold white] Which band is this?   [bold white][V][/bold white] Set/Search Venue   [bold white][E][/bold white] Edit Metadata   [bold white][P][/bold white] Change Privacy   [bold white][R][/bold white] Reject   [bold white][S][/bold white] Skip")
        choice = Prompt.ask("Selection", choices=["a", "b", "v", "e", "p", "r", "s", "A", "B", "V", "E", "P", "R", "S"], default=default_choice).lower()

        if choice == "a":
            coordinator.approve_video(vid.id)
            console.print("[bold green]✓ Approved for upload.[/bold green]\n")
        elif choice == "b":
            if not concert:
                console.print("[yellow]No concert associated with this video yet. Use [V] to set venue and find concert first.[/yellow]\n")
                continue

            # Gather lineup artists
            bands = list(concert.lineup_artists or [])
            if concert.headliner_artist and concert.headliner_artist not in bands:
                bands.insert(0, concert.headliner_artist)

            if not bands:
                console.print(f"[yellow]No lineup found for this show ({concert.headliner_artist}). Enter band name manually:[/yellow]")
                chosen_band = Prompt.ask("Performing Band Name")
            else:
                table = Table(title=f"Lineup for {concert.headliner_artist} ({concert.event_date or 'Show'})", box=box.ROUNDED)
                table.add_column("#", style="bold cyan", justify="right")
                table.add_column("Band / Artist", style="bold white")
                table.add_column("Role", style="yellow")

                for b_idx, band_name in enumerate(bands, start=1):
                    role = "Headliner" if (concert.headliner_artist and band_name.lower() == concert.headliner_artist.lower()) else "Lineup / Support"
                    table.add_row(str(b_idx), band_name, role)

                table.add_row("C", "Custom / Unlisted Opener", "Manual Entry")
                console.print(table)

                valid_band_choices = [str(i) for i in range(1, len(bands) + 1)] + ["c", "C", "0"]
                b_choice = Prompt.ask(
                    "Which band is this? Select number, 'C' for custom, or 0 to cancel",
                    choices=valid_band_choices,
                    default="1",
                )
                if b_choice == "0":
                    console.print("[dim]Cancelled band selection.[/dim]\n")
                    continue

                if b_choice.lower() == "c":
                    chosen_band = Prompt.ask("Enter performing band name")
                else:
                    chosen_band = bands[int(b_choice) - 1]

            with console.status(f"[bold green]Assigning band '{chosen_band}' and updating YouTube draft metadata...[/bold green]"):
                up_vid, up_concert, up_ident = coordinator.reidentify_video_with_band(
                    video_id=vid.id,
                    artist_name=chosen_band,
                )
            console.print(f"[bold green]✓ Band set to:[/bold green] [bold cyan]{chosen_band}[/bold cyan]")
            upload_job = db.get_youtube_upload_by_video_id(vid.id)
            if upload_job:
                console.print(f"  Target YouTube Title: [bold cyan]{upload_job.title}[/bold cyan]")
            console.print("[dim]Candidate updated. You can approve it now or inspect further.[/dim]\n")
        elif choice == "v":
            cur_venue = venue.name if venue else ""
            v_query = Prompt.ask("Search venue name (e.g. 'Come and Take It Live')", default=cur_venue if venue else "")
            city_hint = venue.city if venue else None
            with console.status(f"[bold cyan]Searching venues for '{v_query}' across DB, Setlist.fm & OSM...[/bold cyan]"):
                candidates = coordinator.venue_search_engine.search(query=v_query, city=city_hint, limit=10)

            chosen_cand = None
            if not candidates:
                console.print(f"[yellow]No online matches found for '{v_query}'. Enter manual details:[/yellow]")
                man_name = Prompt.ask("Venue Name", default=v_query)
                man_city = Prompt.ask("City", default=venue.city if venue else "Austin")
                man_state = Prompt.ask("State / Province (e.g. TX)", default=venue.state_province if venue else "TX")
                chosen_cand = {
                    "name": man_name,
                    "city": man_city,
                    "state": man_state,
                    "country": "US",
                    "formatted_address": f"{man_name}, {man_city}, {man_state}",
                }
            else:
                table = Table(title=f"Venues Matching '{v_query}'", box=box.ROUNDED)
                table.add_column("#", style="bold cyan", justify="right")
                table.add_column("Venue Name", style="bold white")
                table.add_column("Location", style="yellow")
                table.add_column("Sources", style="dim")

                for c_idx, cand in enumerate(candidates, start=1):
                    sources_str = ", ".join(cand.get("sources", ["manual"]))
                    loc_desc = f"{cand.get('city', '')}, {cand.get('state') or cand.get('country', '')}"
                    table.add_row(str(c_idx), cand.get("name", "Unknown"), loc_desc, sources_str)

                console.print(table)
                choice_idx = Prompt.ask(
                    "Select venue number (or 0 to cancel)",
                    choices=[str(i) for i in range(len(candidates) + 1)],
                    default="1",
                )
                if choice_idx == "0":
                    console.print("[dim]Cancelled venue update.[/dim]\n")
                    continue
                chosen_cand = candidates[int(choice_idx) - 1]

            with console.status("[bold green]Associating venue and re-searching Setlist.fm concert...[/bold green]"):
                up_vid, up_concert, up_ident = coordinator.reidentify_video_with_venue(
                    video_id=vid.id,
                    venue_name=chosen_cand.get("name", ""),
                    city=chosen_cand.get("city"),
                    state_province=chosen_cand.get("state"),
                    country_code=chosen_cand.get("country", "US"),
                    latitude=chosen_cand.get("latitude"),
                    longitude=chosen_cand.get("longitude"),
                    setlist_fm_venue_id=chosen_cand.get("setlist_fm_venue_id"),
                    osm_id=chosen_cand.get("osm_id"),
                    formatted_address=chosen_cand.get("formatted_address"),
                )

            if up_concert:
                console.print(f"[bold green]✓ Matched Concert:[/bold green] [bold cyan]{up_concert.headliner_artist}[/bold cyan] at [bold yellow]{chosen_cand.get('name')}[/bold yellow] ({up_concert.event_date})")
                if up_ident and up_ident.detected_song_title:
                    console.print(f"  Track: [bold]{up_ident.detected_song_title}[/bold] (Confidence: {up_ident.match_confidence * 100:.1f}%)")
            else:
                console.print(f"[bold green]✓ Venue updated to:[/bold green] [bold yellow]{chosen_cand.get('name')}[/bold yellow] (No setlist found on this date)")
            console.print("[dim]Candidate updated. You can approve it now or inspect further.[/dim]\n")
        elif choice == "e":
            new_title = Prompt.ask("Override Song Title", default=ident.detected_song_title if ident else "")
            new_artist = Prompt.ask("Override Artist", default=ident.detected_artist if ident else (concert.headliner_artist if concert else ""))
            coordinator.approve_video(vid.id, song_title_override=new_title, artist_override=new_artist)
            console.print("[bold green]✓ Updated & Approved.[/bold green]\n")
        elif choice == "p":
            new_priv = Prompt.ask("Select Privacy", choices=["private", "unlisted", "public"], default="unlisted")
            coordinator.approve_video(vid.id, privacy_override=PrivacyStatus(new_priv))
            console.print(f"[bold green]✓ Privacy set to {new_priv} & Approved.[/bold green]\n")
        elif choice == "r":
            coordinator.reject_video(vid.id)
            console.print("[bold red]✗ Rejected candidate.[/bold red]\n")
        elif choice == "s":
            console.print("[yellow]Skipped to next candidate.[/yellow]\n")


def cmd_upload(args: argparse.Namespace) -> None:
    """Upload approved videos using chunked resumable upload."""
    coordinator = PipelineCoordinator()
    db = get_db()
    approved = db.list_videos(status=VideoStatus.APPROVED, limit=args.limit or 10)

    if not approved:
        console.print("[bold yellow]No approved videos pending upload.[/bold yellow]")
        return

    console.print(f"[bold cyan]Found {len(approved)} approved video(s) to upload.[/bold cyan]")

    privacy_override = PrivacyStatus(args.privacy) if args.privacy else None

    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task("Uploading", total=100)

        def on_progress(bytes_sent: int, total_bytes: int):
            progress.update(task_id, completed=bytes_sent, total=total_bytes)

        results = coordinator.upload_approved_videos(
            limit=args.limit,
            privacy_override=privacy_override,
            progress_callback=on_progress,
        )

    for r in results:
        if r.success:
            console.print(f"[bold green]✓ Uploaded successfully:[/bold green] [link={r.youtube_url}]{r.youtube_url}[/link]")
        else:
            console.print(f"[bold red]✗ Upload failed:[/bold red] {r.error_message}")


def extract_auth_code(raw_input: str) -> str:
    """Extract clean OAuth authorization code if user pasted a full callback URL."""
    val = raw_input.strip()
    if "?" in val or "code=" in val:
        from urllib.parse import parse_qs, urlparse
        try:
            parsed = urlparse(val)
            qs = parse_qs(parsed.query)
            if "code" in qs and qs["code"]:
                return qs["code"][0].strip()
        except Exception:
            pass
    return val


def cmd_auth(args: argparse.Namespace) -> None:
    """Manage OAuth authentication flows."""
    target = args.service
    force = getattr(args, "force", False)

    if target == "google-photos":
        client = GooglePhotosClient()
        if client.is_authenticated() and not force:
            console.print("[green]Google Photos is already authenticated. Use --force to re-authenticate.[/green]")
            return
        redirect_uri = getattr(args, "redirect_uri", None) or getattr(client, 'google_photos_redirect_uri', None) or "http://localhost:8090/callback"
        url = client.get_authorization_url(redirect_uri)
        console.print("\n[bold cyan]Google Photos Authorization:[/bold cyan]")
        console.print(f"Open this URL in your browser:\n[link={url}]{url}[/link]\n")
        console.print("[dim]Tip: You can paste either the authorization code or the full redirected URL.[/dim]")
        raw_code = Prompt.ask("Paste the authorization code or redirected URL")
        code = extract_auth_code(raw_code)
        client.exchange_code(code, redirect_uri)
        console.print("[bold green]✓ Google Photos token successfully saved![/bold green]")

    elif target in ("google-drive", "drive"):
        from stagesync.ingestion.google_drive import GoogleDriveClient
        client = GoogleDriveClient()
        if client.is_authenticated() and not force:
            console.print("[green]Google Drive is already authenticated. Use --force to re-authenticate.[/green]")
            return
        redirect_uri = getattr(args, "redirect_uri", None) or client.redirect_uri or "http://localhost:8090/youtube_callback"
        url = client.get_authorization_url(redirect_uri)
        console.print("\n[bold cyan]Google Drive Authorization:[/bold cyan]")
        console.print(f"Open this URL in your browser:\n[link={url}]{url}[/link]\n")
        console.print(
            f"[dim]Note: Google will redirect your browser to {redirect_uri}?code=...\n"
            "If your browser shows 'This site can't be reached', simply copy the code\n"
            "OR the full URL from your browser address bar and paste it below.[/dim]\n"
        )
        raw_code = Prompt.ask("Paste the authorization code or redirected URL")
        code = extract_auth_code(raw_code)
        client.exchange_code(code, redirect_uri)
        console.print("[bold green]✓ Google Drive token successfully saved![/bold green]")

    elif target == "youtube":
        yt = YouTubeAuth()
        if yt.is_authenticated() and not force:
            console.print("[green]YouTube is already authenticated. Use --force to re-authenticate.[/green]")
            return
        redirect_uri = getattr(args, "redirect_uri", None) or getattr(yt, "redirect_uri", None) or "http://localhost:8090/youtube_callback"
        url = yt.get_authorization_url(redirect_uri)
        console.print("\n[bold cyan]YouTube Authorization:[/bold cyan]")
        console.print(f"Open this URL in your browser:\n[link={url}]{url}[/link]\n")
        console.print(
            f"[dim]Note: Google will redirect your browser to {redirect_uri}?code=...\n"
            "If your browser shows 'This site can't be reached', simply copy the code\n"
            "OR the full URL from your browser address bar and paste it below.[/dim]\n"
        )
        raw_code = Prompt.ask("Paste the authorization code or redirected URL")
        code = extract_auth_code(raw_code)
        yt.exchange_code(code, redirect_uri)
        console.print("[bold green]✓ YouTube token successfully saved![/bold green]")

    elif target == "status":
        cmd_status(args)


def cmd_daemon(args: argparse.Namespace) -> None:
    """Run continuous background watcher daemon."""
    from stagesync.daemon import StageSyncDaemon

    console.print(
        Panel(
            f"[bold cyan]StageSync Daemon[/bold cyan]\nInterval: {args.interval}m | Auto-Process: {not args.no_process} | Auto-Upload: {args.auto_upload}",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )
    daemon = StageSyncDaemon(
        interval_minutes=args.interval,
        auto_process=not args.no_process,
        auto_upload=args.auto_upload,
    )
    daemon.start(run_once=args.run_once)


def cmd_serve(args: argparse.Namespace) -> None:
    """Run local Web Dashboard."""
    from stagesync.web.server import run_server

    console.print(
        Panel(
            f"[bold cyan]StageSync Web Dashboard[/bold cyan]\nListening on http://{args.host}:{args.port}",
            border_style="blue",
            box=box.ROUNDED,
        )
    )
    run_server(host=args.host, port=args.port, open_browser=args.open_browser)


def main() -> None:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="stagesync",
        description="StageSync: Autonomous Live Concert Video Archival & YouTube Pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show system status and quotas")
    p_status.set_defaults(func=cmd_status)

    # daemon
    p_daemon = subparsers.add_parser("daemon", help="Run background watcher daemon")
    p_daemon.add_argument("--interval", type=int, default=60, help="Check interval in minutes (default: 60)")
    p_daemon.add_argument("--no-process", action="store_true", help="Only scan without running AI intelligence")
    p_daemon.add_argument("--auto-upload", action="store_true", help="Automatically upload approved videos")
    p_daemon.add_argument("--run-once", action="store_true", help="Execute single cycle and exit")
    p_daemon.set_defaults(func=cmd_daemon)

    # serve / ui / web
    p_serve = subparsers.add_parser("serve", aliases=["web"], help="Launch local Web Dashboard")
    p_serve.add_argument("--host", type=str, default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    p_serve.add_argument("--open-browser", action="store_true", help="Automatically open browser")
    p_serve.set_defaults(func=cmd_serve)

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan Google Drive, Google Photos, or local directory")
    p_scan.add_argument("--drive", action="store_true", help="Scan videos from Google Drive (Full search API)")
    p_scan.add_argument("--drive-folder", type=str, default=None, help="Google Drive folder ID filter")
    p_scan.add_argument("--start-date", type=str, default=None, help="Start date (YYYY-MM-DD) for discovery")
    p_scan.add_argument("--end-date", type=str, default=None, help="End date (YYYY-MM-DD) for discovery")
    p_scan.add_argument("--days", type=int, default=None, help="Days back to scan (e.g. 30)")
    p_scan.add_argument("--album", type=str, default=None, help="Google Photos Album ID filter")
    p_scan.add_argument("--dir", type=str, default=None, help="Local directory path to scan")
    p_scan.add_argument("--picker", action="store_true", help="Launch interactive Google Photos Picker flow")
    p_scan.add_argument("--limit", type=int, default=50, help="Maximum items to scan")
    p_scan.add_argument("--dry-run", action="store_true", help="Catalog candidates without downloading")
    p_scan.set_defaults(func=cmd_scan)

    # process
    p_process = subparsers.add_parser("process", help="Process discovered videos through intelligence pipeline")
    p_process.add_argument("--limit", type=int, default=50, help="Max candidates to process")
    p_process.add_argument("--status", type=str, default=None, help="Filter videos by status (e.g. DOWNLOADED, PENDING_REVIEW)")
    p_process.add_argument("--reprocess", action="store_true", help="Re-process candidates missing GPS coordinates or identification")
    p_process.set_defaults(func=cmd_process)

    # review
    p_review = subparsers.add_parser("review", help="Interactive TUI review queue")
    p_review.add_argument("--non-interactive", action="store_true", help="Print table without interactive prompt")
    p_review.set_defaults(func=cmd_review)

    # upload
    p_upload = subparsers.add_parser("upload", help="Upload approved videos to YouTube")
    p_upload.add_argument("--limit", type=int, default=5, help="Number of videos to upload")
    p_upload.add_argument("--privacy", choices=["private", "unlisted", "public"], help="Override privacy level")
    p_upload.set_defaults(func=cmd_upload)

    # auth
    p_auth = subparsers.add_parser("auth", help="OAuth authentication management")
    p_auth.add_argument(
        "service",
        choices=["google-photos", "google-drive", "youtube", "status"],
        help="Target authentication service",
    )
    p_auth.add_argument(
        "--redirect-uri",
        type=str,
        default=None,
        help="Override OAuth redirect URI (default matches configured service redirect URI)",
    )
    p_auth.add_argument(
        "--force",
        action="store_true",
        help="Force re-authentication even if token exists",
    )
    p_auth.set_defaults(func=cmd_auth)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
