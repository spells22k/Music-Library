#!/usr/bin/env python3

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd


TABLES = [
    ("recordings", "recordings.csv", "recording_id"),
    ("artists", "artists.csv", "artist_id"),
    ("credits", "credits.csv", "credit_id"),
    ("credit_evidence", "credit_evidence.csv", "credit_evidence_id"),
    ("relationships", "relationships.csv", "relationship_id"),
]


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(str(path))
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def backup_path(path):
    candidate = path.with_suffix(path.suffix + ".bak")
    n = 1
    while candidate.exists():
        candidate = path.with_suffix(path.suffix + f".bak{n}")
        n += 1
    return candidate


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def merge_table(label, proposed, existing, id_col):
    if list(proposed.columns) != list(existing.columns):
        raise ValueError(
            f"{label}: schema mismatch.\n"
            f"  proposed: {list(proposed.columns)}\n"
            f"  library:  {list(existing.columns)}"
        )

    if id_col not in proposed.columns:
        raise ValueError(f"{label}: missing ID column {id_col!r}")

    if (proposed[id_col] == "").any():
        raise ValueError(f"{label}: proposed table contains blank {id_col}")

    if (existing[id_col] == "").any():
        raise ValueError(f"{label}: Library table contains blank {id_col}")

    if proposed[id_col].duplicated().any():
        dupes = proposed.loc[proposed[id_col].duplicated(keep=False), id_col].tolist()
        raise ValueError(f"{label}: duplicate proposed IDs: {dupes[:10]}")

    if existing[id_col].duplicated().any():
        dupes = existing.loc[existing[id_col].duplicated(keep=False), id_col].tolist()
        raise ValueError(f"{label}: duplicate Library IDs: {dupes[:10]}")

    columns = list(existing.columns)
    value_columns = [c for c in columns if c != id_col]

    existing_by_id = {
        row[id_col]: row.to_dict()
        for _, row in existing.iterrows()
    }

    final_by_id = {
        entity_id: dict(row)
        for entity_id, row in existing_by_id.items()
    }

    stats = {
        "proposed": len(proposed),
        "new": 0,
        "identical": 0,
        "enriched_entities": set(),
        "enriched_cells": 0,
        "preserved_cells": 0,
        "conflicts": [],
    }

    for _, proposed_row in proposed.iterrows():
        entity_id = proposed_row[id_col]
        proposal = proposed_row.to_dict()

        if entity_id not in existing_by_id:
            final_by_id[entity_id] = proposal
            stats["new"] += 1
            continue

        library_row = existing_by_id[entity_id]
        final_row = dict(library_row)

        exact = all(
            proposal[col] == library_row[col]
            for col in value_columns
        )

        if exact:
            stats["identical"] += 1
            continue

        for col in value_columns:
            old = library_row[col]
            new = proposal[col]

            if old == new:
                continue

            if old == "" and new != "":
                final_row[col] = new
                stats["enriched_entities"].add(entity_id)
                stats["enriched_cells"] += 1
                continue

            if old != "" and new == "":
                stats["preserved_cells"] += 1
                continue

            # Different nonblank values are deliberately conservative:
            # preserve the durable Library and report the conflict.
            stats["conflicts"].append({
                "id": entity_id,
                "column": col,
                "library_value": old,
                "proposed_value": new,
            })

        final_by_id[entity_id] = final_row

    # Preserve Library row order; append genuinely new proposal rows in proposal order.
    existing_ids = list(existing[id_col])
    new_ids = [
        entity_id
        for entity_id in proposed[id_col]
        if entity_id not in existing_by_id
    ]
    final_ids = existing_ids + new_ids

    final = pd.DataFrame(
        [final_by_id[entity_id] for entity_id in final_ids],
        columns=columns,
    )

    stats["enriched_entities"] = len(stats["enriched_entities"])
    return final, stats


def integrity_errors(tables):
    recordings = tables["recordings"]
    artists = tables["artists"]
    credits = tables["credits"]
    evidence = tables["credit_evidence"]
    relationships = tables["relationships"]

    recording_ids = set(recordings["recording_id"])
    artist_ids = set(artists["artist_id"])
    credit_ids = set(credits["credit_id"])

    errors = {}

    errors["Credit -> missing Recording"] = int(
        (~credits["recording_id"].isin(recording_ids)).sum()
    )

    errors["Credit -> missing Artist"] = int(
        (
            (credits["artist_id"] != "")
            & ~credits["artist_id"].isin(artist_ids)
        ).sum()
    )

    errors["Evidence -> missing Credit"] = int(
        (~evidence["credit_id"].isin(credit_ids)).sum()
    )

    errors["Relationship -> missing source"] = int(
        (
            (relationships["source_recording_id"] != "")
            & ~relationships["source_recording_id"].isin(recording_ids)
        ).sum()
    )

    errors["Relationship -> missing target"] = int(
        (
            (relationships["target_recording_id"] != "")
            & ~relationships["target_recording_id"].isin(recording_ids)
        ).sum()
    )

    return errors


def atomic_write(final_tables, library_dir):
    library_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=".shared_library_materialize_",
            dir=library_dir,
        )
    )

    backups = []

    try:
        temp_paths = {}

        for label, filename, _ in TABLES:
            temp_path = temp_dir / filename
            final_tables[label].to_csv(
                temp_path,
                index=False,
                encoding="utf-8",
            )
            temp_paths[label] = temp_path

            # Verify that what we wrote can be parsed and has the expected shape.
            check = read_csv(temp_path)
            expected = final_tables[label]

            if list(check.columns) != list(expected.columns):
                raise RuntimeError(f"{filename}: temporary write schema verification failed")

            if len(check) != len(expected):
                raise RuntimeError(f"{filename}: temporary write row-count verification failed")

        # Back up every durable table before replacing any of them.
        for _, filename, _ in TABLES:
            destination = library_dir / filename
            backup = backup_path(destination)
            shutil.copy2(destination, backup)

            if sha256(destination) != sha256(backup):
                raise RuntimeError(f"{filename}: backup byte verification failed")

            backups.append((destination, backup))

        # Replace only after every temp file and every backup is ready.
        for label, filename, _ in TABLES:
            destination = library_dir / filename
            temp_paths[label].replace(destination)

        # Final parse verification.
        for label, filename, _ in TABLES:
            written = read_csv(library_dir / filename)
            expected = final_tables[label]

            if list(written.columns) != list(expected.columns):
                raise RuntimeError(f"{filename}: final schema verification failed")

            if len(written) != len(expected):
                raise RuntimeError(f"{filename}: final row-count verification failed")

        return [backup for _, backup in backups]

    except Exception:
        # Best-effort rollback from verified backups if replacement began.
        for destination, backup in backups:
            if backup.exists():
                shutil.copy2(backup, destination)
        raise

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Safely materialize run-local normalized proposals into the "
            "durable shared music Library."
        )
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--library-dir", required=True)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Commit validated changes. Without --write, this is a dry run.",
    )
    parser.add_argument(
        "--allow-conflicts",
        action="store_true",
        help=(
            "Permit a write when nonblank conflicts exist. Conflicting Library "
            "values are still preserved. Default behavior blocks writes."
        ),
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    library_dir = Path(args.library_dir)

    print("=" * 100)
    print("SHARED LIBRARY MATERIALIZATION")
    print("=" * 100)
    print("Run directory:           ", run_dir)
    print("Shared Library directory:", library_dir)
    print("Mode:                    ", "WRITE" if args.write else "DRY RUN")
    print()

    proposals = {}
    existing = {}
    final_tables = {}
    stats_by_table = {}

    try:
        for label, filename, id_col in TABLES:
            proposals[label] = read_csv(run_dir / filename)
            existing[label] = read_csv(library_dir / filename)

            final, stats = merge_table(
                label,
                proposals[label],
                existing[label],
                id_col,
            )

            final_tables[label] = final
            stats_by_table[label] = stats

    except Exception as exc:
        print("PRECHECK FAILED")
        print(exc)
        print("No files were changed.")
        return 1

    total_conflicts = 0
    total_changes = 0

    for label, _, _ in TABLES:
        stats = stats_by_table[label]
        conflicts = stats["conflicts"]
        total_conflicts += len(conflicts)

        total_changes += (
            stats["new"]
            + stats["enriched_cells"]
        )

        print("-" * 100)
        print(label.upper())
        print("-" * 100)
        print("Proposed rows:          ", stats["proposed"])
        print("New rows:               ", stats["new"])
        print("Identical existing rows:", stats["identical"])
        print("Enriched entities:      ", stats["enriched_entities"])
        print("Enriched cells:         ", stats["enriched_cells"])
        print("Preserved Library cells:", stats["preserved_cells"])
        print("Nonblank conflicts:     ", len(conflicts))

        if conflicts:
            print()
            print("CONFLICTS (first 50):")
            for conflict in conflicts[:50]:
                print(
                    f"  {conflict['id']} | {conflict['column']} | "
                    f"LIBRARY={conflict['library_value']!r} | "
                    f"PROPOSED={conflict['proposed_value']!r}"
                )
            if len(conflicts) > 50:
                print(f"  ... {len(conflicts) - 50} more")
        print()

    print("=" * 100)
    print("FINAL RELATIONAL INTEGRITY")
    print("=" * 100)

    errors = integrity_errors(final_tables)

    for label, count in errors.items():
        print(f"{label}: {count}")

    integrity_failure = any(errors.values())

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print("New/enriched changes:", total_changes)
    print("Nonblank conflicts:  ", total_conflicts)
    print("Integrity failures:  ", sum(errors.values()))
    print()

    if integrity_failure:
        print("MATERIALIZATION BLOCKED: relational integrity would fail.")
        print("No files were changed.")
        return 1

    if total_conflicts and not args.allow_conflicts:
        print("MATERIALIZATION BLOCKED: nonblank conflicts require review.")
        print("Existing Library values were preserved in the proposal.")
        print("No files were changed.")
        return 2

    if not args.write:
        print("DRY RUN PASSED")
        print("No files were written.")
        return 0

    if total_changes == 0:
        print("WRITE PASSED — no changes were necessary.")
        print("No Library files were rewritten.")
        return 0

    try:
        backups = atomic_write(final_tables, library_dir)
    except Exception as exc:
        print("WRITE FAILED")
        print(repr(exc))
        print("Rollback was attempted from verified backups.")
        return 1

    print("WRITE PASSED")
    print("Shared Library updated atomically after validation.")
    print("Backups:")
    for path in backups:
        print(" ", path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
