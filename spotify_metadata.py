import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth


CACHE_DIR = Path("spotify_cache")
TRACK_CACHE_FILE = CACHE_DIR / "tracks.json"
RESOLUTION_CACHE_FILE = CACHE_DIR / "resolutions.json"

CACHE_DIR.mkdir(exist_ok=True)

load_dotenv()


def normalize(text):
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()
    text = text.replace("&", " and ")
    text = text.replace("’", "'")

    text = re.sub(r"[^a-z0-9\s]", " ", text)

    return " ".join(text.split())


def resolution_key(title, artists, year=None):
    """
    Create a stable key for an unknown recording.

    Example:
        Ponderosa Twins Plus One + Bound + 1971

    becomes something like:
        ponderosa twins plus one|bound|1971
    """
    normalized_title = normalize(title)

    artist_list = [
        normalize(a)
        for a in str(artists).split(",")
        if str(a).strip()
    ]

    artist_list = sorted(
        set(a for a in artist_list if a)
    )

    normalized_artists = "|".join(artist_list)

    normalized_year = (
        str(year).strip()[:4]
        if year
        else ""
    )

    return (
        f"{normalized_artists}"
        f"::{normalized_title}"
        f"::{normalized_year}"
    )


def similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    return SequenceMatcher(None, a, b).ratio()


def load_json_cache(path):
    if not path.exists():
        return {}

    with path.open(
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)


def save_json_cache(path, data):
    with path.open(
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )


def load_track_cache():
    return load_json_cache(
        TRACK_CACHE_FILE
    )


def load_resolution_cache():
    return load_json_cache(
        RESOLUTION_CACHE_FILE
    )


def get_spotify_client():
    client_id = os.getenv(
        "SPOTIFY_CLIENT_ID"
    )

    client_secret = os.getenv(
        "SPOTIFY_CLIENT_SECRET"
    )

    if not client_id or not client_secret:
        raise EnvironmentError(
            "Spotify credentials are missing"
        )

    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=(
                "http://127.0.0.1:8888/callback"
            ),
            scope=(
                "playlist-read-private "
                "playlist-read-collaborative"
            )
        )
    )


def normalize_track_response(track):
    album = track.get("album", {})
    artists = track.get("artists", [])

    spotify_id = track.get("id")

    return {
        "spotify_track_id": spotify_id,
        "spotify_uri": track.get("uri"),
        "spotify_url": (
            track.get(
                "external_urls",
                {}
            ).get("spotify")
        ),
        "isrc": (
            track.get(
                "external_ids",
                {}
            ).get("isrc")
        ),
        "title": track.get("name"),
        "artist_names": ", ".join(
            artist.get("name", "")
            for artist in artists
        ),
        "artist_ids": ", ".join(
            artist.get("id", "")
            for artist in artists
        ),
        "album_id": album.get("id"),
        "album_name": album.get("name"),
        "album_release_date": album.get(
            "release_date"
        ),
        "album_release_precision": album.get(
            "release_date_precision"
        ),
        "album_image_url": (
            album.get(
                "images",
                [{}]
            )[0].get("url")
            if album.get("images")
            else None
        ),
        "album_label": album.get("label"),
        "duration_ms": track.get(
            "duration_ms"
        ),
    }


def cache_track(normalized, track_cache=None):
    if track_cache is None:
        track_cache = load_track_cache()

    spotify_id = normalized.get(
        "spotify_track_id"
    )

    if not spotify_id:
        return

    track_cache[
        f"spotify:{spotify_id}"
    ] = normalized

    save_json_cache(
        TRACK_CACHE_FILE,
        track_cache
    )


def get_track_by_id(
    spotify_track_id,
    sp=None,
    cache=None
):
    """
    Fetch complete Spotify metadata when the
    Spotify track ID is already known.

    Uses tracks.json to avoid repeat API calls.
    """
    if cache is None:
        cache = load_track_cache()

    key = f"spotify:{spotify_track_id}"

    if key in cache:
        return {
            **cache[key],
            "match_status": "cached",
            "match_method": "track_id_cache",
        }

    if sp is None:
        sp = get_spotify_client()

    track = sp.track(
        spotify_track_id
    )

    normalized = normalize_track_response(
        track
    )

    normalized["match_status"] = "matched"
    normalized["match_method"] = "track_id"
    normalized["match_score"] = 1.0

    cache_track(
        normalized,
        cache
    )

    return normalized


def artist_similarity(
    target_artist,
    spotify_artists
):
    target = normalize(
        target_artist
    )

    if not target:
        return 0.0

    scores = []

    for artist in spotify_artists:

        name = normalize(
            artist["name"]
        )

        if name == target:
            scores.append(1.0)

        elif (
            target in name
            or name in target
        ):
            scores.append(0.90)

        else:
            scores.append(
                SequenceMatcher(
                    None,
                    target,
                    name
                ).ratio()
            )

    return max(scores) if scores else 0.0



VERSION_TERMS = {
    "remix",
    "remastered",
    "remaster",
    "spotify singles",
    "live",
    "acoustic",
    "instrumental",
    "demo",
    "edit",
    "radio edit",
    "extended",
    "extended mix",
    "mono",
    "stereo",
    "karaoke",
    "slowed",
    "sped up",
    "speed up",
}


FEATURE_PREFIXES = (
    "feat",
    "featuring",
    "with",
)


def title_base(text):
    """
    Normalize a title for recording identity.

    Removes feature/artist suffixes and preserves version terms
    so that version mismatches can still be detected separately.
    """
    normalized = normalize(text)

    # Remove parenthetical/bracketed feature information.
    normalized = re.sub(
        r"\b(?:feat|featuring|ft|with)\b.*$",
        "",
        normalized
    )

    return " ".join(
        normalized.split()
    )


def compact_title(text):
    """
    Remove whitespace from a normalized title.
    Handles forms such as:
        A N X I E T Y
        ANXIETY
    """
    return re.sub(
        r"\\s+",
        "",
        normalize(text)
    )


def title_tokens(text):
    return set(
        normalize(text).split()
    )


def version_terms_in_title(text):
    normalized = normalize(text)

    found = set()

    for term in VERSION_TERMS:

        if term in normalized:
            found.add(term)

    return found


def extract_title_year(text):
    match = re.search(
        r"\b(19|20)\d{2}\b",
        str(text or "")
    )

    return (
        match.group(0)
        if match
        else None
    )


def title_identity_score(
    target_title,
    candidate_title
):
    """
    Recording-aware title comparison.

    Examples treated favorably:
      A N X I E T Y
      ANXIETY (feat. Doechii)

    Examples treated differently:
      Anxiety
      Anxiety (Mosimann Remix)
    """

    target_normalized = normalize(
        target_title
    )

    candidate_normalized = normalize(
        candidate_title
    )

    if not target_normalized or not candidate_normalized:
        return 0.0

    if target_normalized == candidate_normalized:
        return 1.0

    if (
        compact_title(target_title)
        == compact_title(candidate_title)
    ):
        return 1.0

    target_base = title_base(
        target_title
    )

    candidate_base = title_base(
        candidate_title
    )

    if target_base == candidate_base:
        score = 1.0
    else:
        score = max(
            similarity(
                target_base,
                candidate_base
            ),
            similarity(
                target_normalized,
                candidate_normalized
            )
        )

    # Token containment helps titles like:
    # "A N X I E T Y"
    # vs
    # "ANXIETY"
    if (
        target_base
        and candidate_base
        and (
            target_base in candidate_base
            or candidate_base in target_base
        )
    ):
        score = max(
            score,
            0.92
        )

    target_versions = (
        version_terms_in_title(
            target_title
        )
    )

    candidate_versions = (
        version_terms_in_title(
            candidate_title
        )
    )

    # Requested recording has no version qualifier,
    # but Spotify candidate does.
    if (
        not target_versions
        and candidate_versions
    ):
        score -= 0.18

    # Requested a specific version but candidate dropped it.
    if (
        target_versions
        and not target_versions.intersection(
            candidate_versions
        )
    ):
        score -= 0.20

    # Both have versions, but they disagree.
    if (
        target_versions
        and candidate_versions
        and not target_versions.intersection(
            candidate_versions
        )
    ):
        score -= 0.25

    return max(
        0.0,
        min(
            1.0,
            score
        )
    )


def score_candidate(
    target_title,
    target_artists,
    target_year,
    candidate
):

    candidate_artists = [
        artist.get("name", "")
        for artist in candidate.get(
            "artists",
            []
        )
    ]

    candidate_title = candidate.get(
        "name",
        ""
    )

    title_score = title_identity_score(
        target_title,
        candidate_title
    )

    target_artist_list = [
        normalize(a)
        for a in str(
            target_artists
        ).split(",")
        if a.strip()
    ]

    artist_scores = []

    for target_artist in target_artist_list:

        for candidate_artist in candidate_artists:

            artist_scores.append(
                similarity(
                    target_artist,
                    candidate_artist
                )
            )

    artist_score = (
        max(artist_scores)
        if artist_scores
        else 0.0
    )

    release_date = (
        candidate
        .get("album", {})
        .get("release_date")
    )

    year_score = 0.0
    year_conflict = False
    year_difference = None

    target_title_year = extract_title_year(
        target_title
    )

    candidate_title_year = extract_title_year(
        candidate_title
    )

    title_year_conflict = (
        target_title_year is not None
        and candidate_title_year is not None
        and target_title_year
        != candidate_title_year
    )

    if target_year and release_date:

        target_year_str = str(
            target_year
        )[:4]

        release_year = str(
            release_date
        )[:4]

        if (
            target_year_str.isdigit()
            and release_year.isdigit()
        ):

            year_difference = abs(
                int(target_year_str)
                - int(release_year)
            )

            if year_difference == 0:
                year_score = 1.0
            elif year_difference == 1:
                year_score = 0.5
            elif year_difference <= 3:
                year_score = 0.25
            else:
                year_score = 0.0

            year_conflict = (
                year_difference > 0
            )

    # Stronger than the old 60/30/10 weighting:
    # title identity dominates, artist confirms identity,
    # year helps distinguish recordings/versions.
    score = (
        title_score * 0.62
        + artist_score * 0.28
        + year_score * 0.10
    )

    # A known year mismatch should matter much more than
    # the old 10% year bonus suggested.
    if year_conflict:

        score -= 0.15

    # Strong version mismatch protection.
    target_versions = (
        version_terms_in_title(
            target_title
        )
    )

    candidate_versions = (
        version_terms_in_title(
            candidate_title
        )
    )

    if (
        not target_versions
        and candidate_versions
    ):
        score -= 0.12

    if (
        target_versions
        and candidate_versions
        and not target_versions.intersection(
            candidate_versions
        )
    ):
        score -= 0.15

    score = max(
        0.0,
        min(
            1.0,
            score
        )
    )

    return {
        "score": score,
        "title_score": title_score,
        "artist_score": artist_score,
        "year_score": year_score,
        "year_conflict": year_conflict,
        "year_difference": year_difference,
        "title_year_conflict": title_year_conflict,
        "target_title_year": target_title_year,
        "candidate_title_year": candidate_title_year,
        "target_version_terms": sorted(
            target_versions
        ),
        "candidate_version_terms": sorted(
            candidate_versions
        ),
    }


def resolve_track(
    title,
    artists,
    year=None,
    sp=None,
    track_cache=None,
    resolution_cache=None
):
    """
    Resolve an unknown recording through Spotify.

    FIRST:
        Check resolutions.json.

    If already resolved:
        return cached result without calling Spotify.

    Otherwise:
        Search Spotify.
        Rank candidates.
        Fetch the selected track.
        Save both the track metadata and
        the resolution decision.
    """

    if track_cache is None:
        track_cache = load_track_cache()

    if resolution_cache is None:
        resolution_cache = load_resolution_cache()

    key = resolution_key(
        title,
        artists,
        year
    )

    # ------------------------------------------------------
    # RESOLUTION CACHE
    # ------------------------------------------------------

    if key in resolution_cache:

        cached_resolution = (
            resolution_cache[key]
        )

        spotify_track_id = (
            cached_resolution
            .get("spotify_track_id")
        )

        cached_status = (
            cached_resolution.get(
                "match_status"
            )
        )

        # Reuse a cached MATCHED result only when it was
        # validated under the current recording-identity rules.
        #
        # Older cached matches may not contain year diagnostics,
        # so they are deliberately re-scored.

        cached_year_difference = (
            cached_resolution.get(
                "year_difference"
            )
        )

        cached_title_year_conflict = (
            cached_resolution.get(
                "title_year_conflict",
                False
            )
        )

        cached_target_versions = set(
            cached_resolution.get(
                "target_version_terms",
                []
            )
        )

        cached_candidate_versions = set(
            cached_resolution.get(
                "candidate_version_terms",
                []
            )
        )

        cached_version_conflict = (
            bool(cached_target_versions)
            and bool(cached_candidate_versions)
            and not cached_target_versions.intersection(
                cached_candidate_versions
            )
        )

        cache_is_safe_match = (
            cached_status == "matched"
            and spotify_track_id
            and cached_year_difference == 0
            and not cached_title_year_conflict
            and not cached_version_conflict
        )

        if cache_is_safe_match:

            track_key = (
                f"spotify:{spotify_track_id}"
            )

            if track_key in track_cache:

                return {
                    **track_cache[track_key],
                    "match_status": "matched",
                    "match_method": (
                        "resolution_cache"
                    ),
                    "match_score": (
                        cached_resolution.get(
                            "match_score"
                        )
                    ),
                    "match_margin": (
                        cached_resolution.get(
                            "match_margin"
                        )
                    ),
                }

    if sp is None:
        sp = get_spotify_client()

    # ------------------------------------------------------
    # SEARCH
    # ------------------------------------------------------

    query = (
        f'track:"{title}" '
        f'artist:"{artists}"'
    )

    results = sp.search(
        q=query,
        type="track",
        limit=10
    )

    candidates = (
        results
        .get("tracks", {})
        .get("items", [])
    )

    # Broader fallback.
    if not candidates:

        query = f"{title} {artists}"

        results = sp.search(
            q=query,
            type="track",
            limit=10
        )

        candidates = (
            results
            .get("tracks", {})
            .get("items", [])
        )

    # ------------------------------------------------------
    # NO RESULT
    # ------------------------------------------------------

    if not candidates:

        resolution_cache[key] = {
            "title": title,
            "artists": artists,
            "year": year,
            "spotify_track_id": None,
            "match_status": "not_found",
            "match_score": 0.0,
            "match_margin": 0.0,
        }

        save_json_cache(
            RESOLUTION_CACHE_FILE,
            resolution_cache
        )

        return {
            "title": title,
            "artist_names": artists,
            "spotify_track_id": None,
            "isrc": None,
            "match_status": "not_found",
            "match_method": "search",
            "match_score": 0.0,
            "match_margin": 0.0,
        }

    # ------------------------------------------------------
    # SCORE
    # ------------------------------------------------------

    scored = []

    for candidate in candidates:

        scores = score_candidate(
            title,
            artists,
            year,
            candidate
        )

        scored.append({
            "candidate": candidate,
            **scores,
        })

    scored.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    best = scored[0]

    if len(scored) > 1:

        margin = (
            best["score"]
            - scored[1]["score"]
        )

    else:
        margin = best["score"]

    # ------------------------------------------------------
    # RECORDING-AWARE MATCH CALIBRATION
    # ------------------------------------------------------

    title_score = best.get(
        "title_score",
        0.0
    )

    artist_score = best.get(
        "artist_score",
        0.0
    )

    year_difference = best.get(
        "year_difference"
    )

    title_year_conflict = best.get(
        "title_year_conflict",
        False
    )

    target_versions = set(
        best.get(
            "target_version_terms",
            []
        )
    )

    candidate_versions = set(
        best.get(
            "candidate_version_terms",
            []
        )
    )

    version_conflict = (
        bool(target_versions)
        and bool(candidate_versions)
        and not target_versions.intersection(
            candidate_versions
        )
    )

    # Exact recording identity:
    # same title, strong artist overlap, same known year,
    # and no explicit version conflict.
    exact_recording_match = (
        title_score >= 0.98
        and artist_score >= 0.95
        and year_difference == 0
        and not title_year_conflict
        and not version_conflict
    )

    if exact_recording_match:

        status = "matched"

    # Any known year difference must remain REVIEW.
    elif (
        year_difference is not None
        and year_difference > 0
    ):

        status = "review"

    # A year embedded in the title disagrees.
    elif title_year_conflict:

        status = "review"

    # Explicit remix/live/remaster/etc. mismatch.
    elif version_conflict:

        status = "unmatched"

    # Strong title/artist match but year is unavailable.
    elif (
        year_difference is None
        and title_score >= 0.98
        and artist_score >= 0.95
    ):

        status = "review"

    # Otherwise retain conservative review/unmatched behavior.
    elif best["score"] >= 0.70:

        status = "review"

    else:

        status = "unmatched"

    # ------------------------------------------------------
    # FETCH COMPLETE TRACK METADATA
    # ------------------------------------------------------

    track = sp.track(
        best["candidate"]["id"]
    )

    normalized = normalize_track_response(
        track
    )

    normalized.update({
        "match_status": status,
        "match_method": "search",
        "match_score": best["score"],
        "match_margin": margin,
    })

    # Save canonical Spotify track metadata.
    cache_track(
        normalized,
        track_cache
    )

    # ------------------------------------------------------
    # SAVE RESOLUTION DECISION
    # ------------------------------------------------------

    resolution_cache[key] = {
        "title": title,
        "artists": artists,
        "year": year,

        "spotify_track_id":
            normalized["spotify_track_id"],

        "match_status": status,
        "match_method": "search",
        "match_score": best["score"],
        "match_margin": margin,
        "title_score": best.get(
            "title_score"
        ),
        "artist_score": best.get(
            "artist_score"
        ),
        "year_score": best.get(
            "year_score"
        ),
        "year_conflict": best.get(
            "year_conflict",
            False
        ),
        "year_difference": best.get(
            "year_difference"
        ),
        "target_version_terms": best.get(
            "target_version_terms",
            []
        ),
        "candidate_version_terms": best.get(
            "candidate_version_terms",
            []
        ),
    }

    save_json_cache(
        RESOLUTION_CACHE_FILE,
        resolution_cache
    )

    return normalized
