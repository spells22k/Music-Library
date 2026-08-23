import json
import random
import re
import subprocess
import sys
import hashlib
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import pandas as pd
from playwright.sync_api import sync_playwright

from whosampled_match import (
    canonical_url,
    canonical_url_variants,
    load_artist_slug_cache,
    load_wikidata_cache,
    wikidata_artist_resolution,
    ARTIST_SLUG_CACHE_FILE,
)
from parse_whosampled_track import extract_source_metadata, extract_relationships
from spotify_metadata import resolve_track
from whosampled_review_ui import run_whosampled_review
from whosampled_media import capture_rendered_artwork


PLAYLIST_URL = sys.argv[1]

BLIND_PHASE1_CACHE = (
    "--blind-phase1-cache"
    in sys.argv[2:]
)

if BLIND_PHASE1_CACHE:
    print(
        "PHASE 1 CACHE BLINDING ENABLED:"
    )
    print(
        "  Structured track cache: IGNORED"
    )
    print(
        "  Learned artist slug cache: IGNORED"
    )
    print(
        "  Persistent HTML archive: IGNORED"
    )

# Use the Spotify playlist ID as the stable run identifier.
# Re-running the same playlist therefore resumes the same run.
playlist_id_match = re.search(
    r"/playlist/([A-Za-z0-9]+)",
    PLAYLIST_URL
)

if playlist_id_match:
    playlist_id = playlist_id_match.group(1)
else:
    playlist_id = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        PLAYLIST_URL
    ).strip("_")

RUN_ID = f"playlist_{playlist_id}"

if BLIND_PHASE1_CACHE:
    RUN_ID += "_blind"

RUN_DIR = Path("runs") / RUN_ID

RUN_DIR.mkdir(parents=True, exist_ok=True)

SPOTIFY_FILE = RUN_DIR / "spotify_tracks.csv"
MATCH_FILE = RUN_DIR / "matched_tracks.csv"
WHO_SAMPLED_REVIEW_FILE = RUN_DIR / "whosampled_review_decisions.json"
RELATIONSHIP_FILE = RUN_DIR / "relationships.csv"
RECORDINGS_FILE = RUN_DIR / "recordings.csv"
CREDITS_FILE = RUN_DIR / "credits.csv"
ENRICHED_FILE = RUN_DIR / "relationships_enriched.csv"
STATE_FILE = RUN_DIR / "state.json"

HTML_DIR = RUN_DIR / "whosampled_pages"
HTML_DIR.mkdir(exist_ok=True)


def normalize(text):
    """
    Unicode-safe comparison normalization.

    This function is ONLY for comparing strings and building
    lookup keys. It must never be used as the canonical artist
    name or WhoSampled slug.

    Examples:
        Lô Borges      -> lô borges
        João Gilberto  -> joão gilberto
        Trio Mocotó    -> trio mocotó
    """

    import unicodedata
    import re

    if text is None:
        return ""

    text = str(text).strip()

    if not text:
        return ""

    # Normalize equivalent Unicode representations.
    text = unicodedata.normalize(
        "NFC",
        text
    )

    # Case-insensitive comparison.
    text = text.casefold()

    # Standardize visually equivalent punctuation.
    text = text.replace(
        "’",
        "'"
    )
    text = text.replace(
        "‘",
        "'"
    )
    text = text.replace(
        "–",
        "-"
    )
    text = text.replace(
        "—",
        "-"
    )

    # Treat punctuation that is irrelevant to comparison as
    # separators, but DO NOT strip Unicode letters.
    text = re.sub(
        r"[^\w\s'-]",
        " ",
        text,
        flags=re.UNICODE
    )

    # Collapse repeated whitespace.
    return " ".join(
        text.split()
    )


def artist_list(value):
    return [
        x.strip()
        for x in str(value).split(",")
        if x.strip()
    ]


def safe_filename(text):
    text = unquote(str(text))
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:150]


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def load_state():
    if STATE_FILE.exists():
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    return {
        "playlist_exported": False,
        "tracks_processed": [],
        "relationships_parsed": [],
        "spotify_enriched": False,
        "stopped_on_429": False,
    }


def save_artist_slug_cache(data):
    ARTIST_SLUG_CACHE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    ARTIST_SLUG_CACHE_FILE.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def learn_artist_slug(
    spotify_artists,
    verified_url,
    source_artists,
    cache
):
    """
    Learn a canonical WhoSampled artist slug only for the
    Spotify artist that corresponds to the WhoSampled source artist.

    This is important for multi-artist Spotify tracks. For example:

        Spotify:
            Toquinho, Jorge Ben Jor

        WhoSampled:
            Toquinho

    must produce only:

        Toquinho -> Toquinho

    and must NOT produce:

        Jorge Ben Jor -> Toquinho
    """

    if not verified_url:
        return

    path = (
        str(verified_url)
        .split(
            "whosampled.com",
            1
        )[-1]
        .strip("/")
    )

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    # Verified track URL must be /Artist/Track/.
    if len(parts) != 2:
        return

    canonical_slug = parts[0]

    if not canonical_slug:
        return

    ws_artists = [
        normalize(a)
        for a in str(
            source_artists
            or ""
        ).split(",")
        if str(a).strip()
    ]

    spotify_artist_pairs = [
        (
            artist,
            normalize(artist)
        )
        for artist in spotify_artists
        if str(artist).strip()
    ]

    matching_spotify_artists = []

    for original_artist, normalized_artist in (
        spotify_artist_pairs
    ):

        if any(
            normalized_artist == ws_artist
            or normalized_artist in ws_artist
            or ws_artist in normalized_artist
            for ws_artist in ws_artists
        ):
            matching_spotify_artists.append(
                original_artist
            )

    # If WhoSampled source metadata is unavailable or no Spotify
    # artist matches it, do NOT guess. This prevents contaminating
    # the learned-slug cache on multi-artist tracks.
    if len(matching_spotify_artists) != 1:

        print(
            "SLUG LEARNING SKIPPED:",
            "could not uniquely identify Spotify artist for",
            canonical_slug,
            "WhoSampled artists:",
            source_artists
        )

        return

    artist = matching_spotify_artists[0]
    key = normalize(artist)

    current = cache.get(
        key,
        []
    )

    if isinstance(
        current,
        str
    ):
        current = [current]

    current = [
        str(x)
        for x in current
        if str(x).strip()
    ]

    updated = [canonical_slug]

    for slug in current:
        if normalize(slug) != normalize(
            canonical_slug
        ):
            updated.append(slug)

    if updated != current:

        cache[key] = updated

        save_artist_slug_cache(
            cache
        )

        print(
            "LEARNED WHO SAMPLED ARTIST SLUG:",
            artist,
            "→",
            canonical_slug
        )



def write_dataframe(df, path):
    df.to_csv(
        path,
        index=False,
        encoding="utf-8"
    )


def write_normalized_outputs(
    accepted_html_files,
    matched_file,
    spotify_file,
    relationship_rows,
):
    """
    Write the normalized Phase-1 data model:

        recordings.csv
            one row per approved source recording

        credits.csv
            one row per structured artist/role credit

        relationships.csv
            one row per relationship

    This function uses only already-collected local HTML and CSV
    files. It performs no network requests.
    """

    import hashlib
    import json
    from bs4 import BeautifulSoup
    from parse_whosampled_track import (
        extract_source_metadata,
        extract_relationships,
    )

    matched_df = pd.read_csv(
        matched_file
    )

    spotify_df = pd.read_csv(
        spotify_file
    )

    # --------------------------------------------------------
    # Spotify lookup by the playlist recording's Spotify ID.
    # --------------------------------------------------------

    spotify_by_id = {}

    for _, row in spotify_df.iterrows():

        spotify_id = str(
            row.get(
                "spotify_track_id",
                "",
            )
            or ""
        ).strip()

        if spotify_id:
            spotify_by_id[
                spotify_id
            ] = row.to_dict()

    # --------------------------------------------------------
    # WhoSampled match lookup.
    # --------------------------------------------------------

    match_by_url = {}

    for _, row in matched_df.iterrows():

        url = str(
            row.get(
                "whosampled_url",
                "",
            )
            or ""
        ).strip().rstrip("/")

        if url:

            match_by_url[
                url
            ] = row.to_dict()

    recording_rows = []
    credit_rows = []
    normalized_relationship_rows = []

    recording_ids_by_url = {}

    # --------------------------------------------------------
    # Recordings + credits from approved primary pages.
    # --------------------------------------------------------

    for html_file in accepted_html_files:

        html_path = (
            HTML_DIR / html_file
        )

        if not html_path.exists():
            continue

        try:

            soup = BeautifulSoup(
                html_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ),
                "html.parser",
            )

            source = extract_source_metadata(
                soup
            )

        except Exception as exc:

            print(
                "NORMALIZED PRIMARY PARSE ERROR:",
                html_path,
                repr(exc),
            )
            continue

        source_url = str(
            source.get(
                "source_url",
                "",
            )
            or ""
        ).strip().rstrip("/")

        if not source_url:
            continue

        recording_id = (
            "REC_"
            + hashlib.sha1(
                source_url.encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
        )

        recording_ids_by_url[
            source_url
        ] = recording_id

        local_thumbnail = (
            HTML_DIR.parent
            / "whosampled_media"
            / (
                html_path.stem
                + ".png"
            )
        )

        # ----------------------------------------------------
        # One recording row.
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Spotify identity comes from matched_tracks.csv.
        #
        # That file records the human-reviewed / automated
        # correspondence between this WhoSampled recording and
        # the Spotify recording. It is therefore the authoritative
        # bridge between the two source systems.
        # ----------------------------------------------------

        spotify_track_id = str(
            matched_row.get(
                "spotify_track_id",
                "",
            )
            or ""
        ).strip()

        spotify_isrc = str(
            matched_row.get(
                "isrc",
                "",
            )
            or ""
        ).strip()

        # ----------------------------------------------------
        # Spotify metadata is then looked up by Spotify track ID.
        # ----------------------------------------------------

        spotify_row = (
            spotify_by_id.get(
                spotify_track_id,
                {}
            )
        )

        spotify_url = str(
            spotify_row.get(
                "spotify_url",
                "",
            )
            or ""
        ).strip()

        spotify_album_name = str(
            spotify_row.get(
                "album_name",
                "",
            )
            or ""
        ).strip()

        spotify_album_release_date = str(
            spotify_row.get(
                "album_release_date",
                "",
            )
            or ""
        ).strip()

        spotify_album_image_url = str(
            spotify_row.get(
                "album_image_url",
                "",
            )
            or ""
        ).strip()

        spotify_album_label = str(
            spotify_row.get(
                "album_label",
                "",
            )
            or ""
        ).strip()

        recording_rows.append({

            "recording_id":
                recording_id,

            "title":
                source.get(
                    "source_title",
                    "",
                ),

            "artist_names":
                source.get(
                    "source_artists",
                    "",
                ),

            "album":
                source.get(
                    "source_album",
                    "",
                ),

            "label":
                source.get(
                    "source_label",
                    "",
                ),

            "release_year":
                source.get(
                    "source_release_year",
                    "",
                ),

            "duration":
                source.get(
                    "source_duration",
                    "",
                ),

            "genre":
                source.get(
                    "source_genre",
                    "",
                ),

            "keywords":
                source.get(
                    "source_keywords",
                    "",
                ),

            "whosampled_url":
                source_url,

            "whosampled_thumbnail_url":
                source.get(
                    "source_thumbnail_url",
                    "",
                ),

            "whosampled_thumbnail_path":
                (
                    str(
                        local_thumbnail
                    )
                    if local_thumbnail.exists()
                    else ""
                ),

            "whosampled_thumbnail_status":
                (
                    "captured"
                    if local_thumbnail.exists()
                    else "unavailable"
                ),

            "youtube_video_id":
                source.get(
                    "source_youtube_video_id",
                    "",
                ),

            "youtube_url":
                source.get(
                    "source_youtube_url",
                    "",
                ),

            "youtube_thumbnail_url":
                source.get(
                    "source_youtube_thumbnail_url",
                    "",
                ),

            # Spotify identity / metadata for the playlist
            # recording, where already available.
            "spotify_track_id":
                spotify_track_id,

            "spotify_url":
                spotify_url,

            "spotify_isrc":
                spotify_isrc,

            "spotify_album_name":
                spotify_album_name,

            "spotify_album_release_date":
                spotify_album_release_date,

            "spotify_album_image_url":
                spotify_album_image_url,

            "spotify_album_label":
                spotify_album_label,
        })

        # ----------------------------------------------------
        # Structured credits.
        # ----------------------------------------------------

        raw_credits = source.get(
            "source_credits",
            "[]",
        )

        try:

            parsed_credits = json.loads(
                raw_credits
                or "[]"
            )

        except Exception:

            parsed_credits = []

        for credit in parsed_credits:

            artist_name = str(
                credit.get(
                    "artist",
                    "",
                )
                or ""
            ).strip()

            role = str(
                credit.get(
                    "role",
                    "",
                )
                or ""
            ).strip()

            source_role = str(
                credit.get(
                    "source_role",
                    "",
                )
                or ""
            ).strip()

            if not artist_name or not role:
                continue

            credit_key = (
                f"{recording_id}|"
                f"{artist_name}|"
                f"{role}|"
                f"{source_role}"
            )

            credit_id = (
                "CRD_"
                + hashlib.sha1(
                    credit_key.encode(
                        "utf-8"
                    )
                ).hexdigest()[:16]
            )

            credit_rows.append({

                "credit_id":
                    credit_id,

                "recording_id":
                    recording_id,

                "artist_name":
                    artist_name,

                "role":
                    role,

                "source_role":
                    source_role,

                "source":
                    "WhoSampled",

                "source_url":
                    source_url,
            })

    # --------------------------------------------------------
    # Relationships.
    #
    # Target recording identity is intentionally empty until
    # the related recording's own WhoSampled page is collected.
    # --------------------------------------------------------

    for row in relationship_rows:

        source_url = str(
            row.get(
                "source_url",
                "",
            )
            or ""
        ).strip().rstrip("/")

        source_recording_id = (
            recording_ids_by_url.get(
                source_url,
                ""
            )
        )

        relationship_key = (
            f"{source_recording_id}|"
            f"{row.get('relationship_type', '')}|"
            f"{row.get('related_track', '')}|"
            f"{row.get('related_artist', '')}|"
            f"{row.get('whosampled_relationship_url', '')}"
        )

        relationship_id = (
            "REL_"
            + hashlib.sha1(
                relationship_key.encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
        )

        normalized_relationship_rows.append({

            "relationship_id":
                relationship_id,

            "source_recording_id":
                source_recording_id,

            "target_recording_id":
                "",

            "relationship_type":
                row.get(
                    "relationship_type",
                    "",
                ),

            "related_track":
                row.get(
                    "related_track",
                    "",
                ),

            "related_artist":
                row.get(
                    "related_artist",
                    "",
                ),

            "year":
                row.get(
                    "year",
                    "",
                ),

            "whosampled_relationship_url":
                row.get(
                    "whosampled_relationship_url",
                    "",
                ),

            "detail":
                row.get(
                    "detail",
                    "",
                ),
        })

    recordings_df = pd.DataFrame(
        recording_rows
    )

    credits_df = pd.DataFrame(
        credit_rows
    )

    relationships_df = pd.DataFrame(
        normalized_relationship_rows
    )

    write_dataframe(
        recordings_df,
        RECORDINGS_FILE
    )

    write_dataframe(
        credits_df,
        CREDITS_FILE
    )

    write_dataframe(
        relationships_df,
        RELATIONSHIP_FILE
    )

    print()
    print("=" * 80)
    print("NORMALIZED OUTPUTS")
    print("=" * 80)

    print(
        "Recordings:",
        len(recordings_df),
    )

    print(
        "Credits:",
        len(credits_df),
    )

    print(
        "Relationships:",
        len(relationships_df),
    )



state = load_state()

if BLIND_PHASE1_CACHE:

    state = {
        "playlist_exported": True,
        "tracks_processed": [],
        "relationships_parsed": [],
        "spotify_enriched": False,
        "stopped_on_429": False,
        "secondary_pages_processed": [],
    }

    save_state(
        state
    )

# Shared WhoSampled identity caches.
artist_slug_cache = load_artist_slug_cache()
wikidata_cache = load_wikidata_cache()

if BLIND_PHASE1_CACHE:

    artist_slug_cache = {}

    print(
        "Learned artist slug cache: IGNORED FOR PHASE 1"
    )


# ============================================================
# STEP 1 — SPOTIFY PLAYLIST
# ============================================================

if not state["playlist_exported"]:

    print("\n" + "=" * 80)
    print("STEP 1 — SPOTIFY PLAYLIST")
    print("=" * 80)

    result = subprocess.run([
        sys.executable,
        "spotify_playlist_export.py",
        PLAYLIST_URL,
        "--output",
        str(SPOTIFY_FILE),
    ])

    if result.returncode != 0:
        raise SystemExit(
            "Spotify playlist export failed."
        )

    state["playlist_exported"] = True
    save_state(state)

else:
    print(
        "Spotify playlist already exported; "
        "reusing existing file."
    )


spotify = pd.read_csv(SPOTIFY_FILE)

print(
    "\nPlaylist tracks:",
    len(spotify)
)


# ============================================================
# STEP 2 — LOAD LOCAL WHOSAMPLED CACHE
# ============================================================

cache_file = Path(
    "kanye_whosampled_track_index.csv"
)

if cache_file.exists():

    cache = pd.read_csv(
        cache_file
    )

else:

    cache = pd.DataFrame(
        columns=[
            "track_artist",
            "track_title",
            "whosampled_track_url",
        ]
    )

if BLIND_PHASE1_CACHE:

    cache = pd.DataFrame(
        columns=[
            "track_artist",
            "track_title",
            "whosampled_track_url",
        ]
    )

    print(
        "Local WhoSampled cache: IGNORED FOR PHASE 1"
    )

else:

    print(
        "Local WhoSampled cache:",
        len(cache)
    )



def verify_loaded_page(page, expected_title, expected_artists):
    """
    Verify the WhoSampled page that is already loaded.

    This function NEVER calls page.goto().
    """

    from bs4 import BeautifulSoup

    try:
        html = page.content()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        source = extract_source_metadata(
            soup
        )

        actual_title = normalize(
            source.get(
                "source_title",
                ""
            )
        )

        actual_artists = [
            normalize(a)
            for a in str(
                source.get(
                    "source_artists",
                    ""
                )
            ).split(",")
            if str(a).strip()
        ]

        expected_title_norm = normalize(
            expected_title
        )

        expected_artist_norms = [
            normalize(a)
            for a in expected_artists
            if str(a).strip()
        ]

        title_match = (
            actual_title == expected_title_norm
        )

        artist_match = any(
            ea == aa
            or ea in aa
            or aa in ea
            for ea in expected_artist_norms
            for aa in actual_artists
        )

        canonical_url = source.get(
            "source_url"
        )

        if not canonical_url:
            return {
                "status": "review",
                "url": "",
                "source_title": source.get(
                    "source_title"
                ),
                "source_artists": source.get(
                    "source_artists"
                ),
            }

        # Reject obvious non-track relationship URLs.
        path = canonical_url.lower()

        relationship_markers = (
            "/sample/",
            "/cover/",
            "/interpolation/",
            "/remix/",
            "/search/",
        )

        if any(
            marker in path
            for marker in relationship_markers
        ):
            return {
                "status": "rejected",
                "url": canonical_url,
                "source_title": source.get(
                    "source_title"
                ),
                "source_artists": source.get(
                    "source_artists"
                ),
            }

        if title_match and artist_match:
            status = "matched"
        else:
            status = "review"

        return {
            "status": status,
            "url": canonical_url,
            "source_title": source.get(
                "source_title"
            ),
            "source_artists": source.get(
                "source_artists"
            ),
        }

    except Exception as e:

        print(
            "verify_loaded_page error:",
            repr(e)
        )

        return {
            "status": "review",
            "url": "",
            "source_title": "",
            "source_artists": "",
        }


# ============================================================
# STEP 3 — SPOTIFY → WHOSAMPLED
# ============================================================

if BLIND_PHASE1_CACHE:

    processed = set()
    matched_rows = []

    print(
        "BLIND PHASE 1:"
    )

    print(
        "  Existing track checkpoint: IGNORED"
    )

    print(
        "  Starting with 0 processed tracks"
    )

else:

    processed = set(
        state.get(
            "tracks_processed",
            []
        )
    )

    # Preserve previously completed Phase 1 results when resuming a
    # checkpointed run. Otherwise a run where every track is skipped
    # would overwrite matched_tracks.csv with an empty file.
    matched_rows = []

    if MATCH_FILE.exists():

        try:

            existing_matches_df = pd.read_csv(
                MATCH_FILE
            )

            if not existing_matches_df.empty:

                matched_rows = (
                    existing_matches_df
                    .to_dict("records")
                )

                print(
                    "RESTORED MATCH CHECKPOINT:",
                    len(matched_rows),
                    "existing track results"
                )

        except Exception as e:

            print(
                "MATCH CHECKPOINT RESTORE WARNING:",
                repr(e)
            )



def is_whosampled_artist_profile(url):
    """
    True for /Artist/ and false for /Artist/Track/.
    """

    if not url:
        return False

    path = (
        str(url)
        .split(
            "whosampled.com",
            1
        )[-1]
        .strip("/")
    )

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    return len(parts) == 1


def verify_cached_html_file(
    html_file,
    expected_title,
    expected_artists,
):
    """
    Verify cached WhoSampled HTML without making a network request.
    """

    from bs4 import BeautifulSoup

    try:

        soup = BeautifulSoup(
            Path(html_file).read_text(
                encoding="utf-8",
                errors="ignore"
            ),
            "html.parser"
        )

        source = extract_source_metadata(
            soup
        )

        source_url = (
            source.get(
                "source_url",
                ""
            )
            or ""
        )

        if not source_url:
            return {
                "status": "rejected",
                "url": "",
                "source_title": "",
                "source_artists": "",
            }

        path = (
            source_url
            .split(
                "whosampled.com",
                1
            )[-1]
            .strip("/")
        )

        parts = [
            part
            for part in path.split("/")
            if part
        ]

        if len(parts) != 2:
            return {
                "status": "rejected",
                "url": source_url,
                "source_title": source.get(
                    "source_title",
                    ""
                ),
                "source_artists": source.get(
                    "source_artists",
                    ""
                ),
            }

        actual_title = normalize(
            source.get(
                "source_title",
                ""
            )
        )

        actual_artists = [
            normalize(a)
            for a in str(
                source.get(
                    "source_artists",
                    ""
                )
            ).split(",")
            if str(a).strip()
        ]

        expected_title = normalize(
            expected_title
        )

        expected_artists = [
            normalize(a)
            for a in expected_artists
            if str(a).strip()
        ]

        title_match = (
            actual_title
            == expected_title
        )

        artist_match = any(
            expected_artist == actual_artist
            or expected_artist in actual_artist
            or actual_artist in expected_artist
            for expected_artist in expected_artists
            for actual_artist in actual_artists
        )

        return {
            "status": (
                "matched"
                if title_match and artist_match
                else "review"
            ),
            "url": source_url,
            "source_title": source.get(
                "source_title",
                ""
            ),
            "source_artists": source.get(
                "source_artists",
                ""
            ),
        }

    except Exception as e:

        print(
            "CACHED HTML VERIFICATION ERROR:",
            html_file,
            repr(e)
        )

        return {
            "status": "rejected",
            "url": "",
            "source_title": "",
            "source_artists": "",
        }


def find_local_page(title, artists):
    """
    Locate a potentially relevant archived page.
    Actual acceptance happens through verification.
    """

    from bs4 import BeautifulSoup

    archive_dir = Path(
        "whosampled_html_archive"
    )

    if not archive_dir.exists():
        return None, None

    target_title = normalize(
        title
    )

    target_artists = [
        normalize(a)
        for a in artists
        if str(a).strip()
    ]

    for html_file in archive_dir.glob(
        "*.html"
    ):

        try:

            soup = BeautifulSoup(
                html_file.read_text(
                    encoding="utf-8",
                    errors="ignore"
                ),
                "html.parser"
            )

            source = extract_source_metadata(
                soup
            )

            saved_title = normalize(
                source.get(
                    "source_title",
                    ""
                )
            )

            if saved_title != target_title:
                continue

            saved_artists = [
                normalize(a)
                for a in str(
                    source.get(
                        "source_artists",
                        ""
                    )
                ).split(",")
                if str(a).strip()
            ]

            if any(
                target_artist == saved_artist
                or target_artist in saved_artist
                or saved_artist in target_artist
                for target_artist in target_artists
                for saved_artist in saved_artists
            ):

                return (
                    html_file,
                    source.get(
                        "source_url"
                    )
                )

        except Exception as e:

            print(
                "LOCAL ARCHIVE ERROR:",
                html_file,
                repr(e)
            )

    return None, None


def learn_artist_slug(
    spotify_artists,
    verified_url,
    source_artists,
    cache,
):
    """
    Learn a canonical WhoSampled artist slug only when exactly
    one Spotify artist corresponds to the WhoSampled artist.
    """

    if not verified_url:
        return

    path = (
        str(verified_url)
        .split(
            "whosampled.com",
            1
        )[-1]
        .strip("/")
    )

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    if len(parts) != 2:
        return

    # Store the canonical WhoSampled slug in its raw Unicode form.
    # Example:
    #   /L%C3%B4-Borges/  ->  Lô-Borges
    canonical_slug = unquote(
        parts[0]
    )

    ws_artists = [
        normalize(a)
        for a in str(
            source_artists
            or ""
        ).split(",")
        if str(a).strip()
    ]

    if not ws_artists:
        print(
            "SLUG LEARNING SKIPPED:",
            "WhoSampled source artist metadata unavailable"
        )
        return

    matches = []

    for spotify_artist in spotify_artists:

        spotify_norm = normalize(
            spotify_artist
        )

        if any(
            spotify_norm == ws_artist
            or spotify_norm in ws_artist
            or ws_artist in spotify_norm
            for ws_artist in ws_artists
        ):

            matches.append(
                spotify_artist
            )

    if len(matches) != 1:

        print(
            "SLUG LEARNING SKIPPED:",
            "WhoSampled artist did not map uniquely",
            source_artists,
            "←",
            spotify_artists
        )

        return

    artist = matches[0]
    key = normalize(artist)

    current = cache.get(
        key,
        []
    )

    if isinstance(
        current,
        str
    ):
        current = [current]

    current = [
        str(x)
        for x in current
        if str(x).strip()
    ]

    updated = [canonical_slug]

    for slug in current:

        if normalize(slug) != normalize(
            canonical_slug
        ):

            updated.append(slug)

    if updated != current:

        cache[key] = updated

        save_artist_slug_cache(
            cache
        )

        print(
            "LEARNED WHO SAMPLED ARTIST SLUG:",
            artist,
            "→",
            canonical_slug
        )


def title_urls_for_slug(
    artist_slug,
    title,
):
    """
    Generate several plausible learned-slug title candidates.

    WhoSampled's canonical title slugs are not reliably reproducible
    with one universal punctuation rule, so candidate generation is
    deliberately inclusive. Actual acceptance is still determined by
    page-content verification.

    Order:
        1. Existing Unicode slugify candidate
        2. Unicode punctuation-preserving candidate
        3. Existing ASCII slugify candidate
        4. ASCII punctuation-preserving candidate
    """

    import re
    import unicodedata
    from urllib.parse import quote

    from whosampled_match import (
        slugify,
        ascii_slugify,
    )

    artist_slug = unquote(
        str(artist_slug)
    ).strip("/")

    title = str(title).strip()

    candidates = []
    seen = set()

    def add_candidate(title_slug, label):

        if not title_slug:
            return

        url = (
            "https://www.whosampled.com/"
            + artist_slug
            + "/"
            + quote(
                title_slug,
                safe="-"
            )
            + "/"
        )

        if url not in seen:

            seen.add(url)

            candidates.append({
                "label": label,
                "url": url,
            })

    # 1. Existing Unicode slugifier.
    add_candidate(
        slugify(title),
        "unicode_slugify"
    )

    # 2. Preserve Unicode punctuation while converting spaces
    #    to hyphens.
    unicode_punctuation = unicodedata.normalize(
        "NFC",
        title
    )

    unicode_punctuation = re.sub(
        r"\s+",
        "-",
        unicode_punctuation
    )

    unicode_punctuation = re.sub(
        r"-+",
        "-",
        unicode_punctuation
    ).strip("-")

    add_candidate(
        unicode_punctuation,
        "unicode_punctuation_preserved"
    )

    # 3. Existing ASCII slugifier.
    add_candidate(
        ascii_slugify(title),
        "ascii_slugify"
    )

    # 4. Preserve ASCII punctuation while transliterating
    #    accented characters.
    ascii_punctuation = (
        unicodedata.normalize(
            "NFKD",
            title
        )
        .encode(
            "ascii",
            "ignore"
        )
        .decode(
            "ascii"
        )
    )

    ascii_punctuation = re.sub(
        r"\s+",
        "-",
        ascii_punctuation
    )

    ascii_punctuation = re.sub(
        r"-+",
        "-",
        ascii_punctuation
    ).strip("-")

    add_candidate(
        ascii_punctuation,
        "ascii_punctuation_preserved"
    )

    print(
        "TITLE URL CANDIDATES:",
        title
    )

    for item in candidates:
        print(
            "  ",
            item["label"],
            "→",
            item["url"]
        )

    return [
        item["url"]
        for item in candidates
    ]



def discover_track_url_from_artist_page(
    page,
    artist_slug,
    title,
):
    """
    Ask the known WhoSampled artist page for the exact canonical
    track URL instead of assuming a universal title-slug rule.

    Returns:
        canonical track URL or None
    """

    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    artist_slug = unquote(
        str(artist_slug)
    ).strip("/")

    if not artist_slug:
        return None

    artist_url = (
        "https://www.whosampled.com/"
        + artist_slug
        + "/"
    )

    print()
    print(
        "ARTIST PAGE DISCOVERY:",
        artist_url
    )

    try:

        response = page.goto(
            artist_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except Exception as e:

        print(
            "ARTIST PAGE DISCOVERY ERROR:",
            repr(e)
        )

        return None

    status = (
        response.status
        if response
        else None
    )

    print(
        "ARTIST PAGE DISCOVERY STATUS:",
        status
    )

    if status == 429:

        state_message = (
            "Stopped safely on HTTP 429 "
            "while discovering artist track URL."
        )

        raise SystemExit(
            state_message
        )

    print(
        "Waiting 12 seconds "
        "before the next WhoSampled request..."
    )

    time.sleep(12)

    if status != 200:
        return None

    soup = BeautifulSoup(
        page.content(),
        "html.parser"
    )

    target_title = normalize(
        title
    )

    exact_matches = []
    fallback_matches = []

    for link in soup.select(
        "a[href]"
    ):

        href = (
            link.get(
                "href",
                ""
            )
            or ""
        ).strip()

        if not href:
            continue

        absolute_url = urljoin(
            artist_url,
            href
        )

        if not absolute_url.startswith(
            "https://www.whosampled.com/"
        ):
            continue

        if not is_canonical_track_url(
            absolute_url
        ):
            continue

        link_text = normalize(
            link.get_text(
                " ",
                strip=True
            )
        )

        if not link_text:
            continue

        if link_text == target_title:

            exact_matches.append(
                absolute_url
            )

        elif (
            target_title in link_text
            or link_text in target_title
        ):

            fallback_matches.append(
                absolute_url
            )

    # Exact title match is preferred.
    if exact_matches:

        url = exact_matches[0]

        print(
            "CANONICAL TRACK LINK DISCOVERED:",
            url
        )

        return url

    # Then allow a conservative normalized containment match.
    if fallback_matches:

        url = fallback_matches[0]

        print(
            "POTENTIAL TRACK LINK DISCOVERED:",
            url
        )

        return url

    print(
        "ARTIST PAGE DID NOT CONTAIN TRACK:",
        title
    )

    return None


def save_verified_track_html(
    page,
    title,
):
    """
    Save a verified/reviewed WhoSampled page to:

        1. the current run's HTML archive
        2. the persistent HTML archive
        3. the current run's local artwork cache
        4. the persistent artwork cache

    Artwork capture is opportunistic and uses the already-loaded
    Playwright page. It does not issue a second WhoSampled request.
    """

    html = page.content()

    filename = (
        safe_filename(
            title
        )
        + ".html"
    )

    # --------------------------------------------------------
    # HTML ARCHIVES
    # --------------------------------------------------------

    run_html = (
        HTML_DIR / filename
    )

    archive_dir = Path(
        "whosampled_html_archive"
    )

    archive_dir.mkdir(
        exist_ok=True
    )

    archive_html = (
        archive_dir / filename
    )

    run_html.write_text(
        html,
        encoding="utf-8"
    )

    archive_html.write_text(
        html,
        encoding="utf-8"
    )

    print(
        "HTML SAVED:",
        run_html
    )

    # --------------------------------------------------------
    # ARTWORK ARCHIVE
    # --------------------------------------------------------
    #
    # This uses the existing Playwright page. The helper tries
    # to capture the rendered image rather than asking the
    # browser later to load the remote WhoSampled image URL.
    # --------------------------------------------------------

    run_media_dir = (
        HTML_DIR.parent
        / "whosampled_media"
    )

    persistent_media_dir = Path(
        "whosampled_media"
    )

    run_media_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    persistent_media_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        artwork = capture_rendered_artwork(
            page=page,
            title=title,
            output_dir=run_media_dir,
        )

    except Exception as exc:

        print(
            "WHO SAMPLED ARTWORK CAPTURE ERROR:",
            repr(exc)
        )

        artwork = {
            "whosampled_thumbnail_url": "",
            "whosampled_thumbnail_path": "",
            "whosampled_thumbnail_status":
                "unavailable",
        }

    run_thumbnail = (
        artwork.get(
            "whosampled_thumbnail_path",
            ""
        )
    )

    if run_thumbnail:

        run_thumbnail_path = Path(
            run_thumbnail
        )

        persistent_thumbnail = (
            persistent_media_dir
            / run_thumbnail_path.name
        )

        try:

            persistent_thumbnail.write_bytes(
                run_thumbnail_path.read_bytes()
            )

            print(
                "WHO SAMPLED ARTWORK SAVED:",
                run_thumbnail_path
            )

            print(
                "WHO SAMPLED ARTWORK ARCHIVED:",
                persistent_thumbnail
            )

            artwork[
                "persistent_thumbnail_path"
            ] = str(
                persistent_thumbnail
            )

        except Exception as exc:

            print(
                "WHO SAMPLED ARTWORK ARCHIVE ERROR:",
                repr(exc)
            )

            artwork[
                "persistent_thumbnail_path"
            ] = ""

    else:

        print(
            "WHO SAMPLED ARTWORK:",
            "unavailable"
        )

        artwork[
            "persistent_thumbnail_path"
        ] = ""

    return {
        "html_path":
            str(run_html),

        "persistent_html_path":
            str(archive_html),

        "thumbnail_path":
            artwork.get(
                "whosampled_thumbnail_path",
                "",
            ),

        "persistent_thumbnail_path":
            artwork.get(
                "persistent_thumbnail_path",
                "",
            ),

        "thumbnail_url":
            artwork.get(
                "whosampled_thumbnail_url",
                "",
            ),

        "thumbnail_status":
            artwork.get(
                "whosampled_thumbnail_status",
                "unavailable",
            ),
    }


def request_whosampled_candidate(
    page,
    candidate_urls,
    title,
    expected_artists,
    source,
    state,
    processed,
    matched_rows,
):
    """
    Try URL variants for ONE artist identity.

    artist profile = stop this identity
    review track = retain and stop this identity
    matched = accept
    404 = continue
    """

    artist_profile_result = None

    for candidate_url in candidate_urls:

        print()
        print(
            "WHO SAMPLED REQUEST:",
            candidate_url
        )

        print(
            "RESOLUTION SOURCE:",
            source
        )

        try:

            response = page.goto(
                candidate_url,
                wait_until="domcontentloaded",
                timeout=60000
            )

        except Exception as e:

            print(
                "REQUEST ERROR:",
                repr(e)
            )

            continue

        status = (
            response.status
            if response
            else None
        )

        print(
            "STATUS:",
            status
        )

        if status == 429:

            print(
                "\nWHO SAMPLED RATE LIMITED."
            )

            state[
                "stopped_on_429"
            ] = True

            state[
                "tracks_processed"
            ] = sorted(
                processed
            )

            save_state(
                state
            )

            # IMPORTANT:
            # Preserve the last valid matched_tracks.csv checkpoint.
            # On a rate limit, matched_rows may contain only the
            # current in-memory batch (or nothing at all). Writing it
            # here can destroy a previously valid checkpoint.
            #
            # The state checkpoint above is still saved, and the
            # existing MATCH_FILE is intentionally left untouched.

            if MATCH_FILE.exists():
                print(
                    "Existing matched_tracks.csv preserved after 429."
                )
            else:
                print(
                    "No existing matched_tracks.csv checkpoint "
                    "available after 429."
                )

            raise SystemExit(
                "Stopped safely on HTTP 429."
            )

        print(
            "Waiting 12 seconds "
            "before the next WhoSampled request..."
        )

        time.sleep(12)

        if status != 200:
            continue

        try:

            verified = verify_loaded_page(
                page,
                title,
                expected_artists
            )

        except Exception as e:

            print(
                "Verification error:",
                repr(e)
            )

            verified = {
                "status": "rejected",
                "url": "",
                "source_title": "",
                "source_artists": "",
            }

        verified_status = verified.get(
            "status",
            "rejected"
        )

        verified_url = (
            verified.get(
                "url"
            )
            or ""
        )

        print(
            "VERIFICATION:",
            verified_status,
            verified_url
        )

        print(
            "BROWSER FINAL URL:",
            page.url
        )

        if verified_status == "matched":

            return (
                "matched",
                verified
            )

        if (
            verified_status != "rejected"
            and verified_url
            and is_whosampled_artist_profile(
                verified_url
            )
        ):

            print(
                "WHO SAMPLED ARTIST PROFILE ONLY:",
                verified_url
            )

            # This candidate landed on the known artist profile,
            # but there may be another title candidate that points
            # to the actual track page. Remember the artist identity
            # result, then continue through the remaining candidates.
            artist_profile_result = (
                "artist_profile",
                verified
            )

            continue

        if verified_status == "review":

            print(
                "TRACK REVIEW CANDIDATE:",
                verified_url or "(canonical URL unavailable)"
            )

            return (
                "review",
                verified
            )

        print(
            "PAGE REJECTED:",
            verified_url or "(canonical URL unavailable)"
        )

    if artist_profile_result is not None:

        return artist_profile_result

    return (
        "not_found",
        None
    )


with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    context = browser.new_context(
        viewport={
            "width": 1440,
            "height": 900
        }
    )

    page = context.new_page()

    for _, row in spotify.iterrows():

        spotify_id = str(
            row["spotify_track_id"]
        )

        if spotify_id in processed:

            print(
                "SKIPPED TRACK:",
                row["title"],
                "— already processed in checkpoint."
            )

            continue

        title = str(
            row["title"]
        ).strip()

        artists = artist_list(
            row["artist_names"]
        )

        print()
        print(
            "=" * 80
        )

        print(
            "TRACK:",
            title,
            "—",
            row["artist_names"]
        )

        print(
            "=" * 80
        )

        ws_url = None
        match_method = None
        match_status = "unresolved"
        matched_source_artists = ""

        # ----------------------------------------------------
        # 1. STRUCTURED TRACK CACHE
        # ----------------------------------------------------

        title_norm = normalize(
            title
        )

        candidates = cache[
            cache["track_title"]
            .fillna("")
            .map(normalize)
            == title_norm
        ].copy()

        if (
            BLIND_PHASE1_CACHE
            and not candidates.empty
        ):

            raise RuntimeError(
                "Blind Phase 1 mode unexpectedly found "
                "structured cache candidates."
            )

        if not candidates.empty:

            print(
                "STRUCTURED CACHE CANDIDATES:",
                len(candidates)
            )

            artist_norms = [
                normalize(a)
                for a in artists
            ]

            cached_url = None

            for _, candidate in candidates.iterrows():

                ws_artist = normalize(
                    candidate["track_artist"]
                )

                if any(
                    ws_artist in artist
                    or artist in ws_artist
                    for artist in artist_norms
                ):

                    cached_url = candidate[
                        "whosampled_track_url"
                    ]

                    break

            if cached_url is None:

                cached_url = candidates.iloc[0][
                    "whosampled_track_url"
                ]

            cached_file = None

            archive_dir = Path(
                "whosampled_html_archive"
            )

            if archive_dir.exists():

                from bs4 import BeautifulSoup

                for candidate_file in (
                    archive_dir.glob("*.html")
                ):

                    try:

                        soup = BeautifulSoup(
                            candidate_file.read_text(
                                encoding="utf-8",
                                errors="ignore"
                            ),
                            "html.parser"
                        )

                        source = (
                            extract_source_metadata(
                                soup
                            )
                        )

                        if (
                            source.get(
                                "source_url"
                            )
                            == cached_url
                        ):

                            cached_file = (
                                candidate_file
                            )

                            break

                    except Exception:
                        continue

            if cached_file is not None:

                verified = (
                    verify_cached_html_file(
                        cached_file,
                        title,
                        artists
                    )
                )

                print(
                    "STRUCTURED CACHE VERIFICATION:",
                    verified.get(
                        "status"
                    ),
                    verified.get(
                        "url"
                    )
                )

                if verified.get(
                    "status"
                ) == "matched":

                    ws_url = verified.get(
                        "url"
                    )

                    matched_source_artists = (
                        verified.get(
                            "source_artists",
                            ""
                        )
                    )

                    match_method = (
                        "track_cache"
                    )

                    match_status = (
                        "matched"
                    )

                    print(
                        "SKIPPED WHO SAMPLED REQUEST:"
                    )

                    print(
                        "  SOURCE: structured track cache"
                    )

                    print(
                        "  REASON: archived HTML verified"
                    )

                    print(
                        "  URL:",
                        ws_url
                    )

                else:

                    print(
                        "STRUCTURED CACHE REJECTED:",
                        cached_url
                    )

            else:

                print(
                    "STRUCTURED CACHE HAS NO "
                    "VERIFIABLE HTML:",
                    cached_url
                )

        # ----------------------------------------------------
        # 2. PERSISTENT HTML ARCHIVE
        # ----------------------------------------------------

        if (
            ws_url is None
            and not BLIND_PHASE1_CACHE
        ):

            local_file, local_url = (
                find_local_page(
                    title,
                    artists
                )
            )

            if local_file is not None:

                verified = (
                    verify_cached_html_file(
                        local_file,
                        title,
                        artists
                    )
                )

                print(
                    "LOCAL HTML CACHE VERIFICATION:",
                    verified.get(
                        "status"
                    ),
                    verified.get(
                        "url"
                    )
                )

                if verified.get(
                    "status"
                ) == "matched":

                    ws_url = verified.get(
                        "url"
                    )

                    matched_source_artists = (
                        verified.get(
                            "source_artists",
                            ""
                        )
                    )

                    match_method = (
                        "html_cache"
                    )

                    match_status = (
                        "matched"
                    )

                    print(
                        "SKIPPED WHO SAMPLED REQUEST:"
                    )

                    print(
                        "  SOURCE: persistent HTML archive"
                    )

                    print(
                        "  REASON: cached page verified locally"
                    )

                    print(
                        "  FILE:",
                        local_file
                    )

                    print(
                        "  URL:",
                        ws_url
                    )

                    run_html = (
                        HTML_DIR
                        / local_file.name
                    )

                    run_html.write_text(
                        local_file.read_text(
                            encoding="utf-8",
                            errors="ignore"
                        ),
                        encoding="utf-8"
                    )

                else:

                    print(
                        "LOCAL HTML CACHE REJECTED:",
                        local_file
                    )

        # ----------------------------------------------------
        # 3. NETWORK RESOLUTION
        # ----------------------------------------------------

        if ws_url is None:

            print(
                "NO VERIFIED CACHE HIT."
            )

            print(
                "Proceeding to live WhoSampled resolution."
            )

            review_candidate = None

            for spotify_artist in artists:

                artist_key = normalize(
                    spotify_artist
                )

                # --------------------------------------------
                # A. LEARNED ARTIST SLUG → DIRECT TRACK URL
                #
                # The artist slug has already been verified from a
                # previous successful WhoSampled track match.
                #
                # Try the Unicode title candidate first.
                # Only after it fails do we try the ASCII title.
                #
                # Artist-page pagination is intentionally NOT done
                # here. That happens later in the separate artist
                # enrichment phase.
                # --------------------------------------------

                learned_slugs = (
                    artist_slug_cache.get(
                        artist_key,
                        []
                    )
                )

                if isinstance(
                    learned_slugs,
                    str
                ):

                    learned_slugs = [
                        learned_slugs
                    ]

                learned_slugs = [
                    str(slug)
                    for slug in learned_slugs
                    if str(slug).strip()
                ]

                if learned_slugs:

                    print(
                        "ARTIST SLUG CACHE HIT:",
                        spotify_artist,
                        "→",
                        learned_slugs
                    )

                    for learned_slug in learned_slugs:

                        print(
                            "LEARNED SLUG DIRECT RESOLUTION:",
                            learned_slug
                        )

                        result_status, verified = (
                            request_whosampled_candidate(
                                page,
                                title_urls_for_slug(
                                    learned_slug,
                                    title
                                ),
                                title,
                                artists,
                                "learned_slug",
                                state,
                                processed,
                                matched_rows,
                            )
                        )

                        if result_status == "matched":

                            ws_url = verified.get(
                                "url"
                            ) or page.url

                            matched_source_artists = (
                                verified.get(
                                    "source_artists",
                                    ""
                                )
                            )

                            match_method = (
                                "learned_slug"
                            )

                            match_status = (
                                "matched"
                            )

                            break

                        if result_status == "review":

                            if (
                                review_candidate
                                is None
                            ):

                                review_candidate = (
                                    verified
                                )

                            break

                        if (
                            result_status
                            == "artist_profile"
                        ):

                            print(
                                "LEARNED SLUG RETURNED ARTIST PROFILE:",
                                learned_slug
                            )

                            # The artist identity is already known.
                            # Do not search Wikidata aliases for it.
                            # Continue to the next learned slug, if any.
                            continue

                    if ws_url is not None:
                        break

                    if review_candidate is not None:
                        break

                    # A learned slug is authoritative for this artist.
                    # Do not fall back to the same artist's Spotify name
                    # or Wikidata aliases during this pass.
                    continue

                # --------------------------------------------
                # B. SPOTIFY ARTIST NAME
                # --------------------------------------------

                result_status, verified = (
                    request_whosampled_candidate(
                        page,
                        canonical_url_variants(
                            spotify_artist,
                            title
                        ),
                        title,
                        artists,
                        "spotify_name",
                        state,
                        processed,
                        matched_rows,
                    )
                )

                if result_status == "matched":

                    ws_url = verified.get(
                        "url"
                    ) or page.url

                    matched_source_artists = (
                        verified.get(
                            "source_artists",
                            ""
                        )
                    )

                    match_method = "direct"
                    match_status = "matched"

                    break

                if result_status == "review":

                    if (
                        review_candidate
                        is None
                    ):

                        review_candidate = (
                            verified
                        )

                    break

                if (
                    result_status
                    == "artist_profile"
                ):

                    # WhoSampled already recognizes the artist.
                    # Do not try Wikidata aliases for this identity.
                    continue

                # --------------------------------------------
                # C. WIKIDATA
                #
                # Canonical first.
                # If canonical completely fails, top 3 aliases.
                # --------------------------------------------

                try:

                    wd = (
                        wikidata_artist_resolution(
                            spotify_artist,
                            wikidata_cache
                        )
                    )

                except Exception as e:

                    print(
                        "WIKIDATA ERROR:",
                        repr(e)
                    )

                    wd = None

                if not wd:

                    continue

                print(
                    "WIKIDATA RESULT:",
                    wd.get(
                        "status"
                    ),
                    wd.get(
                        "label"
                    )
                )

                if (
                    wd.get(
                        "status"
                    )
                    != "resolved"
                ):

                    continue

                canonical_name = str(
                    wd.get(
                        "label",
                        ""
                    )
                    or ""
                ).strip()

                wikidata_candidates = []

                if canonical_name:

                    wikidata_candidates.append(
                        (
                            canonical_name,
                            "wikidata_canonical"
                        )
                    )

                # Only top 3 aliases.
                seen = {
                    normalize(
                        canonical_name
                    )
                }

                alias_count = 0

                for alias in wd.get(
                    "aliases",
                    []
                ):

                    if alias_count >= 3:
                        break

                    alias = str(
                        alias
                        or ""
                    ).strip()

                    alias_key = normalize(
                        alias
                    )

                    if (
                        not alias_key
                        or alias_key in seen
                    ):
                        continue

                    seen.add(
                        alias_key
                    )

                    wikidata_candidates.append(
                        (
                            alias,
                            "wikidata_alias"
                        )
                    )

                    alias_count += 1

                for (
                    candidate_name,
                    candidate_source,
                ) in wikidata_candidates:

                    print(
                        "WIKIDATA SLUG ATTEMPT:",
                        candidate_name,
                        "(",
                        candidate_source,
                        ")"
                    )

                    result_status, verified = (
                        request_whosampled_candidate(
                            page,
                            canonical_url_variants(
                                candidate_name,
                                title
                            ),
                            title,
                            artists,
                            candidate_source,
                            state,
                            processed,
                            matched_rows,
                        )
                    )

                    if result_status == "matched":

                        ws_url = verified.get(
                            "url"
                        ) or page.url

                        matched_source_artists = (
                            verified.get(
                                "source_artists",
                                ""
                            )
                        )

                        match_method = "wikidata"
                        match_status = "matched"

                        break

                    if result_status == "review":

                        review_candidate = (
                            review_candidate
                            or verified
                        )

                        break

                    if (
                        result_status
                        == "artist_profile"
                    ):

                        # Canonical WhoSampled artist identity has
                        # been established. Stop alias expansion.
                        break

                if ws_url is not None:
                    break

            # ------------------------------------------------
            # Final result for unresolved/review.
            # ------------------------------------------------

            if (
                ws_url is None
                and review_candidate is not None
            ):

                ws_url = review_candidate.get(
                    "url",
                    ""
                )

                matched_source_artists = (
                    review_candidate.get(
                        "source_artists",
                        ""
                    )
                )

                match_method = "review"
                match_status = "review"

                print(
                    "PAGE SAVED FOR REVIEW:",
                    ws_url
                )

                save_verified_track_html(
                    page,
                    title
                )

            elif ws_url is None:

                match_status = "unresolved"

                print(
                    "UNRESOLVED AFTER DIRECT + WIKIDATA RESOLUTION"
                )

        # ----------------------------------------------------
        # Learn slug ONCE.
        # ----------------------------------------------------

        if (
            ws_url
            and match_status == "matched"
        ):

            learn_artist_slug(
                artists,
                ws_url,
                matched_source_artists,
                artist_slug_cache
            )

        # ----------------------------------------------------
        # Save track-level checkpoint.
        # ----------------------------------------------------

        matched_rows.append({
            "spotify_track_id":
                row["spotify_track_id"],
            "isrc":
                row["isrc"],
            "spotify_title":
                title,
            "spotify_artists":
                row["artist_names"],
            "whosampled_url":
                ws_url,
            "match_method":
                match_method,
            "match_status":
                match_status,
        })

        processed.add(
            spotify_id
        )

        state[
            "tracks_processed"
        ] = sorted(
            processed
        )

        save_state(
            state
        )

    browser.close()


matched_df = pd.DataFrame(
    matched_rows
)

write_dataframe(
    matched_df,
    MATCH_FILE
)

print()
print(
    "WhoSampled track matching complete."
)

if not matched_df.empty:

    print(
        "Matched:",
        (
            matched_df["match_status"]
            == "matched"
        ).sum()
    )

    print(
        "Review:",
        (
            matched_df["match_status"]
            == "review"
        ).sum()
    )

    print(
        "Unresolved:",
        (
            matched_df["match_status"]
            == "unresolved"
        ).sum()
    )

# ============================================================
# ============================================================
# ============================================================
# WHO SAMPLED CONTRIBUTOR REVIEW GATE
# ============================================================
#
# Phase 1 candidates classified as "review" must be explicitly
# accepted, rejected, or left unresolved before Step 4 can treat
# their WhoSampled page as graph evidence.
#
# The review UI updates matched_tracks.csv. Step 4 then reads that
# updated file, so rejected and unresolved pages remain excluded.
# ============================================================

print()
print("=" * 80)
print("WHO SAMPLED CONTRIBUTOR REVIEW")
print("=" * 80)

matched_pre_review = pd.read_csv(
    MATCH_FILE
)

review_count = int(
    matched_pre_review["match_status"]
    .fillna("")
    .eq("review")
    .sum()
)

if review_count:

    print(
        "WhoSampled review candidates:",
        review_count,
    )

    matched_df = run_whosampled_review(
        MATCH_FILE,
        WHO_SAMPLED_REVIEW_FILE,
    )

    state[
        "whosampled_review_complete"
    ] = True

    save_state(
        state
    )

else:

    print(
        "No WhoSampled review candidates."
    )

print()

# STEP 4 — PARSE SAVED HTML + SECONDARY RELATIONSHIP PAGES
# ============================================================

print()
print("=" * 80)
print("STEP 4 — RELATIONSHIP EXTRACTION")
print("=" * 80)

from bs4 import BeautifulSoup

from parse_whosampled_track import (
    extract_source_metadata,
    extract_relationships,
    extract_secondary_pages,
)


def secondary_archive_file(url):
    """Return the persistent archive filename for a secondary URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)

    parts = [
        part
        for part in parsed.path.strip("/").split("/")
        if part
    ]

    if not parts:
        return None

    filename = "-".join(
        safe_filename(part)
        for part in parts
    ) + ".html"

    return Path(
        "whosampled_html_archive"
    ) / filename


def load_html_file(path):
    return BeautifulSoup(
        Path(path).read_text(
            encoding="utf-8",
            errors="ignore"
        ),
        "html.parser"
    )


def normalize_secondary_relationship(row, primary_source):
    """Preserve the original track as the relationship source."""
    result = dict(row)

    for key in (
        "source_title",
        "source_url",
        "source_artists",
        "source_producers",
        "source_album",
        "source_label",
        "source_release_year",
    ):
        result[key] = primary_source.get(
            key,
            ""
        )

    return result


all_relationship_rows = []

processed_secondary = set(
    state.get(
        "secondary_pages_processed",
        []
    )
)

primary_html_files = sorted(
    HTML_DIR.glob("*.html")
)

# ------------------------------------------------------------
# Step 4 only extracts relationships from Phase 1 tracks that
# were explicitly verified as MATCHED. Review/unresolved pages
# remain archived for human review but are not graph evidence.
# ------------------------------------------------------------
approved_match_urls = set()

if MATCH_FILE.exists():

    try:

        matched_source_df = pd.read_csv(
            MATCH_FILE
        )

        for _, match_row in matched_source_df.iterrows():

            if (
                str(
                    match_row.get(
                        "match_status",
                        ""
                    )
                ).strip().lower()
                == "matched"
            ):

                matched_url = str(
                    match_row.get(
                        "whosampled_url",
                        ""
                    )
                    or ""
                ).strip()

                if matched_url:
                    approved_match_urls.add(
                        matched_url.rstrip("/")
                    )

        print(
            "STEP 4 APPROVED MATCHED TRACKS:",
            len(approved_match_urls)
        )

    except Exception as e:

        raise SystemExit(
            "Could not read matched_tracks.csv for Step 4 filtering: "
            + repr(e)
        )

else:

    raise SystemExit(
        "matched_tracks.csv is required for Step 4 filtering."
    )

accepted_primary_html_files = []

# ------------------------------------------------------------
# STEP 4 owns its own Playwright browser.
# STEP 3 has already closed its browser by this point.
# ------------------------------------------------------------

secondary_playwright = None
secondary_browser = None
secondary_page = None

for html_file in primary_html_files:

    if html_file.name.startswith(
        "secondary_"
    ):
        continue

    print()
    print(
        "Parsing primary:",
        html_file.name
    )

    try:

        soup = load_html_file(
            html_file
        )

        primary_source = (
            extract_source_metadata(
                soup
            )
        )

        primary_source_url = str(
            primary_source.get(
                "source_url",
                ""
            )
            or ""
        ).strip().rstrip("/")

        if primary_source_url not in approved_match_urls:

            print(
                "  SKIPPED PRIMARY PAGE:"
            )

            print(
                "    REASON: Phase 1 status was not matched"
            )

            print(
                "    SOURCE URL:",
                primary_source_url
            )

            continue

        accepted_primary_html_files.append(
            html_file.name
        )

        print(
            "  APPROVED PRIMARY PAGE: Phase 1 matched"
        )

        primary_relationships = (
            extract_relationships(
                soup,
                primary_source
            )
        )

        print(
            "  Primary relationships:",
            len(primary_relationships)
        )

        all_relationship_rows.extend(
            primary_relationships
        )

        secondary_pages = (
            extract_secondary_pages(
                soup
            )
        )

        print(
            "  Secondary pages discovered:",
            len(secondary_pages)
        )

        for secondary in secondary_pages:

            secondary_url = secondary["url"]

            archive_file = (
                secondary_archive_file(
                    secondary_url
                )
            )

            secondary_file = None

            # -----------------------------------------------
            # Persistent secondary-page cache.
            # -----------------------------------------------

            if (
                archive_file
                and archive_file.exists()
            ):

                secondary_file = archive_file

                print(
                    "  SECONDARY CACHE HIT:",
                    secondary_file
                )

            # -----------------------------------------------

                # Materialize persistent cache into the current run.
                run_secondary = (
                    HTML_DIR
                    / (
                        "secondary_"
                        + archive_file.name
                    )
                )

                if not run_secondary.exists():
                    run_secondary.write_text(
                        archive_file.read_text(
                            encoding="utf-8",
                            errors="ignore"
                        ),
                        encoding="utf-8"
                    )
                    print(
                        "  SECONDARY RUN CACHE CREATED:",
                        run_secondary
                    )

            # Current run cache fallback.
            # -----------------------------------------------

            if secondary_file is None:

                for candidate in (
                    HTML_DIR.glob(
                        "secondary_*.html"
                    )
                ):

                    try:

                        candidate_soup = (
                            load_html_file(
                                candidate
                            )
                        )

                        candidate_source = (
                            extract_source_metadata(
                                candidate_soup
                            )
                        )

                        if (
                            candidate_source.get(
                                "source_url"
                            )
                            == secondary_url
                        ):

                            secondary_file = candidate
                            break

                    except Exception:
                        continue

            # -----------------------------------------------
            # Network acquisition only if uncached.
            # -----------------------------------------------

            if secondary_file is None:

                if secondary_url in processed_secondary:
                    continue

                print(
                    "  SECONDARY REQUEST:",
                    secondary_url
                )

                try:

                    if secondary_page is None:

                        secondary_playwright = (
                            sync_playwright().start()
                        )

                        secondary_browser = (
                            secondary_playwright.chromium.launch(
                                headless=False
                            )
                        )

                        secondary_page = (
                            secondary_browser.new_page()
                        )

                        print(
                            "  Started dedicated "
                            "secondary-page browser."
                        )

                    response = secondary_page.goto(
                        secondary_url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )

                except Exception as e:

                    print(
                        "  SECONDARY REQUEST ERROR:",
                        repr(e)
                    )
                    continue

                status = (
                    response.status
                    if response
                    else None
                )

                print(
                    "  SECONDARY STATUS:",
                    status
                )

                if status == 429:

                    state[
                        "stopped_on_429"
                    ] = True

                    save_state(state)

                    raise SystemExit(
                        "Stopped safely on HTTP 429."
                    )

                delay = 12

                print(
                    "  Waiting 12 seconds "
                    "before next WhoSampled request..."
                )

                time.sleep(
                    delay
                )

                if status != 200:
                    continue

                html = secondary_page.content()

                if archive_file is None:
                    continue

                archive_file.parent.mkdir(
                    parents=True,
                    exist_ok=True
                )

                archive_file.write_text(
                    html,
                    encoding="utf-8"
                )

                secondary_file = archive_file

                run_secondary = (
                    HTML_DIR
                    / (
                        "secondary_"
                        + archive_file.name
                    )
                )

                run_secondary.write_text(
                    html,
                    encoding="utf-8"
                )

                print(
                    "  SECONDARY HTML SAVED:",
                    archive_file
                )

            # -----------------------------------------------
            # Parse the secondary page locally.
            # -----------------------------------------------

            try:

                secondary_soup = (
                    load_html_file(
                        secondary_file
                    )
                )

                secondary_source = (
                    extract_source_metadata(
                        secondary_soup
                    )
                )

                secondary_relationships = (
                    extract_relationships(
                        secondary_soup,
                        secondary_source
                    )
                )

                print(
                    "  Secondary relationships:",
                    len(
                        secondary_relationships
                    )
                )

                for row in secondary_relationships:

                    all_relationship_rows.append(
                        normalize_secondary_relationship(
                            row,
                            primary_source
                        )
                    )

                processed_secondary.add(
                    secondary_url
                )

                state[
                    "secondary_pages_processed"
                ] = sorted(
                    processed_secondary
                )

                save_state(state)

            except Exception as e:

                print(
                    "  SECONDARY PARSE ERROR:",
                    repr(e)
                )


    except Exception as e:

        print(
            "PRIMARY PARSE ERROR:",
            html_file,
            repr(e)
        )



# ------------------------------------------------------------
# Close the dedicated Step-4 browser.
# ------------------------------------------------------------

if secondary_browser is not None:

    secondary_browser.close()

if secondary_playwright is not None:

    secondary_playwright.stop()


# ------------------------------------------------------------
# Deduplicate primary + secondary relationships.
# ------------------------------------------------------------

unique = {}

for row in all_relationship_rows:

    key = (
        row.get(
            "relationship_type",
            ""
        ),
        row.get(
            "related_track",
            ""
        ),
        row.get(
            "related_artist",
            ""
        ),
        row.get(
            "whosampled_relationship_url",
            ""
        ),
    )

    unique[key] = row


relationships_df = pd.DataFrame(
    list(unique.values())
)

# ============================================================
# NORMALIZED CANONICAL OUTPUTS
# ============================================================
#
# These files separate:
#
#   recordings.csv
#       one row per recording
#
#   credits.csv
#       one row per artist/role credit
#
#   relationships.csv
#       one row per graph relationship
#
# The older relationship table remains available above as an
# intermediate/compatibility artifact. These normalized files
# are the foundation for the eventual Neo4j model.
# ============================================================

recording_rows = []
credit_rows = []
recording_ids_by_url = {}

# Use the approved primary pages rather than relationship rows
# so a recording with four relationships is still represented
# only once.
for html_name in accepted_primary_html_files:

    html_path = (
        HTML_DIR / html_name
    )

    try:

        soup = load_html_file(
            html_path
        )

        source = extract_source_metadata(
            soup
        )

    except Exception as exc:

        print(
            "NORMALIZED RECORDING PARSE ERROR:",
            html_path,
            repr(exc)
        )
        continue

    source_url = str(
        source.get(
            "source_url",
            ""
        )
        or ""
    ).strip()

    if not source_url:
        continue

    source_url = source_url.rstrip("/")

    # Stable internal recording ID based on the source's
    # canonical WhoSampled URL.
    recording_id = (
        "REC_"
        + hashlib.sha1(
            source_url.encode(
                "utf-8"
            )
        ).hexdigest()[:16]
    )

    recording_ids_by_url[
        source_url
    ] = recording_id

    # Local Phase-1 artwork cache.
    media_path = (
        HTML_DIR.parent
        / "whosampled_media"
        / (
            safe_filename(
                source.get(
                    "source_title",
                    ""
                )
            )
            + ".png"
        )
    )

    media_status = (
        "captured"
        if media_path.exists()
        else "unavailable"
    )

    recording_rows.append({
        "recording_id":
            recording_id,

        "title":
            source.get(
                "source_title",
                "",
            ),

        "artist_names":
            source.get(
                "source_artists",
                "",
            ),

        "album":
            source.get(
                "source_album",
                "",
            ),

        "label":
            source.get(
                "source_label",
                "",
            ),

        "release_year":
            source.get(
                "source_release_year",
                "",
            ),

        "duration":
            source.get(
                "source_duration",
                "",
            ),

        "genre":
            source.get(
                "source_genre",
                "",
            ),

        "keywords":
            source.get(
                "source_keywords",
                "",
            ),

        "whosampled_url":
            source_url,

        "whosampled_thumbnail_url":
            source.get(
                "source_thumbnail_url",
                "",
            ),

        "whosampled_thumbnail_path":
            str(
                media_path
            )
            if media_path.exists()
            else "",

        "whosampled_thumbnail_status":
            media_status,

        "youtube_video_id":
            source.get(
                "source_youtube_video_id",
                "",
            ),

        "youtube_url":
            source.get(
                "source_youtube_url",
                "",
            ),

        "youtube_thumbnail_url":
            source.get(
                "source_youtube_thumbnail_url",
                "",
            ),
    })

    # --------------------------------------------------------
    # Credits.
    # --------------------------------------------------------

    raw_credits = source.get(
        "source_credits",
        "[]",
    )

    try:

        parsed_credits = json.loads(
            raw_credits
            if raw_credits
            else "[]"
        )

    except Exception:

        parsed_credits = []

    credit_number = 0

    for credit in parsed_credits:

        artist_name = str(
            credit.get(
                "artist",
                ""
            )
            or ""
        ).strip()

        role = str(
            credit.get(
                "role",
                ""
            )
            or ""
        ).strip()

        source_role = str(
            credit.get(
                "source_role",
                ""
            )
            or ""
        ).strip()

        if not artist_name or not role:
            continue

        credit_number += 1

        credit_key = (
            f"{recording_id}|"
            f"{artist_name}|"
            f"{role}|"
            f"{source_role}"
        )

        credit_id = (
            "CRD_"
            + hashlib.sha1(
                credit_key.encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
        )

        credit_rows.append({
            "credit_id":
                credit_id,

            "recording_id":
                recording_id,

            "artist_name":
                artist_name,

            "role":
                role,

            "source_role":
                source_role,

            "source":
                "WhoSampled",

            "source_url":
                source_url,
        })

recordings_df = pd.DataFrame(
    recording_rows
)

credits_df = pd.DataFrame(
    credit_rows
)

# ------------------------------------------------------------
# Normalize the current relationship rows.
#
# Source recording IDs can be resolved now.
# Target recording IDs remain blank until we begin collecting
# each related recording's own WhoSampled track page.
# ------------------------------------------------------------

normalized_relationship_rows = []

for _, row in relationships_df.iterrows():

    source_url = str(
        row.get(
            "source_url",
            ""
        )
        or ""
    ).strip().rstrip("/")

    source_recording_id = (
        recording_ids_by_url.get(
            source_url,
            ""
        )
    )

    relationship_key = (
        f"{source_recording_id}|"
        f"{row.get('relationship_type', '')}|"
        f"{row.get('related_track', '')}|"
        f"{row.get('related_artist', '')}|"
        f"{row.get('whosampled_relationship_url', '')}"
    )

    relationship_id = (
        "REL_"
        + hashlib.sha1(
            relationship_key.encode(
                "utf-8"
            )
        ).hexdigest()[:16]
    )

    normalized_relationship_rows.append({
        "relationship_id":
            relationship_id,

        "source_recording_id":
            source_recording_id,

        # Target recording identity will be populated when the
        # related track's own WhoSampled page is collected and
        # reconciled.
        "target_recording_id":
            "",

        "relationship_type":
            row.get(
                "relationship_type",
                "",
            ),

        "related_track":
            row.get(
                "related_track",
                "",
            ),

        "related_artist":
            row.get(
                "related_artist",
                "",
            ),

        "year":
            row.get(
                "year",
                "",
            ),

        "whosampled_relationship_url":
            row.get(
                "whosampled_relationship_url",
                "",
            ),

        "detail":
            row.get(
                "detail",
                "",
            ),
    })

normalized_relationships_df = pd.DataFrame(
    normalized_relationship_rows
)

write_dataframe(
    recordings_df,
    RECORDINGS_FILE
)

write_dataframe(
    credits_df,
    CREDITS_FILE
)

write_dataframe(
    normalized_relationships_df,
    RELATIONSHIP_FILE
)

print()
print(
    "NORMALIZED RECORDINGS:",
    len(recordings_df)
)

print(
    "NORMALIZED CREDITS:",
    len(credits_df)
)

print(
    "NORMALIZED RELATIONSHIPS:",
    len(normalized_relationships_df)
)

print(
    "RECORDINGS FILE:",
    RECORDINGS_FILE
)

print(
    "CREDITS FILE:",
    CREDITS_FILE
)

print(
    "RELATIONSHIPS FILE:",
    RELATIONSHIP_FILE
)


print()
print("=" * 80)
print("RELATIONSHIP EXTRACTION COMPLETE")
print("=" * 80)
print(
    "Unique relationships:",
    len(relationships_df)
)

print(
    "Secondary pages processed:",
    len(processed_secondary)
)

state[
    "relationships_parsed"
] = sorted(
    accepted_primary_html_files
)

save_state(state)


# STEP 5 — RELATED TRACK → SPOTIFY
# ============================================================

print("\n" + "=" * 80)
print("STEP 5 — SPOTIFY ENRICHMENT")
print("=" * 80)

if not relationships_df.empty:

    enriched_rows = []

    for _, rel in relationships_df.iterrows():

        result = resolve_track(
            title=rel["related_track"],
            artists=rel["related_artist"],
            year=rel["year"],
        )

        enriched_rows.append({

            **rel.to_dict(),

            "spotify_match_status":
                result.get(
                    "match_status"
                ),

            "spotify_match_method":
                result.get(
                    "match_method"
                ),

            "spotify_match_score":
                result.get(
                    "match_score"
                ),

            "spotify_track_id":
                result.get(
                    "spotify_track_id"
                ),

            "spotify_isrc":
                result.get(
                    "isrc"
                ),

            "spotify_title":
                result.get(
                    "title"
                ),

            "spotify_artist_names":
                result.get(
                    "artist_names"
                ),

            "spotify_album_name":
                result.get(
                    "album_name"
                ),

            "spotify_album_release_date":
                result.get(
                    "album_release_date"
                ),

            "spotify_album_image_url":
                result.get(
                    "album_image_url"
                ),

            "spotify_album_label":
                result.get(
                    "album_label"
                ),
        })

    enriched_df = pd.DataFrame(
        enriched_rows
    )

    write_dataframe(
        enriched_df,
        ENRICHED_FILE
    )

    state[
        "spotify_enriched"
    ] = True

    save_state(state)

    print(
        "Enriched relationships:",
        len(enriched_df)
    )

else:

    print(
        "No relationships available yet."
    )


# ============================================================
# FINAL
# ============================================================

print("\n" + "=" * 80)
print("PIPELINE FINISHED / CHECKPOINTED")
print("=" * 80)
print(
    "RUN DIRECTORY:",
    RUN_DIR
)
print(
    "Spotify:",
    SPOTIFY_FILE
)
print(
    "Matches:",
    MATCH_FILE
)
print(
    "HTML:",
    HTML_DIR
)
print(
    "Relationships:",
    RELATIONSHIP_FILE
)
print(
    "Enriched:",
    ENRICHED_FILE
)
print(
    "State:",
    STATE_FILE
)
