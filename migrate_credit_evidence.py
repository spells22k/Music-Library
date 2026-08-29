#!/usr/bin/env python3

import argparse
import hashlib
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd


LEGACY_ROLE_NORMALIZATION = {
    "produced_by": "producer",
}

CATALOG_NORMALIZATION = {
    "WhoSampled track artist": ("performer", ""),
    "Producer": ("producer", ""),
    "Composer": ("composer", ""),
    "Composers": ("composer", ""),
    "Lyricist": ("lyricist", ""),
    "Writer": ("writer", ""),
    "Guest Vocals": ("performer", "guest_vocals"),
}


def clean(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalize_existing_role(role):
    role = clean(role)
    return LEGACY_ROLE_NORMALIZATION.get(role, role)


def stable_id(prefix, *parts):
    payload = "|".join(clean(p) for p in parts)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def backup_file(path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.bak_{stamp}")
    shutil.copy2(path, backup)
    return backup


def canonical_artist_names(artists):
    result = {}
    if artists is None:
        return result
    for _, row in artists.iterrows():
        artist_id = clean(row.get("artist_id", ""))
        if not artist_id:
            continue
        name = (
            clean(row.get("canonical_name", ""))
            or clean(row.get("artist_name", ""))
            or clean(row.get("name", ""))
        )
        if name:
            result[artist_id] = name
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Migrate canonical credits into separate credit and evidence layers."
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run = Path(args.run_dir)
    credits_path = run / "credits.csv"
    catalog_path = run / "artist_catalog_credits_parsed.csv"
    artists_path = run / "artists.csv"
    evidence_path = run / "credit_evidence.csv"

    if not credits_path.exists():
        raise SystemExit(f"Missing: {credits_path}")
    if not catalog_path.exists():
        raise SystemExit(f"Missing: {catalog_path}")

    credits = pd.read_csv(credits_path, dtype=str, keep_default_na=False)
    catalog = pd.read_csv(catalog_path, dtype=str, keep_default_na=False)
    artists = (
        pd.read_csv(artists_path, dtype=str, keep_default_na=False)
        if artists_path.exists()
        else None
    )
    artist_names = canonical_artist_names(artists)

    # Canonical relationship key:
    # Recording + Artist + normalized role.
    groups = defaultdict(list)

    for idx, row in credits.iterrows():
        recording_id = clean(row.get("recording_id", ""))
        artist_id = clean(row.get("artist_id", ""))
        role = normalize_existing_role(row.get("role", ""))

        if not recording_id or not artist_id or not role:
            raise SystemExit(
                f"Existing credits.csv row {idx} lacks recording_id, artist_id, or role."
            )

        key = (recording_id, artist_id, role)
        groups[key].append({
            "origin": "existing",
            "recording_id": recording_id,
            "artist_id": artist_id,
            "artist_name": clean(row.get("artist_name", "")),
            "role": role,
            "function": clean(row.get("function", "")),
            "artist_order": clean(row.get("artist_order", "")),
            "source_role": clean(row.get("source_role", "")),
            "source": clean(row.get("source", "")),
            "source_url": clean(row.get("source_url", "")),
        })

    eligible_mask = (
        catalog["canonical_recording_id"].map(clean).ne("")
        & catalog["canonical_artist_id"].map(clean).ne("")
    )
    eligible = catalog[eligible_mask].copy()
    deferred = catalog[~eligible_mask].copy()

    unmapped = []

    for idx, row in eligible.iterrows():
        source_role = clean(row.get("source_role", ""))
        if source_role not in CATALOG_NORMALIZATION:
            unmapped.append((idx, source_role))
            continue

        role, function = CATALOG_NORMALIZATION[source_role]
        recording_id = clean(row.get("canonical_recording_id", ""))
        artist_id = clean(row.get("canonical_artist_id", ""))

        key = (recording_id, artist_id, role)
        groups[key].append({
            "origin": "catalog",
            "recording_id": recording_id,
            "artist_id": artist_id,
            "artist_name": (
                clean(row.get("canonical_artist_name", ""))
                or clean(row.get("artist_name", ""))
            ),
            "role": role,
            "function": function,
            "artist_order": clean(row.get("artist_order", "")),
            "source_role": source_role,
            "source": clean(row.get("source", "")),
            "source_url": clean(row.get("source_url", "")),
        })

    if unmapped:
        print("UNMAPPED ELIGIBLE SOURCE ROLES:")
        for idx, role in unmapped:
            print(idx, role)
        print("FAILED SAFELY. No files modified.")
        raise SystemExit(1)

    # Validate artist order and build canonical credits.
    canonical_rows = []
    evidence_rows = []
    order_conflicts = []

    for key in sorted(groups):
        recording_id, artist_id, role = key
        assertions = groups[key]

        orders = sorted({
            clean(a["artist_order"])
            for a in assertions
            if clean(a["artist_order"])
        })
        if len(orders) > 1:
            order_conflicts.append((key, orders))
            continue

        functions = sorted({
            clean(a["function"])
            for a in assertions
            if clean(a["function"])
        })

        # Current CSV representation uses a JSON array string so multiple
        # functions can later coexist without becoming separate relationships.
        function_value = json_array(functions) if functions else ""

        canonical_name = artist_names.get(artist_id, "")
        if not canonical_name:
            # Prefer a reconciled/catalog spelling if present, otherwise first.
            names = [clean(a["artist_name"]) for a in assertions if clean(a["artist_name"])]
            canonical_name = names[0] if names else ""

        cid = stable_id("CRD", recording_id, artist_id, role)

        canonical_rows.append({
            "credit_id": cid,
            "recording_id": recording_id,
            "artist_id": artist_id,
            "artist_name": canonical_name,
            "role": role,
            "function": function_value,
            "artist_order": orders[0] if orders else "",
        })

        for a in assertions:
            source_role = clean(a["source_role"])
            source = clean(a["source"])
            source_url = clean(a["source_url"])
            eid = stable_id(
                "CRDE",
                cid,
                source_role,
                source,
                source_url,
            )
            evidence_rows.append({
                "credit_evidence_id": eid,
                "credit_id": cid,
                "source_role": source_role,
                "source": source,
                "source_url": source_url,
            })

    if order_conflicts:
        print("ARTIST-ORDER CONFLICTS:")
        for key, orders in order_conflicts:
            print(key, orders)
        print("FAILED SAFELY. No files modified.")
        raise SystemExit(1)

    canonical_out = pd.DataFrame(canonical_rows)
    evidence_out = pd.DataFrame(evidence_rows)

    # Exact evidence duplicates are not silently discarded.
    evidence_key_cols = ["credit_id", "source_role", "source", "source_url"]
    dup_evidence = evidence_out.duplicated(evidence_key_cols, keep=False)
    if dup_evidence.any():
        print("EXACT DUPLICATE EVIDENCE DETECTED:")
        print(evidence_out.loc[dup_evidence, evidence_key_cols].to_string(index=False))
        print("FAILED SAFELY. No files modified.")
        raise SystemExit(1)

    canonical_key_cols = ["recording_id", "artist_id", "role"]
    if canonical_out.duplicated(canonical_key_cols).any():
        raise RuntimeError("Duplicate canonical credit keys generated.")

    if canonical_out["credit_id"].duplicated().any():
        raise RuntimeError("Duplicate credit_id generated.")

    if evidence_out["credit_evidence_id"].duplicated().any():
        raise RuntimeError("Duplicate credit_evidence_id generated.")

    missing_credit_refs = set(evidence_out["credit_id"]) - set(canonical_out["credit_id"])
    if missing_credit_refs:
        raise RuntimeError(
            f"Evidence references missing canonical credits: {len(missing_credit_refs)}"
        )

    existing_normalized_keys = {
        (
            clean(row.get("recording_id", "")),
            clean(row.get("artist_id", "")),
            normalize_existing_role(row.get("role", "")),
        )
        for _, row in credits.iterrows()
    }
    final_keys = {
        tuple(row)
        for row in canonical_out[canonical_key_cols].itertuples(index=False, name=None)
    }
    new_keys = final_keys - existing_normalized_keys

    print("=" * 100)
    print("CREDIT / CREDIT-EVIDENCE MIGRATION")
    print("=" * 100)
    print("Mode:", "DRY RUN" if args.dry_run else "WRITE")
    print()
    print("Existing credits.csv rows:", len(credits))
    print("Existing normalized canonical relationships:", len(existing_normalized_keys))
    print("Existing duplicate relationships collapsed:", len(credits) - len(existing_normalized_keys))
    print("Legacy produced_by rows normalized:", (credits["role"].map(clean) == "produced_by").sum())
    print("Eligible catalog assertions:", len(eligible))
    print("Deferred catalog assertions:", len(deferred))
    print("New catalog canonical relationships:", len(new_keys))
    print()
    print("FINAL")
    print("Canonical credits:", len(canonical_out))
    print("Credit evidence rows:", len(evidence_out))
    print("Duplicate canonical keys:", canonical_out.duplicated(canonical_key_cols).sum())
    print("Duplicate evidence assertions:", evidence_out.duplicated(evidence_key_cols).sum())
    print("Missing evidence -> credit references:", len(missing_credit_refs))
    print("Legacy produced_by remaining:", (canonical_out["role"] == "produced_by").sum())

    if len(credits) == 160 and len(eligible) == 78:
        expected = {
            "canonical": 219,
            "evidence": 238,
            "new": 69,
        }
        print()
        print("CHECKPOINT EXPECTATIONS")
        print("Expected canonical credits:", expected["canonical"])
        print("Expected evidence rows:", expected["evidence"])
        print("Expected new catalog relationships:", expected["new"])
        if (
            len(canonical_out) != expected["canonical"]
            or len(evidence_out) != expected["evidence"]
            or len(new_keys) != expected["new"]
        ):
            print("FAILED SAFELY: checkpoint counts differ from approved preflight.")
            print("No files modified.")
            raise SystemExit(1)

    if args.dry_run:
        print()
        print("DRY RUN PASSED.")
        print("No files modified.")
        return

    # Back up credits.csv before mutation.
    credits_backup = backup_file(credits_path)

    # credit_evidence.csv should not already exist for this first migration.
    # If it does, back it up rather than overwriting without protection.
    evidence_backup = None
    if evidence_path.exists():
        evidence_backup = backup_file(evidence_path)

    canonical_out.to_csv(credits_path, index=False)
    evidence_out.to_csv(evidence_path, index=False)

    print()
    print("WRITE COMPLETE")
    print("credits.csv backup:", credits_backup)
    if evidence_backup:
        print("credit_evidence.csv backup:", evidence_backup)
    print("Updated:", credits_path)
    print("Created/updated:", evidence_path)
    print("PASSED.")


def json_array(values):
    # Compact, deterministic JSON serialization.
    import json
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
