import argparse
import json
import time
from pathlib import Path

import pandas as pd
from spotipy.exceptions import SpotifyException

from spotify_metadata import (
    get_spotify_client,
    normalize,
)


def clean(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    return str(value).strip()


def load_json(path):
    if not path.exists():
        return {}

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return (
            data
            if isinstance(data, dict)
            else {}
        )

    except Exception:
        return {}


def save_json(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def normalize_artist(item):
    return {
        "spotify_artist_id":
            clean(
                item.get("id")
            ),

        "spotify_artist_name":
            clean(
                item.get("name")
            ),

        "spotify_uri":
            clean(
                item.get("uri")
            ),

        "spotify_url":
            clean(
                item.get(
                    "external_urls",
                    {}
                ).get(
                    "spotify"
                )
            ),

        "followers":
            (
                item.get(
                    "followers",
                    {}
                ).get(
                    "total"
                )
            ),

        "popularity":
            item.get(
                "popularity"
            ),

        "genres":
            item.get(
                "genres",
                []
            ),

        "images":
            item.get(
                "images",
                []
            ),
    }


def retry_after_seconds(exc):
    headers = getattr(
        exc,
        "headers",
        None,
    )

    if not headers:
        return None

    value = (
        headers.get("Retry-After")
        or headers.get("retry-after")
    )

    if value is None:
        return None

    try:
        return int(
            float(value)
        )
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--run-dir",
        default=(
            "runs/"
            "playlist_3XtRerTr3ndS88v51AAixb"
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=8.0,
        help=(
            "Seconds between Spotify artist searches."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum new Spotify searches this run."
        ),
    )

    parser.add_argument(
        "--results",
        type=int,
        default=5,
        help=(
            "Spotify artist results retained per search."
        ),
    )

    args = parser.parse_args()

    run_dir = Path(
        args.run_dir
    )

    candidate_file = (
        run_dir
        / "related_track_pages"
        / "artist_identity_candidates.csv"
    )

    cache_file = (
        run_dir
        / "related_track_pages"
        / "spotify_artist_search_cache.json"
    )

    if not candidate_file.exists():
        raise SystemExit(
            f"Missing: {candidate_file}"
        )

    df = pd.read_csv(
        candidate_file
    ).fillna("")

    # One Spotify search per unique WhoSampled artist.
    artists = {}

    for _, row in df.iterrows():

        artist_name = clean(
            row.get(
                "artist_name"
            )
        )

        ws_url = clean(
            row.get(
                "whosampled_url"
            )
        )

        provisional_id = clean(
            row.get(
                "provisional_artist_id"
            )
        )

        if (
            not artist_name
            or not provisional_id
        ):
            continue

        key = provisional_id

        artists.setdefault(
            key,
            {
                "provisional_artist_id":
                    provisional_id,

                "artist_name":
                    artist_name,

                "whosampled_url":
                    ws_url,
            },
        )

    cache = load_json(
        cache_file
    )

    print("=" * 88)
    print("PACED SPOTIFY ARTIST SEARCH")
    print("=" * 88)
    print()
    print(
        "Unique WhoSampled artists:",
        len(artists),
    )
    print(
        "Existing cached searches:",
        len(cache),
    )
    print(
        "Delay:",
        args.delay,
        "seconds",
    )
    print()

    pending = [
        (
            key,
            value,
        )
        for key, value
        in artists.items()
        if (
            key not in cache
            or cache[
                key
            ].get(
                "status"
            )
            not in {
                "searched",
                "no_results",
            }
        )
    ]

    print(
        "Pending Spotify searches:",
        len(pending),
    )

    if not pending:
        print()
        print(
            "Nothing to request."
        )
        return

    sp = get_spotify_client()

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Spotipy normally retries 429 responses automatically.
    # That is what can produce an enormous automatic sleep.
    #
    # Disable adapter retries for this controlled test so WE
    # receive the 429 and can checkpoint + stop immediately.
    # --------------------------------------------------------

    try:
        adapter = sp._session.get_adapter(
            "https://"
        )

        adapter.max_retries = 0

        print(
            "Automatic HTTP retries disabled "
            "for this search run."
        )

    except Exception as exc:
        print(
            "WARNING: could not disable "
            "HTTP adapter retries:",
            repr(exc),
        )

    requested = 0

    for key, artist in pending:

        if (
            args.limit is not None
            and requested >= args.limit
        ):
            print()
            print(
                "REQUEST LIMIT REACHED:",
                args.limit,
            )
            break

        name = artist[
            "artist_name"
        ]

        print()
        print("-" * 88)
        print(
            "WhoSampled artist:",
            name,
        )
        print(
            "WhoSampled URL:",
            artist[
                "whosampled_url"
            ],
        )

        query = (
            'artist:"'
            + name.replace(
                '"',
                ""
            )
            + '"'
        )

        print(
            "Spotify query:",
            query,
        )

        try:
            response = sp.search(
                q=query,
                type="artist",
                limit=args.results,
            )

            requested += 1

        except SpotifyException as exc:

            status = getattr(
                exc,
                "http_status",
                None,
            )

            retry_after = (
                retry_after_seconds(
                    exc
                )
            )

            print(
                "SPOTIFY ERROR:",
                status,
            )

            if status == 429:

                print(
                    "Retry-After:",
                    retry_after,
                    "seconds",
                )

                cache[
                    key
                ] = {
                    **artist,

                    "status":
                        "rate_limited",

                    "retry_after_seconds":
                        retry_after,

                    "query":
                        query,

                    "candidates":
                        [],
                }

                save_json(
                    cache_file,
                    cache,
                )

                print()
                print(
                    "Stopping safely on Spotify 429."
                )
                print(
                    "No further artist requests "
                    "will be made this run."
                )

                return

            cache[
                key
            ] = {
                **artist,

                "status":
                    "spotify_error",

                "http_status":
                    status,

                "error":
                    repr(exc),

                "query":
                    query,

                "candidates":
                    [],
            }

            save_json(
                cache_file,
                cache,
            )

            continue

        items = (
            response.get(
                "artists",
                {}
            ).get(
                "items",
                []
            )
            or []
        )

        candidates = [
            normalize_artist(
                item
            )
            for item in items
        ]

        # Put exact normalized-name candidates first.
        candidates.sort(
            key=lambda candidate: (
                normalize(
                    candidate.get(
                        "spotify_artist_name"
                    )
                )
                != normalize(name),

                -int(
                    candidate.get(
                        "popularity"
                    )
                    or 0
                ),
            )
        )

        status = (
            "searched"
            if candidates
            else "no_results"
        )

        cache[
            key
        ] = {
            **artist,

            "status":
                status,

            "query":
                query,

            "candidates":
                candidates,
        }

        # Checkpoint after EVERY request.
        save_json(
            cache_file,
            cache,
        )

        print(
            "Candidates:",
            len(candidates),
        )

        for index, candidate in enumerate(
            candidates,
            start=1,
        ):
            print(
                f"  {index}.",
                candidate[
                    "spotify_artist_name"
                ],
                "|",
                candidate[
                    "spotify_artist_id"
                ],
                "| popularity",
                candidate[
                    "popularity"
                ],
            )

        if (
            args.limit is None
            or requested < args.limit
        ):
            print(
                f"Waiting {args.delay:g} seconds..."
            )

            time.sleep(
                args.delay
            )

    print()
    print("=" * 88)
    print("SPOTIFY ARTIST SEARCH SUMMARY")
    print("=" * 88)
    print(
        "Requests made:",
        requested,
    )
    print(
        "Cache:",
        cache_file,
    )


if __name__ == "__main__":
    main()
