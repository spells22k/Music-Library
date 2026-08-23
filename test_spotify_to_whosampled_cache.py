import re
import unicodedata
import pandas as pd
from difflib import SequenceMatcher

SPOTIFY_FILE = "kanye_spotify_test.csv"
CACHE_FILE = "kanye_whosampled_track_index.csv"


def normalize(text):
    if not text:
        return ""

    text = str(text)

    # Normalize Unicode, including Spotify's JAŸ-Z representation.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    # Normalize common symbols/punctuation.
    text = text.replace("&", " and ")
    text = text.replace("’", "'")

    text = re.sub(r"[^a-z0-9\s]", " ", text)

    return " ".join(text.split())


def title_similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    return SequenceMatcher(None, a, b).ratio()


def artist_similarity(spotify_artists, cache_artist):
    spotify = [
        normalize(x)
        for x in str(spotify_artists).split(",")
        if str(x).strip()
    ]

    cache = normalize(cache_artist)

    if not cache:
        return 0.0, []

    scores = []

    for artist in spotify:
        if not artist:
            continue

        if artist == cache:
            scores.append(1.0)
        elif artist in cache or cache in artist:
            scores.append(0.9)
        else:
            scores.append(
                SequenceMatcher(None, artist, cache).ratio()
            )

    best = max(scores) if scores else 0.0

    return best, spotify


spotify = pd.read_csv(SPOTIFY_FILE)
cache = pd.read_csv(CACHE_FILE)

print("Spotify tracks:", len(spotify))
print("Cached WhoSampled tracks:", len(cache))
print()

results = []

for _, row in spotify.iterrows():

    spotify_title = str(row["title"])
    spotify_artists = str(row["artist_names"])

    candidates = []

    for _, candidate in cache.iterrows():

        title_score = title_similarity(
            spotify_title,
            candidate["track_title"]
        )

        artist_score, _ = artist_similarity(
            spotify_artists,
            candidate["track_artist"]
        )

        # Title is the primary signal.
        # Artist confirms the identity.
        score = (
            title_score * 0.65
            + artist_score * 0.35
        )

        # Exact title + reasonable artist is extremely strong.
        if (
            normalize(spotify_title)
            == normalize(candidate["track_title"])
            and artist_score >= 0.80
        ):
            score = max(score, 0.95)

        candidates.append({
            "title_score": title_score,
            "artist_score": artist_score,
            "score": score,
            "track_artist": candidate["track_artist"],
            "track_title": candidate["track_title"],
            "whosampled_url": candidate["whosampled_track_url"],
            "discovered_via": candidate["discovered_via_artist"],
        })

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    top = candidates[:3]

    best = top[0]

    margin = (
        best["score"] - top[1]["score"]
        if len(top) > 1
        else best["score"]
    )

    if best["score"] >= 0.90 and margin >= 0.05:
        status = "HIGH_CONFIDENCE"
    elif best["score"] >= 0.75:
        status = "REVIEW"
    else:
        status = "NO_CONFIDENT_MATCH"

    print("=" * 80)
    print(f"SPOTIFY: {spotify_title} — {spotify_artists}")
    print("=" * 80)

    for rank, candidate in enumerate(top, 1):

        print(
            f"{rank}. "
            f"SCORE={candidate['score']:.3f} "
            f"(title={candidate['title_score']:.3f}, "
            f"artist={candidate['artist_score']:.3f})"
        )

        print(
            f"   {candidate['track_artist']} — "
            f"{candidate['track_title']}"
        )

        print(
            f"   {candidate['whosampled_url']}"
        )

    print()
    print("RESULT:", status)
    print("BEST:", best["whosampled_url"])
    print()

    results.append({
        "spotify_track_id": row["spotify_track_id"],
        "isrc": row["isrc"],
        "spotify_title": spotify_title,
        "spotify_artists": spotify_artists,
        "whosampled_artist": best["track_artist"],
        "whosampled_title": best["track_title"],
        "whosampled_url": best["whosampled_url"],
        "match_score": best["score"],
        "title_score": best["title_score"],
        "artist_score": best["artist_score"],
        "margin_over_second": margin,
        "status": status,
    })


output = pd.DataFrame(results)

output.to_csv(
    "kanye_spotify_cache_match_test.csv",
    index=False
)

print("=" * 80)
print("CACHE MATCH TEST COMPLETE")
print("=" * 80)
print(
    output[
        [
            "spotify_title",
            "spotify_artists",
            "whosampled_artist",
            "whosampled_title",
            "match_score",
            "status",
        ]
    ].to_string(index=False)
)

print()
print("Output: kanye_spotify_cache_match_test.csv")
