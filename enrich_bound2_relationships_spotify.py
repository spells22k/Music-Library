import os
import re
import unicodedata
import pandas as pd
import spotipy

from difflib import SequenceMatcher
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth


INPUT = "bound_2_playwright_relationships.csv"
OUTPUT = "bound_2_relationships_enriched.csv"


def normalize(text):
    if not text:
        return ""

    text = str(text)

    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = text.replace("&", " and ")
    text = text.replace("’", "'")

    text = re.sub(r"[^a-z0-9\s]", " ", text)

    return " ".join(text.split())


def similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    return SequenceMatcher(None, a, b).ratio()


def artist_similarity(target_artist, spotify_artists):
    target = normalize(target_artist)

    if not target:
        return 0.0

    scores = []

    for artist in spotify_artists:
        name = normalize(artist["name"])

        if name == target:
            scores.append(1.0)
        elif target in name or name in target:
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


def year_similarity(target_year, release_date):
    if not target_year or not release_date:
        return 0.0

    target_year = str(target_year).strip()

    release_year = str(
        release_date
    ).strip()[:4]

    if (
        target_year.isdigit()
        and release_year.isdigit()
        and target_year == release_year
    ):
        return 1.0

    return 0.0


def score_candidate(
    related_track,
    related_artist,
    related_year,
    candidate
):
    title_score = similarity(
        related_track,
        candidate["name"]
    )

    artist_score = artist_similarity(
        related_artist,
        candidate["artists"]
    )

    year_score = year_similarity(
        related_year,
        candidate["album"]["release_date"]
    )

    # Title is the most important signal.
    score = (
        title_score * 0.60
        + artist_score * 0.30
        + year_score * 0.10
    )

    return {
        "score": score,
        "title_score": title_score,
        "artist_score": artist_score,
        "year_score": year_score,
    }


load_dotenv()

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


sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=(
            "playlist-read-private "
            "playlist-read-collaborative"
        )
    )
)


df = pd.read_csv(INPUT)

results = []


for _, row in df.iterrows():

    related_track = str(
        row["related_track"]
    ).strip()

    related_artist = str(
        row["related_artist"]
    ).strip()

    related_year = str(
        row["year"]
    ).strip()

    print()
    print("=" * 80)
    print(
        "WHO SAMPLED:",
        related_track,
        "—",
        related_artist,
        related_year
    )
    print("=" * 80)

    # Spotify Search currently allows up to 10 results
    # per request.
    query = (
        f'track:"{related_track}" '
        f'artist:"{related_artist}"'
    )

    try:
        search = sp.search(
            q=query,
            type="track",
            limit=10
        )

        candidates = search.get(
            "tracks",
            {}
        ).get(
            "items",
            []
        )

    except Exception as e:

        print(
            "SPOTIFY SEARCH ERROR:",
            repr(e)
        )

        results.append({
            **row.to_dict(),
            "spotify_match_status": "error",
            "spotify_match_score": None,
        })

        continue


    scored = []

    for candidate in candidates:

        scores = score_candidate(
            related_track,
            related_artist,
            related_year,
            candidate
        )

        scored.append({
            "candidate": candidate,
            **scores
        })


    scored.sort(
        key=lambda x: x["score"],
        reverse=True
    )


    if not scored:

        print("NO SPOTIFY RESULTS")

        results.append({
            **row.to_dict(),
            "spotify_match_status": "unmatched",
            "spotify_match_score": None,
        })

        continue


    print("TOP SPOTIFY CANDIDATES:")

    for rank, item in enumerate(
        scored[:3],
        start=1
    ):

        candidate = item["candidate"]

        print(
            f"{rank}. "
            f"{candidate['name']} — "
            f"{', '.join(a['name'] for a in candidate['artists'])}"
        )

        print(
            f"   score={item['score']:.3f} "
            f"title={item['title_score']:.3f} "
            f"artist={item['artist_score']:.3f} "
            f"year={item['year_score']:.3f}"
        )

        print(
            f"   album={candidate['album']['name']}"
        )

        print(
            f"   release={candidate['album']['release_date']}"
        )

        print(
            f"   id={candidate['id']}"
        )


    best = scored[0]

    second = (
        scored[1]
        if len(scored) > 1
        else None
    )

    margin = (
        best["score"] - second["score"]
        if second
        else best["score"]
    )


    if (
        best["score"] >= 0.85
        and margin >= 0.05
    ):
        match_status = "matched"

    elif best["score"] >= 0.75:
        match_status = "review"

    else:
        match_status = "unmatched"


    # Only fetch the complete Spotify track object for the
    # selected candidate. This is where we get full metadata
    # including external_ids/isrc.
    full_track = None

    if match_status in {
        "matched",
        "review"
    }:

        try:
            full_track = sp.track(
                best["candidate"]["id"]
            )

        except Exception as e:

            print(
                "SPOTIFY TRACK FETCH ERROR:",
                repr(e)
            )


    output = {
        **row.to_dict(),

        "spotify_match_status": match_status,
        "spotify_match_score": best["score"],
        "spotify_match_margin": margin,

        "spotify_track_id": None,
        "spotify_uri": None,
        "spotify_url": None,
        "spotify_isrc": None,

        "spotify_title": None,
        "spotify_artist_names": None,

        "spotify_album_name": None,
        "spotify_album_id": None,
        "spotify_album_release_date": None,
        "spotify_album_image_url": None,

        "spotify_duration_ms": None,
    }


    if full_track:

        album = full_track.get(
            "album",
            {}
        )

        artists = full_track.get(
            "artists",
            []
        )

        spotify_id = full_track.get(
            "id"
        )

        output.update({

            "spotify_track_id":
                spotify_id,

            "spotify_uri":
                full_track.get("uri"),

            "spotify_url":
                (
                    full_track
                    .get("external_urls", {})
                    .get("spotify")
                ),

            "spotify_isrc":
                (
                    full_track
                    .get("external_ids", {})
                    .get("isrc")
                ),

            "spotify_title":
                full_track.get("name"),

            "spotify_artist_names":
                ", ".join(
                    artist["name"]
                    for artist in artists
                ),

            "spotify_album_name":
                album.get("name"),

            "spotify_album_id":
                album.get("id"),

            "spotify_album_release_date":
                album.get("release_date"),

            "spotify_album_image_url":
                (
                    album
                    .get("images", [{}])[0]
                    .get("url")
                    if album.get("images")
                    else None
                ),

            "spotify_duration_ms":
                full_track.get(
                    "duration_ms"
                ),
        })


    results.append(output)

    print()
    print(
        "RESULT:",
        match_status
    )

    if output["spotify_track_id"]:
        print(
            "SPOTIFY:",
            output["spotify_title"],
            "—",
            output["spotify_artist_names"]
        )

        print(
            "ISRC:",
            output["spotify_isrc"]
        )


result_df = pd.DataFrame(results)

result_df.to_csv(
    OUTPUT,
    index=False
)


print()
print("=" * 80)
print("SPOTIFY ENRICHMENT COMPLETE")
print("=" * 80)

print(
    "Relationships:",
    len(result_df)
)

print(
    "Matched:",
    (
        result_df[
            "spotify_match_status"
        ] == "matched"
    ).sum()
)

print(
    "Review:",
    (
        result_df[
            "spotify_match_status"
        ] == "review"
    ).sum()
)

print(
    "Unmatched:",
    (
        result_df[
            "spotify_match_status"
        ] == "unmatched"
    ).sum()
)

print()
print("Output:", OUTPUT)
