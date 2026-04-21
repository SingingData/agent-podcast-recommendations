#!/usr/bin/env python3
"""
agent-fetch-recos-add-to-library.py
────────────────────────────────────────────────────────────────────────────────
Fetches new podcast episodes from Spotify, extracts music recommendations from
show notes, searches for each track on Spotify using fuzzy matching, and adds
matched tracks to a dedicated playlist.

Podcasts to monitor are configured in podcasts_sources.txt (one per line).
Each podcast maintains its own state file in administrative-files/ to track
which episodes have been processed and which tracks have been added.

On completion, sends an email notification listing any newly added tracks.

Runs on a cron schedule — see cron-jobs-schedule.txt for timing details.
────────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import sys
import subprocess

# ── Dependency bootstrap ──────────────────────────────────────────────────────
# Read requirements.txt from administrative-files/ and install any packages
# that are not already present in the current Python environment.
# This runs silently if all packages are already installed.

_REQUIREMENTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "administrative-files",
    "requirements.txt"
)

if os.path.exists(_REQUIREMENTS_FILE):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "-r", _REQUIREMENTS_FILE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

# ── Standard library imports ──────────────────────────────────────────────────

import json
import time
import shutil
import smtplib
import logging
from datetime import datetime
from html.parser import HTMLParser
from html import unescape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Third-party imports ───────────────────────────────────────────────────────

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from thefuzz import fuzz
from dotenv import load_dotenv


# ── Config ────────────────────────────────────────────────────────────────────

# Resolve absolute paths relative to this script's location
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
ADMIN_DIR     = os.path.join(BASE_DIR, "administrative-files")    # state, logs, unmatched
PODCASTS_FILE = os.path.join(BASE_DIR, "podcasts_sources.txt")    # podcast list
LOG_FILE      = os.path.join(ADMIN_DIR, "agent.log")              # persistent run log
ENV_FILE      = os.path.join(os.path.dirname(BASE_DIR), ".env")   # shared workspace .env

# Minimum fuzzy match score (0–100) required to accept a Spotify search result.
# Scores below this threshold are logged as unmatched and skipped.
FUZZY_THRESHOLD = 80

# Spotify OAuth scopes required by this agent
SPOTIFY_SCOPES = (
    "playlist-modify-public playlist-modify-private "
    "playlist-read-private playlist-read-collaborative "
    "user-library-read"
)


# ── Logging ───────────────────────────────────────────────────────────────────

# Write all log output to both the persistent log file and stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ── Podcast config ────────────────────────────────────────────────────────────

def load_podcasts():
    """
    Read podcasts_sources.txt and return a list of (show_id, playlist_name, description) tuples.

    File format (one podcast per line):
        show_id | playlist_name | description
    Lines starting with # are treated as comments and ignored.
    """
    podcasts = []

    with open(PODCASTS_FILE, "r") as f:
        for line in f:
            line = line.strip()

            # Skip blank lines and comments
            if not line or line.startswith("#"):
                continue

            parts = [p.strip() for p in line.split("|")]

            if len(parts) >= 2:
                show_id       = parts[0]
                playlist_name = parts[1]
                description   = parts[2] if len(parts) >= 3 else ""
                podcasts.append((show_id, playlist_name, description))

    log.info(f"Loaded {len(podcasts)} podcast(s) from {PODCASTS_FILE}")
    return podcasts


# ── State ─────────────────────────────────────────────────────────────────────

def _slug(playlist_name):
    """
    Convert a playlist name to a lowercase hyphenated slug suitable for use in filenames.
    Example: 'Ill Advised Picks' -> 'ill-advised-picks'
    """
    slug = playlist_name.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


def state_file_for(show_id, playlist_name=""):
    """Return the full path to the state JSON file for a given podcast."""
    name = _slug(playlist_name) if playlist_name else show_id
    return os.path.join(ADMIN_DIR, f"state_{name}.json")


def state_backup_for(show_id, playlist_name=""):
    """Return the full path to the state backup file for a given podcast."""
    name = _slug(playlist_name) if playlist_name else show_id
    return os.path.join(ADMIN_DIR, f"state_{name}.backup.json")


def load_state(show_id, playlist_name=""):
    """
    Load persisted state for a podcast from disk.
    Returns a default empty state dict if no state file exists yet (first run).

    State tracks:
      - show_id / playlist_name    : identifiers for this podcast
      - playlist_id                : Spotify playlist ID (set on first run, reused thereafter)
      - processed_episode_ids      : list of episode IDs already handled
      - extracted_tracks           : full list of all tracks ever parsed from show notes
      - spotify_track_ids          : Spotify track IDs already added to the playlist
      - last_run                   : ISO timestamp of the last successful run
    """
    state_file = state_file_for(show_id, playlist_name)

    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            return json.load(f)

    # Return a fresh default state for first-time runs
    return {
        "show_id":               show_id,
        "playlist_name":         playlist_name,
        "playlist_id":           None,
        "processed_episode_ids": [],
        "extracted_tracks":      [],
        "spotify_track_ids":     [],
        "last_run":              None
    }


def save_state(state):
    """
    Write state to disk, backing up the previous version first.
    The backup allows manual recovery if the state file is corrupted.
    """
    show_id       = state["show_id"]
    playlist_name = state.get("playlist_name", "")
    state_file    = state_file_for(show_id, playlist_name)
    backup_file   = state_backup_for(show_id, playlist_name)

    # Rotate current state to backup before overwriting
    if os.path.exists(state_file):
        shutil.copy(state_file, backup_file)

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

    log.info("State saved.")


# ── Spotify Client ────────────────────────────────────────────────────────────

def get_spotify_client():
    """
    Initialise and return an authenticated Spotify client.

    On first run, opens a browser window for the user to authorise the app.
    On subsequent runs, uses the cached refresh token in .spotify_token_cache
    to silently obtain a new access token — no browser interaction required.

    Credentials are read from environment variables set in the workspace .env file:
      SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI
    """
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id     = os.getenv("SPOTIFY_CLIENT_ID"),
            client_secret = os.getenv("SPOTIFY_CLIENT_SECRET"),
            redirect_uri  = os.getenv("SPOTIFY_REDIRECT_URI"),
            scope         = SPOTIFY_SCOPES,
            cache_path    = os.path.join(BASE_DIR, ".spotify_token_cache"),
            open_browser  = True
        )
    )


# ── Retry wrapper ─────────────────────────────────────────────────────────────

def spotify_call(fn, *args, retries=5, **kwargs):
    """
    Call a Spotify API function with automatic retry on rate limit (429) or
    service unavailable (503) errors.

    Uses the Retry-After header value when available; otherwise falls back to
    exponential backoff (2^attempt seconds). Raises after all retries are exhausted.
    """
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)

        except spotipy.exceptions.SpotifyException as e:
            if e.http_status in (429, 503):
                # Respect the Retry-After header if present, else use exponential backoff
                wait = int(e.headers.get("Retry-After", 2 ** attempt))
                log.warning(f"Rate limited. Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise

    raise Exception(f"Failed after {retries} retries.")


# ── Episode Fetching ──────────────────────────────────────────────────────────

def fetch_episodes(sp, show_id, state):
    """
    Fetch all episodes for a podcast show that have not yet been processed.

    Episodes are skipped only if their ID already appears in state['processed_episode_ids'].
    No date filtering is applied — this ensures episodes are never silently missed
    if the agent was offline or failing during the period they were published.

    Returns a list of episode dicts containing id, name, release_date,
    description, and html_description.
    """
    log.info("Fetching episodes...")
    episodes = []
    offset   = 0

    while True:

        # Fetch up to 50 episodes at a time (Spotify API page size limit)
        results = spotify_call(sp.show_episodes, show_id, limit=50, offset=offset)
        items   = results.get("items", [])

        if not items:
            break

        for ep in items:
            ep_id = ep["id"]

            # Skip only episodes explicitly confirmed as processed — do not filter by date.
            # This ensures any episode not in processed_episode_ids is always retried,
            # regardless of when the last run occurred or whether it succeeded.
            if ep_id in state["processed_episode_ids"]:
                continue

            episodes.append({
                "id":               ep_id,
                "name":             ep["name"],
                "release_date":     ep.get("release_date", ""),
                "description":      ep.get("description", ""),
                "html_description": ep.get("html_description", "")
            })

        # Stop paginating when there are no more pages
        if results["next"] is None:
            break

        offset += 50

    log.info(f"Found {len(episodes)} new episode(s) to process.")
    return episodes


# ── HTML List Item Parser ─────────────────────────────────────────────────────

class ListItemParser(HTMLParser):
    """
    Minimal HTML parser that extracts the text content of all <li> elements.

    Podcast show notes are delivered as HTML. Track listings appear as <li>
    items inside unordered lists. This parser walks the HTML and collects
    the plain text of each <li>, handling HTML entities and character references.
    """

    def __init__(self):
        super().__init__()
        self.in_li   = False   # True while inside a <li> element
        self.items   = []      # Collected plain-text list items
        self.current = []      # Text fragments for the item currently being parsed

    def handle_starttag(self, tag, attrs):
        # Start collecting text when we enter a <li> tag
        if tag == "li":
            self.in_li   = True
            self.current = []

    def handle_endtag(self, tag):
        # On closing </li>, join collected fragments and store the item
        if tag == "li":
            self.in_li = False
            text = unescape("".join(self.current)).strip()
            if text:
                self.items.append(text)

    def handle_data(self, data):
        # Accumulate raw text while inside a list item
        if self.in_li:
            self.current.append(data)

    def handle_entityref(self, name):
        # Resolve named HTML entities (e.g. &amp;) inside list items
        if self.in_li:
            self.current.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        # Resolve numeric character references (e.g. &#160;) inside list items
        if self.in_li:
            self.current.append(unescape(f"&#{name};"))


# ── Track Extraction ──────────────────────────────────────────────────────────

def extract_tracks_from_description(html_description, episode_name):
    """
    Parse music recommendations from a podcast episode's HTML show notes.

    Handles two track listing formats used in the show notes:
      Format 1 — quoted title followed by artist:
          'Track Title' by Artist Name
          "Track Title" by Artist Name  (curly or straight quotes)
      Format 2 — title and artist separated by a dash:
          Track Title - Artist Name
          Track Title — Artist Name  (em dash)

    Returns a list of track dicts with keys:
        spotify_track_id  (None until matched)
        artist
        track
        source_episode
    """
    tracks = []
    seen   = set()   # Deduplicate tracks within the same episode

    # Extract all <li> items from the episode HTML
    parser = ListItemParser()
    parser.feed(html_description)
    li_items = parser.items

    log.info(f"  Found {len(li_items)} list item(s) in show notes.")

    for item in li_items:
        item = item.strip()
        if not item or len(item) < 3:
            continue

        track_name, artist_name = None, None

        # Try Format 1: 'Track' by Artist (any quote style)
        m = re.match(
            r"""['\u2018\u2019\u201c\u201d\"](.+?)['\u2018\u2019\u201c\u201d\"]\s+by\s+(.+)""",
            item, re.IGNORECASE
        )
        if m:
            track_name  = m.group(1).strip()
            artist_name = m.group(2).strip()

        # Try Format 2: Track - Artist (hyphen, en dash, or em dash)
        if not track_name:
            m = re.match(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$", item)
            if m:
                track_name  = m.group(1).strip()
                artist_name = m.group(2).strip()

                # Strip trailing "featuring X" qualifiers from the artist name
                artist_name = re.sub(
                    r"\s*[-\u2013]\s*featuring.+$", "", artist_name,
                    flags=re.IGNORECASE
                ).strip()

        if track_name and artist_name:

            # Strip any lingering stray quote characters from both fields
            track_name  = re.sub(
                r"^['\u2018\u2019\u201c\u201d\"]+|['\u2018\u2019\u201c\u201d\"]+$",
                "", track_name
            ).strip()
            artist_name = re.sub(
                r"^['\u2018\u2019\u201c\u201d\"]+|['\u2018\u2019\u201c\u201d\"]+$",
                "", artist_name
            ).strip()

            # Use a normalised key to avoid adding the same track twice from one episode
            key = f"{artist_name.lower()}|{track_name.lower()}"
            if key not in seen:
                seen.add(key)
                log.info(f"    Extracted: {artist_name} — {track_name}")
                tracks.append({
                    "spotify_track_id": None,
                    "artist":           artist_name,
                    "track":            track_name,
                    "source_episode":   episode_name
                })

        else:
            # List item did not match either format — log for manual review
            log.info(f"    Could not parse list item: {item}")

    # If the episode links to a Spotify playlist in the show notes, log the URL
    pl = re.search(r'open\.spotify\.com/playlist/([A-Za-z0-9]+)', html_description)
    if pl:
        log.info(f"  Episode playlist: https://open.spotify.com/playlist/{pl.group(1)}")

    if not tracks:
        log.info(f"  No tracks found in show notes for: {episode_name}")

    return tracks


# ── Playlist Management ───────────────────────────────────────────────────────

def get_or_create_playlist(sp, state, playlist_name, description):
    """
    Return the Spotify playlist ID to use for this podcast.

    If the playlist ID is already stored in state, returns it immediately.
    Otherwise, searches the user's existing playlists for a matching name.
    If none is found, creates a new public playlist and stores its ID in state.
    """
    if state["playlist_id"]:
        log.info(f"Using existing playlist ID: {state['playlist_id']}")
        return state["playlist_id"]

    # Search existing playlists before creating a new one
    log.info(f"Looking for existing playlist named '{playlist_name}'...")
    offset = 0

    while True:
        results = spotify_call(sp.current_user_playlists, limit=50, offset=offset)
        items   = results.get("items", [])

        if not items:
            break

        for pl in items:
            if pl["name"].lower() == playlist_name.lower():
                log.info(f"Found existing playlist: {pl['id']}")
                state["playlist_id"] = pl["id"]
                return pl["id"]

        if results["next"] is None:
            break

        offset += 50

    # No matching playlist found — create a new one
    log.info(f"Creating new playlist: {playlist_name}")
    user_id  = spotify_call(sp.current_user)["id"]
    playlist = spotify_call(
        sp.user_playlist_create,
        user_id,
        playlist_name,
        public=True,
        description=description
    )
    state["playlist_id"] = playlist["id"]
    log.info(f"Created playlist: {playlist['id']}")
    return playlist["id"]


def get_existing_track_ids(sp, playlist_id):
    """
    Fetch the set of all Spotify track IDs currently in a playlist.

    Used to avoid adding duplicates when tracks were added in a previous run
    but the state file was lost or reset. Paginates through all tracks in batches of 100.
    """
    track_ids = set()
    offset    = 0

    while True:
        results = spotify_call(sp.playlist_tracks, playlist_id, limit=100, offset=offset)
        items   = results.get("items", [])

        if not items:
            break

        for item in items:
            if item.get("track"):
                track_ids.add(item["track"]["id"])

        if results["next"] is None:
            break

        offset += 100

    return track_ids


# Regex to strip version suffixes from Spotify track titles before fuzzy matching.
# Prevents mismatches caused by titles like "Blue Jean - 2018 Remaster" or
# "Space Oddity (2015 Remaster)" when the source only lists "Blue Jean".
_VERSION_SUFFIX_RE = re.compile(
    r'\s*[-\u2013(]\s*'
    r'(\d{4}\s+)?(remaster(ed)?|live|remix(ed)?|acoustic|radio edit|single version|'
    r'mono|stereo|demo|edit|version|anniversary|deluxe|extended|instrumental).*$',
    re.IGNORECASE
)


def _clean_spotify_title(name: str) -> str:
    """Strip remaster/live/remix suffixes from a Spotify track title before matching."""
    return _VERSION_SUFFIX_RE.sub('', name).strip()


def search_and_match_track(sp, artist, track):
    """
    Search Spotify for a track and return its track ID if a confident match is found.

    Uses a combined fuzzy score averaging:
      - Token sort ratio on the track name (after stripping version suffixes)
      - Token sort ratio on the artist name

    Returns the Spotify track ID if the combined score meets FUZZY_THRESHOLD,
    otherwise returns None and logs the track as unmatched.
    """
    query = f"track:{track} artist:{artist}"

    try:
        results = spotify_call(sp.search, q=query, type="track", limit=5)
        items   = results.get("tracks", {}).get("items", [])

        for item in items:

            # Strip version suffixes before comparing track names
            clean_name   = _clean_spotify_title(item["name"])
            name_score   = fuzz.token_sort_ratio(track.lower(), clean_name.lower())

            # Join all artist names for multi-artist tracks
            artist_score = fuzz.token_sort_ratio(
                artist.lower(),
                " ".join(a["name"] for a in item["artists"]).lower()
            )

            combined = (name_score + artist_score) / 2

            if combined >= FUZZY_THRESHOLD:
                log.info(
                    f"    Matched '{artist} - {track}' → '{item['name']}' "
                    f"(score: {combined:.0f})"
                )
                return item["id"]

        log.warning(f"    No match above threshold for: {artist} - {track}")

    except Exception as e:
        log.warning(f"    Search failed for '{artist} - {track}': {e}")

    return None


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject, body):
    """
    Send a plain-text email notification via Gmail SMTP (SSL, port 465).

    Credentials are read from the workspace .env file:
      GMAIL_ADDRESS       — the sending Gmail account
      GMAIL_APP_PASSWORD  — Gmail app password (spaces stripped automatically)
      NOTIFICATION_EMAIL  — recipient address

    Logs a warning and returns silently if any credential is missing.
    """
    gmail_address  = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
    recipient      = os.getenv("NOTIFICATION_EMAIL")

    # Skip silently if credentials are not fully configured
    if not all([gmail_address, gmail_password, recipient]):
        log.warning("Email credentials incomplete — skipping notification.")
        return

    msg            = MIMEMultipart()
    msg["From"]    = gmail_address
    msg["To"]      = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, gmail_password)
            server.sendmail(gmail_address, recipient, msg.as_string())
        log.info(f"Email sent to {recipient}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def build_email_body(new_tracks, playlist_id):
    """
    Build the plain-text body for the new-tracks notification email.
    Lists each newly added track with its source episode and a link to the playlist.
    """
    lines = [
        f"🎵 {len(new_tracks)} new track(s) added to the playlist!\n",
        f"Playlist: https://open.spotify.com/playlist/{playlist_id}\n",
        "-" * 40, ""
    ]

    for t in new_tracks:
        if t.get("artist") and t.get("track"):
            lines.append(f"• {t['artist']} — {t['track']}")
        elif t.get("spotify_track_id"):
            lines.append(f"• https://open.spotify.com/track/{t['spotify_track_id']}")
        lines.append(f"  From: {t['source_episode']}\n")

    return "\n".join(lines)


# ── Process single podcast ────────────────────────────────────────────────────

def process_podcast(sp, show_id, playlist_name, description):
    """
    Run the full fetch → extract → match → add cycle for a single podcast.

    Steps:
      1. Load persisted state for this podcast
      2. Resolve or create the target Spotify playlist
      3. Fetch all unprocessed episodes
      4. For each episode, parse show notes for track listings
      5. Search Spotify for each track and add matches to the playlist
      6. Append any unmatched tracks to the unmatched log file
      7. Save updated state to disk

    Returns (new_tracks_added, playlist_id) for use in the email notification.
    """
    log.info(f"--- Podcast: {playlist_name} (show: {show_id}) ---")

    state          = load_state(show_id, playlist_name)
    unmatched_file = os.path.join(ADMIN_DIR, f"unmatched_{_slug(playlist_name)}.log")

    # Resolve or create the Spotify playlist for this podcast
    playlist_id        = get_or_create_playlist(sp, state, playlist_name, description)

    # Fetch current playlist contents to prevent duplicate additions
    existing_track_ids = get_existing_track_ids(sp, playlist_id)
    log.info(f"Playlist has {len(existing_track_ids)} existing track(s).")

    episodes = fetch_episodes(sp, show_id, state)

    all_extracted    = []   # All tracks parsed from show notes this run
    new_tracks_added = []   # Tracks successfully added to the playlist
    unmatched        = []   # Tracks that could not be matched on Spotify

    for ep in episodes:
        log.info(f"Processing: {ep['name']} ({ep['release_date']})")

        html_desc = ep.get("html_description") or ep.get("description", "")
        tracks    = extract_tracks_from_description(html_desc, ep["name"])

        for t in tracks:
            all_extracted.append(t)

            # Use pre-resolved track ID if available, otherwise search Spotify
            if t.get("spotify_track_id"):
                track_id = t["spotify_track_id"]
            else:
                track_id = search_and_match_track(sp, t["artist"], t["track"])

            if not track_id:
                # Log unmatched track for manual review
                label = f"{t.get('artist', '?')} - {t.get('track', '?')}"
                log.warning(f"  Unmatched: {label}")
                unmatched.append(f"{label} (from: {t['source_episode']})")
                continue

            # Skip if track is already in the playlist (via live check or state)
            if track_id in existing_track_ids or track_id in state["spotify_track_ids"]:
                log.info(f"  Already in playlist, skipping.")
                continue

            # Add the track to the playlist and record it in state
            spotify_call(sp.playlist_add_items, playlist_id, [track_id])
            existing_track_ids.add(track_id)
            state["spotify_track_ids"].append(track_id)
            new_tracks_added.append(t)
            log.info(f"  ✅ Added to playlist.")

        # Mark episode as processed regardless of how many tracks were found
        state["processed_episode_ids"].append(ep["id"])

    # Append any unmatched tracks to the per-podcast unmatched log
    if unmatched:
        with open(unmatched_file, "a") as f:
            f.write(f"\n--- Run {datetime.now().isoformat()} ---\n")
            f.write("\n".join(unmatched) + "\n")

    # Persist updated state and record the run timestamp
    state["extracted_tracks"].extend(all_extracted)
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    log.info(f"  Episodes processed: {len(episodes)}")
    log.info(f"  Tracks found:       {len(all_extracted)}")
    log.info(f"  Tracks added:       {len(new_tracks_added)}")
    log.info(f"  Unmatched:          {len(unmatched)}")

    return new_tracks_added, playlist_id


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    """
    Entry point. Loads environment variables, authenticates with Spotify,
    then processes each podcast listed in podcasts_sources.txt in sequence.
    Sends one email notification per playlist that received new tracks.
    On hard failure, sends an error alert email and re-raises the exception.
    """
    start_time = time.time()
    log.info("=" * 50)
    log.info("Podcast Agent starting...")
    log.info("=" * 50)

    # Load all credentials and config from the shared workspace .env file
    load_dotenv(ENV_FILE)
    podcasts = load_podcasts()

    try:
        sp = get_spotify_client()
        log.info("Spotify authenticated.")

        # Accumulate (track, playlist_id, playlist_name) across all podcasts
        total_added = []

        # Process each podcast in the order listed in podcasts_sources.txt
        for show_id, playlist_name, description in podcasts:
            new_tracks, playlist_id = process_podcast(sp, show_id, playlist_name, description)
            for t in new_tracks:
                total_added.append((t, playlist_id, playlist_name))

        elapsed = time.time() - start_time
        log.info("=" * 50)
        log.info(f"Done in {elapsed:.1f}s — {len(podcasts)} podcast(s) processed")
        log.info(f"Total tracks added: {len(total_added)}")
        log.info("=" * 50)

        if total_added:
            # Group new tracks by playlist and send one email per playlist
            by_playlist = {}
            for t, pid, pname in total_added:
                by_playlist.setdefault((pid, pname), []).append(t)

            for (pid, pname), tracks in by_playlist.items():
                send_email(
                    f"🎵 {len(tracks)} new track(s) added to {pname}",
                    build_email_body(tracks, pid)
                )
        else:
            log.info("No new tracks — skipping email.")

    except Exception as e:
        # On hard failure, log the full traceback and send an alert email
        log.error(f"Hard failure: {e}", exc_info=True)
        send_email(
            "⚠️ Podcast Agent Error",
            f"The podcast agent failed:\n\n{e}\n\nCheck {LOG_FILE} for details."
        )
        raise


if __name__ == "__main__":
    main()
