import json
from pathlib import Path

import pandas as pd

from identity_proposals import (
    load_proposals,
    save_proposals,
    upsert_proposal,
)


RUN = Path(
    "runs/playlist_3XtRerTr3ndS88v51AAixb"
)

CANDIDATES = (
    RUN
    / "artist_catalog_spotify_candidates.csv"
)

CATALOG_RECORDINGS = (
    RUN
    / "artist_catalog_recordings_parsed.csv"
)

REVIEWS = (
    RUN
    / "artist_catalog_spotify_reviews.json"
)

OUTPUT = (
    RUN
    / "canonical_identity_proposals.json"
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


# ------------------------------------------------------------
# Load inputs.
# ------------------------------------------------------------

candidate_df = pd.read_csv(
    CANDIDATES
).fillna("")

catalog_df = pd.read_csv(
    CATALOG_RECORDINGS
).fillna("")

reviews = {}

if REVIEWS.exists():
    reviews = json.loads(
        REVIEWS.read_text(
            encoding="utf-8"
        )
    )

if not isinstance(
    reviews,
    dict,
):
    reviews = {}


# ------------------------------------------------------------
# Build provisional -> canonical recording mapping.
# ------------------------------------------------------------

canonical_by_provisional = {}

for _, row in catalog_df.iterrows():

    provisional_id = clean(
        row.get(
            "recording_id"
        )
    )

    canonical_id = clean(
        row.get(
            "canonical_recording_id"
        )
    )

    if not provisional_id:
        continue

    canonical_by_provisional[
        provisional_id
    ] = (
        canonical_id
        or provisional_id
    )


# ------------------------------------------------------------
# Load/create proposal store.
# ------------------------------------------------------------

proposals = load_proposals(
    OUTPUT
)

processed = 0
created_ids = []


# ------------------------------------------------------------
# Convert each plausible Spotify candidate into a durable
# identity proposal.
# ------------------------------------------------------------

for _, row in candidate_df.iterrows():

    provisional_recording_id = clean(
        row.get(
            "recording_id"
        )
    )

    spotify_track_id = clean(
        row.get(
            "spotify_track_id"
        )
    )

    whosampled_url = clean(
        row.get(
            "whosampled_url"
        )
    )

    candidate_status = clean(
        row.get(
            "candidate_status"
        )
    ).casefold()

    # No proposed Spotify identity exists.
    if (
        not provisional_recording_id
        or not spotify_track_id
        or not whosampled_url
        or candidate_status
        == "no_plausible_candidate"
    ):
        continue

    canonical_recording_id = (
        canonical_by_provisional.get(
            provisional_recording_id,
            provisional_recording_id,
        )
    )

    review = reviews.get(
        provisional_recording_id,
        {}
    )

    if not isinstance(
        review,
        dict,
    ):
        review = {}

    review_decision = clean(
        review.get(
            "decision"
        )
    ).casefold()

    selected_spotify_id = clean(
        review.get(
            "spotify_track_id"
        )
    )

    selected_flag = clean(
        row.get(
            "spotify_selected_candidate"
        )
    ).casefold()

    is_selected = (
        selected_flag
        in {
            "true",
            "1",
            "yes",
        }
    )

    # --------------------------------------------------------
    # Determine proposal state.
    # --------------------------------------------------------

    status = "pending"

    # Accepted recording-level review:
    # only the selected candidate is the accepted identity.
    if review_decision == "accepted":

        if (
            selected_spotify_id
            and spotify_track_id
            == selected_spotify_id
        ):
            status = "accepted"

        elif is_selected:
            status = "accepted"

        else:
            status = "rejected"

    # Explicit rejection of a selected candidate.
    elif review_decision == "rejected":

        if (
            selected_spotify_id
            and spotify_track_id
            == selected_spotify_id
        ):
            status = "rejected"

        elif is_selected:
            status = "rejected"

    evidence = {
        "origin":
            "artist_catalog_spotify_candidate",

        "provisional_recording_id":
            provisional_recording_id,

        "candidate_rank":
            clean(
                row.get(
                    "candidate_rank"
                )
            ),

        "title_score":
            clean(
                row.get(
                    "title_score"
                )
            ),

        "artist_score":
            clean(
                row.get(
                    "artist_score"
                )
            ),

        "duration_score":
            clean(
                row.get(
                    "duration_score"
                )
            ),

        "duration_difference_seconds":
            clean(
                row.get(
                    "duration_difference_seconds"
                )
            ),

        "year_score":
            clean(
                row.get(
                    "year_score"
                )
            ),

        "album_score":
            clean(
                row.get(
                    "album_score"
                )
            ),

        "candidate_status":
            candidate_status,

        "review_decision":
            review_decision,

        "selected_candidate":
            is_selected,
    }

    pid = upsert_proposal(
        proposals,

        canonical_recording_id=
            canonical_recording_id,

        source_system=
            "whosampled",

        source_identity=
            whosampled_url,

        candidate_system=
            "spotify",

        candidate_identity=
            spotify_track_id,

        confidence_score=
            clean(
                row.get(
                    "match_score"
                )
            ),

        evidence=
            evidence,

        status=
            status,

        source_title=
            clean(
                row.get(
                    "whosampled_title"
                )
            ),

        source_artist_names=
            clean(
                row.get(
                    "whosampled_artist_names"
                )
            ),

        candidate_title=
            clean(
                row.get(
                    "spotify_title"
                )
            ),

        candidate_artist_names=
            clean(
                row.get(
                    "spotify_artist_names"
                )
            ),

        candidate_url=
            clean(
                row.get(
                    "spotify_url"
                )
            ),
    )

    processed += 1
    created_ids.append(
        pid
    )


save_proposals(
    OUTPUT,
    proposals
)


# ------------------------------------------------------------
# Report.
# ------------------------------------------------------------

print("=" * 100)
print("CANONICAL IDENTITY PROPOSAL BUILD")
print("=" * 100)

print()
print(
    "Candidate rows converted:",
    processed
)

print(
    "Stored proposals:",
    len(proposals)
)

print()
print("Statuses:")

for status in (
    "pending",
    "accepted",
    "rejected",
):

    count = sum(
        1
        for proposal
        in proposals.values()
        if clean(
            proposal.get(
                "status"
            )
        )
        == status
    )

    print(
        f"  {status}:",
        count
    )

print()
print(
    "Output:",
    OUTPUT
)

print()
print(
    "No network requests were made."
)
