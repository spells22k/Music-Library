import argparse
from pathlib import Path

import pandas as pd


COLUMNS = [
    "provisional_artist_id",
    "artist_name",
    "whosampled_url",
    "evidence_type",
    "evidence_recording_id",
    "evidence_recording_url",
]

KEY_COLUMNS = [
    "provisional_artist_id",
    "whosampled_url",
    "evidence_type",
    "evidence_recording_id",
]


def clean(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    return str(value).strip()


def normalized_url(value):
    return clean(value).rstrip("/").casefold()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--run-dir",
        required=True,
    )

    args = parser.parse_args()

    run_dir = Path(args.run_dir)

    related_file = (
        run_dir
        / "related_track_pages"
        / "related_artist_identity_candidates.csv"
    )

    catalog_file = (
        run_dir
        / "artist_catalog_credits_parsed.csv"
    )

    output_file = (
        run_dir
        / "related_track_pages"
        / "artist_identity_candidates.csv"
    )

    if not related_file.exists():
        raise SystemExit(
            f"Missing related-track candidate file: "
            f"{related_file}"
        )

    if not catalog_file.exists():
        raise SystemExit(
            f"Missing catalog credit file: "
            f"{catalog_file}"
        )

    related = pd.read_csv(
        related_file
    ).fillna("")

    catalog = pd.read_csv(
        catalog_file
    ).fillna("")

    missing_related = [
        column
        for column in COLUMNS
        if column not in related.columns
    ]

    if missing_related:
        raise SystemExit(
            "Related-track candidate file is missing "
            "required columns: "
            + ", ".join(missing_related)
        )

    required_catalog = [
        "artist_id",
        "artist_name",
        "role",
        "source_url",
        "canonical_recording_id",
        "artist_whosampled_url",
        "artist_reconciliation_status",
    ]

    missing_catalog = [
        column
        for column in required_catalog
        if column not in catalog.columns
    ]

    if missing_catalog:
        raise SystemExit(
            "Catalog credit file is missing "
            "required columns: "
            + ", ".join(missing_catalog)
        )

    # --------------------------------------------------------
    # Preserve the existing related-track evidence.
    #
    # This file is still produced initially by
    # parse_related_track_pages.py. This stage augments that
    # evidence with unresolved eligible catalog-credit artists.
    # --------------------------------------------------------

    rows = []

    for _, row in related.iterrows():
        item = {
            column: clean(
                row.get(column)
            )
            for column in COLUMNS
        }

        if (
            not item["provisional_artist_id"]
            or not item["artist_name"]
        ):
            continue

        rows.append(item)

    related_rows = len(rows)

    # --------------------------------------------------------
    # Add unresolved catalog-credit artist evidence.
    #
    # Requirements:
    #
    #   * source recording is canonical
    #   * artist identity remains unresolved
    #   * explicit WhoSampled artist profile exists
    #
    # Resolved identities do NOT re-enter review.
    # --------------------------------------------------------

    catalog_rows = 0

    for _, row in catalog.iterrows():
        canonical_recording_id = clean(
            row.get(
                "canonical_recording_id"
            )
        )

        status = clean(
            row.get(
                "artist_reconciliation_status"
            )
        ).casefold()

        ws_url = clean(
            row.get(
                "artist_whosampled_url"
            )
        )

        provisional_artist_id = clean(
            row.get(
                "artist_id"
            )
        )

        artist_name = clean(
            row.get(
                "artist_name"
            )
        )

        if not canonical_recording_id:
            continue

        if status != "unresolved":
            continue

        if not ws_url:
            continue

        if (
            not provisional_artist_id
            or not artist_name
        ):
            continue

        rows.append({
            "provisional_artist_id":
                provisional_artist_id,

            "artist_name":
                artist_name,

            "whosampled_url":
                ws_url,

            "evidence_type":
                clean(
                    row.get("role")
                ),

            "evidence_recording_id":
                canonical_recording_id,

            "evidence_recording_url":
                clean(
                    row.get("source_url")
                ),
        })

        catalog_rows += 1

    # --------------------------------------------------------
    # Deduplicate evidence assertions.
    #
    # URL normalization is used for comparison only. We retain
    # the original URL text in the output.
    # --------------------------------------------------------

    deduplicated = {}

    for row in rows:
        key = (
            clean(
                row.get(
                    "provisional_artist_id"
                )
            ),
            normalized_url(
                row.get(
                    "whosampled_url"
                )
            ),
            clean(
                row.get(
                    "evidence_type"
                )
            ).casefold(),
            clean(
                row.get(
                    "evidence_recording_id"
                )
            ),
        )

        if key not in deduplicated:
            deduplicated[key] = row

    output = pd.DataFrame(
        list(
            deduplicated.values()
        ),
        columns=COLUMNS,
    )

    output = output.sort_values(
        by=[
            "artist_name",
            "provisional_artist_id",
            "evidence_recording_id",
            "evidence_type",
        ],
        key=lambda series: (
            series.astype(str).str.casefold()
        ),
        kind="stable",
    ).reset_index(drop=True)

    # --------------------------------------------------------
    # Safety backup.
    # --------------------------------------------------------

    backup_file = (
        output_file.with_name(
            output_file.stem
            + ".before_catalog_credit_bridge"
            + output_file.suffix
        )
    )

    if output_file.exists():
        existing_bytes = output_file.read_bytes()

        if not backup_file.exists():
            backup_file.write_bytes(
                existing_bytes
            )

    output.to_csv(
        output_file,
        index=False,
        encoding="utf-8",
    )

    print("=" * 100)
    print("ARTIST IDENTITY CANDIDATE EVIDENCE BUILD")
    print("=" * 100)

    print()
    print(
        "Related-track evidence rows read:",
        related_rows,
    )
    print(
        "Eligible catalog-credit rows read:",
        catalog_rows,
    )
    print(
        "Candidate evidence rows written:",
        len(output),
    )

    print()
    print(
        "Output:",
        output_file,
    )
    print(
        "Backup:",
        backup_file,
    )

    print()
    print(
        "No Spotify requests were made."
    )
    print(
        "No WhoSampled requests were made."
    )
    print(
        "No artist identities were merged."
    )


if __name__ == "__main__":
    main()
