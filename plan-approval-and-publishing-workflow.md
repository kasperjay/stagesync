# StageSync approval and publishing implementation plan

Status: implemented, tested, and deployed. Date: November 4, 2026.

## Scope and decisions

This is the active plan for the uploaded workflow design and recovered conversation. The final correction supersedes any artist-first approval layout: **Venue → Show → Artist → Song**. Approval verifies clip identity; publishing independently prepares and releases an upload.

Sources: the uploaded StageSync Concert Workflow Dashboard PNG, the HTML conversation inside StageSync_Conversation_and_Images-a3044aa2eaaa.zip, the preceding artist-matching bug report, and the current repository. The ZIP contains only two substantive user corrections and their replies, not a complete original chat export. Its four images are unlabeled schematic blocks; use the standalone detailed PNG and existing approved styling for visual guidance. No additional requirements are inferred from missing conversation text.

No blocking product questions. Proposed defaults: unknown songs require an explicit human choice; batch approval is an explicit action, never automatic; scheduling means YouTube release time, displayed in America/Chicago and stored in UTC. Existing visibility, domains, ingestion behavior, recordings, and published uploads remain protected. Mockup artist names, counts, provider names, and channel names are examples, not production data or requirements to change providers.

## 1. Separate verified identity from publication readiness

Affected files:

- `file stagesync/database/models.py`, `file connection.py`: typed approval state, identity revision, evidence, publishing draft and channel-specific job persistence.
- `file stagesync/pipeline.py`: separate identity approval, draft generation, explicit queueing, and upload dispatch.
- `file stagesync/web/server.py`, `file cli.py`, `file daemon.py`: enforce identical approval and queue rules through every entry point.
- `file tests/test_pipeline_integration.py`, `file test_web_server.py`, `file test_cli.py`, `file test_daemon.py`: extend existing lifecycle checks.

Tasks:

- [x]   Replace the current `approve_video` behavior that creates a QUEUED YouTube job. Save verified venue, show/date, performing artist, song resolution, evidence, and approval revision atomically. Approval may create an editable DRAFT only.

- [x]   Add explicit publishing draft save and enqueue operations. The uploader must select explicitly queued jobs, not every APPROVED video. Apply this rule to web, CLI, daemon, retries, and batch actions.

- [x]   Keep detected candidates distinct from manually confirmed identity. Preserve manual identification precedence and wrong-artist rejection already present in the matching code.

- [x]   Model per-field confirmation and dependency invalidation: venue changes invalidate incompatible show/artist/song choices; show/date changes invalidate incompatible artist/setlist/song choices; artist changes clear prior song/confidence. Revoke approval and block pending publication when its identity revision becomes stale.

- [x]   Do not overwrite manually edited publication text when identity changes; mark the draft as needing review and offer explicit regeneration from the newly approved identity.

- [x]   Keep existing upload history and media intact. Existing queued records do not prove separate publication consent: require explicit publication review before dispatch under the new contract.

Acceptance checks: approval sends no upload request; daemon and CLI cannot bypass explicit queueing; retries cannot publish stale revisions; upstream edits invalidate only affected choices; publication edits never mutate concert identity; existing published records stay unchanged. Use temporary SQLite databases and deterministic local service fakes.

## 2. Implement ordered approval and artist-constrained song resolution

Affected files:

- `file stagesync/web/index.html`, `file dashboard.html`: approved shell, ordered review workspace, queue navigation and preview.
- `file stagesync/web/server.py`: venue/show/artist/song endpoints, clip preview, approval and review actions.
- `file stagesync/pipeline.py`: dependency validation, artist selection, song matching/options and review transitions.
- `file stagesync/intelligence/setlist_fm.py`, `file musicbrainz.py`, `file ai_audio_judge.py`: exact show/artist candidate sourcing and evidence.
- `file tests/test_user_assist_venue.py`, `file test_user_assist_band.py`, `file test_setlist_fm.py`, `file test_intelligence_services.py`, `file test_web_server.py`: regressions and input validation.

Tasks:

- [x]   Present clip playback beside metadata, evidence/source, and confidence when actually supplied. Provide venue search/selection, then show selection constrained by venue and date, then lineup artist selection or manual entry, then song matching.

- [x]   Require confirmation of detected values; do not treat a suggested headliner as the performing artist for every festival clip. Support manually recorded show details when the catalog lacks the event.

- [x]   Scope setlist lookup to the selected artist, venue/show and performance date. A same-city/date result or another artist's headliner setlist is not an acceptable substitute.

- [x]   Attempt audio recognition within the selected artist context. A wrong-artist acoustic result must never replace that selection. Missing/weak evidence returns no match or an unconfirmed suggestion, not a fabricated high-confidence song.

- [x]   On unsuccessful matching, open manual choices from that artist's exact show setlist when available; otherwise provide artist-scoped discography search/autocomplete. Keep discography/manual entry available when a setlist exists but omits the track.

- [x]   Support explicit manual song entry, covers, improvisation and unknown-track resolution. Preserve the performing artist separately from original recording artist evidence. Manual confirmation displays as manual, not as 100% acoustic confidence.

- [x]   Keep lyrics/discography candidates labeled as suggestions requiring confirmation. Debounce autocomplete and ignore responses belonging to a superseded artist/show revision.

- [x]   Implement Approve & Next, Flag for Later, Reject Match, and explicit batch approval. Reject Match rejects the candidate without deleting footage or rejecting the recording itself. Flagging persists across reloads; advancement happens only after a successful save.

- [x]   Batch approval checks complete identity and evidence for each selected clip and reports exclusions. A high score alone never bypasses missing venue, show, artist or song resolution.

Acceptance checks: reproduce Warped Tour '14 with a manually chosen artist and conflicting Marzomets 98% result; the selected artist survives and the conflicting song is rejected. Test artist changes, missing setlists, wrong venue/date results, discography failures, explicit unknown/cover entry, stale matching responses, save failures, suggestion confirmation, flag/reject persistence and per-clip batch eligibility.

## 3. Build publishing controls and connect the approved visual workflow to real data

Affected files:

- `file stagesync/web/index.html`, `file dashboard.html`, `file server.py`: publishing editor, workflow navigation, truthful aggregates, library/archive and status endpoints.
- `file stagesync/database/models.py`, `file connection.py`: draft edits, selected thumbnail, channel-specific jobs, scheduling and saved defaults.
- `file stagesync/publisher/metadata_gen.py`, `file youtube_auth.py`, `file resumable_upload.py`: independent metadata edits, actual channel identity, thumbnails and scheduled publication.
- `file stagesync/intelligence/thumbnail_extractor.py`, `file pipeline.py`, `file daemon.py`, `file cli.py`: real frame selection, explicit dispatch, retry and scheduling lifecycle.
- `file tests/test_metadata_gen.py`, `file test_resumable_upload.py`, `file test_auth_and_credentials.py`, `file test_web_server.py`, `file test_daemon.py`, `file test_anti_slop.py`: publication and truthful-data regressions.
- `file README.md`, `file DOCUMENTATION.md`, `file ERD.md`: document actual aiohttp/HTML architecture, approval sequence and publishing contract; remove obsolete architectural claims.

Tasks:

- [x]   Populate suggested title, description and tags from approved identity; allow independent edits and persisted defaults. Validate fields before queueing and preserve edits on reload.

- [x]   Provide real clip-frame thumbnails, frame selection/preview, and persistence of the chosen frame through upload. Report thumbnail failure separately from successful video upload so retries do not duplicate videos.

- [x]   Show authenticated YouTube channels by actual identity; support selecting one or more connected channels with isolated credentials and one publication job per clip/channel. Never render a fictional Main Channel or silently use a different account.

- [x]   Implement Upload Now and Schedule Upload as explicit actions. Persist timezone-aware future release times and privacy requirements; use the provider's scheduled-release mechanism, distinguish uploaded-private from publicly released, and handle expired/rejected schedules without publishing immediately.

- [x]   Keep chunked resume, quota limits and error reporting. Claim each queued job atomically and make repeated enqueue/retry actions idempotent. Display channel-specific progress, errors and resulting URLs.

- [x]   Implement selection-based bulk publication edits with a preview of affected clips; saving defaults must not silently overwrite existing drafts.

- [x]   Preserve the approved typography, palette, layout and page elements while wiring Overview, Ingest, Processing/Identify, Approval, Publish, Library/Shows and Archive to existing backend capabilities. Publish must become its own editor rather than a catalog filtered to APPROVED.

- [x]   Wire source selection, file import, queue progress and supported cancel/retry actions to actual ingestion behavior. Unsupported pause or other mockup controls must not pretend to work. Keep existing scan protections and source integrations.

- [x]   Show real processing steps, provider names and event logs. Derive waveform display from audio if retained; decorative animation must not imply processing progress or recognition evidence.

- [x]   Compute dashboard counts from their actual meanings; distinguish detected shows, verified matches, approved clips, queued jobs and published uploads. Do not substitute another metric for a missing aggregate. Show loading, empty and unavailable states explicitly.

- [x]   Group Library by show with real clip/approval/publication counts, filters and search; show completed uploads, destinations and history in Archive. Use actual clip thumbnails and filenames when metadata is absent; never invent titles, artists, trends or confidence.

Acceptance checks: draft editing leaves approved identity unchanged; drafts survive regeneration attempts; duplicate clicks yield one job per destination; channel credentials remain isolated; thumbnail retries do not reupload videos; future and daylight-saving schedules resolve correctly; partial failures retain successful destinations; dashboard totals agree with database fixtures; unavailable integrations and empty catalogs render honest states. Extend existing tests and add small pure-state tests for frontend ordering, stale responses and save/advance behavior.

## Implementation checkpoints

Complete in order: identity/publishing separation → ordered approval → publishing and remaining data wiring. Run the relevant checks after each increment and the full existing suite before release. Implementation was approved on November 4, 2026. Once final checks pass, restart the existing managed service, verify HTTP 200 and intended content, commit the scoped changes, and deploy through its existing configuration without changing visibility or domains. Preserve unrelated working-tree changes.

## Implementation notes — November 4, 2026

The live `file dashboard.html` shell is preserved. Ordered Approval, a separate publishing editor, Library / Shows, Archive, and Workflow Counts are layered into that shell through `file stagesync/web/static/workflow.js`. The live root `file index.html` shell embeds `/workspace` and now routes its navigation to these controls, with real workflow counts and show summaries. Existing ingestion source selection, scan cancellation and progress remain the actual production implementations; no simulated pause or waveform was introduced.

`file stagesync/workflow.py` owns ordered field confirmations and revision snapshots, independent draft persistence, channel-specific job persistence and atomic job claims. `file stagesync/web/workflow.py` provides the matching HTTP operations. Existing queued rows remain historical and are never dispatched by the new explicit queue. Publication text is stored separately from the older generated metadata records, protecting manual edits during identification changes.

Scheduling accepts America/Chicago local time, rejects nonexistent spring-forward times, and offers first/second occurrence for repeated fall-back times. The uploader sends a future private `publishAt` release; `SCHEDULED` and `UPLOADED` are distinct from `PUBLISHED`. Public release is confirmed from YouTube when the dispatcher refreshes scheduled jobs. Interrupted jobs retain their resumable session and original metadata; changed metadata cannot silently reuse that session. Abandoned active jobs are recovered after a 30-minute heartbeat timeout.

Connected channels are resolved through YouTube rather than invented labels. Additional channel tokens use separate JSON files in the configured token directory's `channels/` subfolder. Existing upload-only authorizations may need reconnecting with the added `youtube.readonly` scope before channel identity is available. Unavailable channels show an explicit unavailable state and block queueing.

Validation: all 193 tests passed, including frontend state and root navigation tests, including real temporary SQLite and HTTP lifecycle checks, per-channel partial failures, thumbnail failure isolation, stale revisions, explicit unknown resolution, DST transitions, preserved draft edits and strict venue/date/artist setlist lookup. No real YouTube uploads or external messages were sent during testing. The existing managed service was republished without visibility changes and returned HTTP 200. Playwright verified root Approval, Library, Archive, and Publishing navigation, plus clip review in the workspace.