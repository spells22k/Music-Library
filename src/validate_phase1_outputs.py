import json
from pathlib import Path

import pandas as pd


def clean(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


RUN_NAME = "playlist_3XtRerTr3ndS88v51AAixb_blind"
RUN_DIR = Path("runs") / RUN_NAME

RECORDINGS_FILE = RUN_DIR / "recordings.csv"
CREDITS_FILE = RUN_DIR / "credits.csv"
RELATIONSHIPS_FILE = RUN_DIR / "relationships.csv"
MATCH_FILE = RUN_DIR / "matched_tracks.csv"

REPORT_FILE = RUN_DIR / "phase1_validation_report.json"


def load_csv(path):
    if not path.exists():
        raise FileNotFoundError(path)

    return pd.read_csv(path)


report = {
    "run": RUN_NAME,
    "status": "passed",
    "checks": {},
    "errors": [],
    "warnings": [],
}


# ------------------------------------------------------------
# Required files.
# ------------------------------------------------------------

for path in [
    MATCH_FILE,
    RECORDINGS_FILE,
    CREDITS_FILE,
    RELATIONSHIPS_FILE,
]:

    key = f"file:{path.name}"
    exists = path.exists()

    report["checks"][key] = {
        "exists": exists,
        "path": str(path),
    }

    if not exists:
        report["errors"].append(
            f"Missing required file: {path}"
        )


if report["errors"]:

    report["status"] = "failed"

else:

    matched = load_csv(
        MATCH_FILE
    )

    recordings = load_csv(
        RECORDINGS_FILE
    )

    credits = load_csv(
        CREDITS_FILE
    )

    relationships = load_csv(
        RELATIONSHIPS_FILE
    )

    # --------------------------------------------------------
    # Approved recording count.
    # --------------------------------------------------------

    approved_count = int(
        matched[
            "match_status"
        ]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
        .eq("matched")
        .sum()
    )

    report["checks"][
        "approved_recording_count"
    ] = {
        "matched_tracks": approved_count,
        "recordings": len(recordings),
        "pass":
            approved_count
            == len(recordings),
    }

    if approved_count != len(recordings):

        report["errors"].append(
            "Number of normalized recordings does not "
            "equal number of approved matched tracks."
        )

    # --------------------------------------------------------
    # Recording identity fields.
    # --------------------------------------------------------

    recording_required = [
        "recording_id",
        "title",
        "whosampled_url",
        "spotify_track_id",
        "spotify_isrc",
    ]

    for column in recording_required:

        present = (
            column in recordings.columns
        )

        report["checks"][
            f"recordings_column:{column}"
        ] = {
            "present": present
        }

        if not present:
            report["errors"].append(
                f"recordings.csv missing column: {column}"
            )

    # --------------------------------------------------------
    # YouTube coverage.
    # --------------------------------------------------------

    if {
        "youtube_video_id",
        "youtube_url",
    }.issubset(
        recordings.columns
    ):

        youtube_id_count = int(
            recordings[
                "youtube_video_id"
            ]
            .fillna("")
            .astype(str)
            .str.strip()
            .ne("")
            .sum()
        )

        youtube_url_count = int(
            recordings[
                "youtube_url"
            ]
            .fillna("")
            .astype(str)
            .str.strip()
            .ne("")
            .sum()
        )

        report["checks"][
            "youtube_coverage"
        ] = {
            "recordings": len(recordings),
            "video_ids": youtube_id_count,
            "urls": youtube_url_count,
            "all_have_video_id":
                youtube_id_count
                == len(recordings),
            "all_have_url":
                youtube_url_count
                == len(recordings),
        }

        if youtube_id_count != len(recordings):
            report["warnings"].append(
                "Not every recording has a YouTube video ID."
            )

        if youtube_url_count != len(recordings):
            report["warnings"].append(
                "Not every recording has a YouTube URL."
            )

    # --------------------------------------------------------
    # Artwork coverage.
    # --------------------------------------------------------

    if {
        "whosampled_thumbnail_url",
        "whosampled_thumbnail_path",
    }.issubset(
        recordings.columns
    ):

        remote_art_count = int(
            recordings[
                "whosampled_thumbnail_url"
            ]
            .fillna("")
            .astype(str)
            .str.strip()
            .ne("")
            .sum()
        )

        local_paths = [
            Path(clean(value))
            for value in recordings[
                "whosampled_thumbnail_path"
            ].fillna("")
            if clean(value)
        ]

        local_exists = sum(
            path.exists()
            for path in local_paths
        )

        report["checks"][
            "artwork_coverage"
        ] = {
            "remote_urls":
                remote_art_count,
            "local_paths":
                len(local_paths),
            "local_files_present":
                local_exists,
            "partial_cache_expected":
                True,
        }

    # --------------------------------------------------------
    # Credit integrity.
    # --------------------------------------------------------

    credit_required = [
        "credit_id",
        "recording_id",
        "artist_name",
        "role",
        "source",
        "source_url",
    ]

    for column in credit_required:

        if column not in credits.columns:
            report["errors"].append(
                f"credits.csv missing column: {column}"
            )

    if "recording_id" in credits.columns:

        recording_id_set = set(
            recordings[
                "recording_id"
            ]
            .fillna("")
            .astype(str)
        )

        invalid_credit_ids = sorted(
            set(
                credits[
                    "recording_id"
                ]
                .fillna("")
                .astype(str)
            )
            - recording_id_set
        )

        report["checks"][
            "credit_recording_references"
        ] = {
            "invalid_count":
                len(invalid_credit_ids),
            "invalid_ids":
                invalid_credit_ids,
        }

        if invalid_credit_ids:
            report["errors"].append(
                "credits.csv contains recording IDs "
                "not present in recordings.csv."
            )

    # --------------------------------------------------------
    # Relationship integrity.
    # --------------------------------------------------------

    relationship_required = [
        "relationship_id",
        "source_recording_id",
        "target_recording_id",
        "relationship_type",
        "whosampled_relationship_url",
    ]

    for column in relationship_required:

        if column not in relationships.columns:
            report["errors"].append(
                f"relationships.csv missing column: {column}"
            )

    if "source_recording_id" in relationships.columns:

        recording_id_set = set(
            recordings[
                "recording_id"
            ]
            .fillna("")
            .astype(str)
        )

        invalid_source_ids = sorted(
            set(
                relationships[
                    "source_recording_id"
                ]
                .fillna("")
                .astype(str)
            )
            - recording_id_set
        )

        report["checks"][
            "relationship_source_references"
        ] = {
            "invalid_count":
                len(invalid_source_ids),
            "invalid_ids":
                invalid_source_ids,
        }

        if invalid_source_ids:
            report["errors"].append(
                "relationships.csv contains source "
                "recording IDs not present in recordings.csv."
            )

    # Target IDs should currently be blank.
    if "target_recording_id" in relationships.columns:

        target_ids = (
            relationships[
                "target_recording_id"
            ]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        populated_targets = int(
            target_ids.ne("").sum()
        )

        report["checks"][
            "target_recording_ids"
        ] = {
            "populated":
                populated_targets,
            "expected":
                0,
            "status":
                "expected_blank_until_related_track_collection",
        }

    # --------------------------------------------------------
    # Duplicate canonical recording IDs.
    # --------------------------------------------------------

    duplicate_recording_ids = (
        recordings[
            "recording_id"
        ]
        .duplicated()
        .sum()
    )

    report["checks"][
        "duplicate_recording_ids"
    ] = {
        "duplicates":
            int(duplicate_recording_ids),
    }

    if duplicate_recording_ids:
        report["errors"].append(
            "Duplicate recording IDs found."
        )

    # --------------------------------------------------------
    # Final status.
    # --------------------------------------------------------

    if report["errors"]:
        report["status"] = "failed"
    elif report["warnings"]:
        report["status"] = "passed_with_warnings"

report["summary"] = {
    "recordings":
        len(recordings)
        if "recordings" in locals()
        else 0,

    "credits":
        len(credits)
        if "credits" in locals()
        else 0,

    "relationships":
        len(relationships)
        if "relationships" in locals()
        else 0,
}

REPORT_FILE.write_text(
    json.dumps(
        report,
        indent=2,
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

print()
print("=" * 80)
print("PHASE 1 OUTPUT VALIDATION")
print("=" * 80)

print(
    "Status:",
    report["status"],
)

print(
    "Recordings:",
    report["summary"]["recordings"],
)

print(
    "Credits:",
    report["summary"]["credits"],
)

print(
    "Relationships:",
    report["summary"]["relationships"],
)

if report["errors"]:

    print()
    print("ERRORS:")

    for error in report["errors"]:
        print(
            " -",
            error,
        )

if report["warnings"]:

    print()
    print("WARNINGS:")

    for warning in report["warnings"]:
        print(
            " -",
            warning,
        )

print()
print(
    "Report:",
    REPORT_FILE,
)

if report["status"] == "failed":
    raise SystemExit(1)
