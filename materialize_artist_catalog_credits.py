#!/usr/bin/env python3

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd


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


def stable_id(prefix, *parts):
    payload = "|".join(clean(p) for p in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def backup_file(path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.bak_{stamp}")
    shutil.copy2(path, backup)
    return backup


def parse_functions(value):
    value = clean(value)
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError(f"Function value is not a JSON list: {value!r}")
    return sorted({clean(v) for v in parsed if clean(v)})


def serialize_functions(values):
    values = sorted({clean(v) for v in values if clean(v)})
    return "" if not values else json.dumps(
        values, ensure_ascii=False, separators=(",", ":")
    )


def main():
    parser = argparse.ArgumentParser(
        description="Idempotent post-migration artist catalog credit materializer."
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run = Path(args.run_dir)
    credits_path = run / "credits.csv"
    evidence_path = run / "credit_evidence.csv"
    catalog_path = run / "artist_catalog_credits_parsed.csv"

    for path in (credits_path, evidence_path, catalog_path):
        if not path.exists():
            raise SystemExit(f"Missing: {path}")

    credits = pd.read_csv(credits_path, dtype=str, keep_default_na=False)
    evidence = pd.read_csv(evidence_path, dtype=str, keep_default_na=False)
    catalog = pd.read_csv(catalog_path, dtype=str, keep_default_na=False)

    required_credit = {
        "credit_id", "recording_id", "artist_id", "artist_name",
        "role", "function", "artist_order",
    }
    required_evidence = {
        "credit_evidence_id", "credit_id", "source_role", "source", "source_url",
    }
    required_catalog = {
        "canonical_recording_id", "canonical_artist_id",
        "canonical_artist_name", "artist_name", "source_role",
        "artist_order", "source", "source_url",
    }

    for label, df, required in [
        ("credits.csv", credits, required_credit),
        ("credit_evidence.csv", evidence, required_evidence),
        ("artist_catalog_credits_parsed.csv", catalog, required_catalog),
    ]:
        missing = required - set(df.columns)
        if missing:
            raise SystemExit(f"{label} missing columns: {sorted(missing)}")

    credit_key_cols = ["recording_id", "artist_id", "role"]
    evidence_key_cols = ["credit_id", "source_role", "source", "source_url"]

    if credits.duplicated(credit_key_cols).any():
        raise SystemExit("FAILED SAFELY: duplicate canonical credit keys already exist.")
    if credits["credit_id"].duplicated().any():
        raise SystemExit("FAILED SAFELY: duplicate credit IDs already exist.")
    if evidence.duplicated(evidence_key_cols).any():
        raise SystemExit("FAILED SAFELY: duplicate evidence assertions already exist.")
    if evidence["credit_evidence_id"].duplicated().any():
        raise SystemExit("FAILED SAFELY: duplicate evidence IDs already exist.")

    credit_ids = set(credits["credit_id"].map(clean))
    missing_refs = {
        clean(v) for v in evidence["credit_id"]
        if clean(v) and clean(v) not in credit_ids
    }
    if missing_refs:
        raise SystemExit(
            f"FAILED SAFELY: {len(missing_refs)} evidence rows reference missing credits."
        )

    key_to_index = {}
    key_to_credit_id = {}
    for idx, row in credits.iterrows():
        key = (
            clean(row["recording_id"]),
            clean(row["artist_id"]),
            clean(row["role"]),
        )
        key_to_index[key] = idx
        key_to_credit_id[key] = clean(row["credit_id"])

    existing_evidence_keys = {
        (
            clean(row["credit_id"]),
            clean(row["source_role"]),
            clean(row["source"]),
            clean(row["source_url"]),
        )
        for _, row in evidence.iterrows()
    }

    eligible_mask = (
        catalog["canonical_recording_id"].map(clean).ne("")
        & catalog["canonical_artist_id"].map(clean).ne("")
    )
    eligible = catalog[eligible_mask].copy()
    deferred = catalog[~eligible_mask].copy()

    proposed_credits = []
    proposed_evidence = []
    proposed_evidence_keys = set()
    proposed_key_to_credit_id = {}
    function_updates = {}
    order_conflicts = []
    unmapped = []
    already_materialized = 0

    for idx, row in eligible.iterrows():
        source_role = clean(row["source_role"])
        if source_role not in CATALOG_NORMALIZATION:
            unmapped.append((idx, source_role))
            continue

        role, new_function = CATALOG_NORMALIZATION[source_role]
        recording_id = clean(row["canonical_recording_id"])
        artist_id = clean(row["canonical_artist_id"])
        artist_name = (
            clean(row["canonical_artist_name"]) or clean(row["artist_name"])
        )
        artist_order = clean(row["artist_order"])
        source = clean(row["source"])
        source_url = clean(row["source_url"])

        key = (recording_id, artist_id, role)

        if key in key_to_credit_id:
            cid = key_to_credit_id[key]
            cidx = key_to_index[key]

            existing_order = clean(credits.at[cidx, "artist_order"])
            if existing_order and artist_order and existing_order != artist_order:
                order_conflicts.append(
                    (key, existing_order, artist_order, source_url)
                )

            if new_function:
                existing_functions = parse_functions(credits.at[cidx, "function"])
                combined = sorted(set(existing_functions + [new_function]))
                if combined != existing_functions:
                    function_updates[key] = combined

        elif key in proposed_key_to_credit_id:
            cid = proposed_key_to_credit_id[key]
            if new_function:
                for proposed in proposed_credits:
                    if proposed["_key"] == key:
                        funcs = parse_functions(proposed["function"])
                        proposed["function"] = serialize_functions(
                            funcs + [new_function]
                        )
                        break
        else:
            cid = stable_id("CRD", recording_id, artist_id, role)
            proposed_key_to_credit_id[key] = cid
            proposed_credits.append({
                "_key": key,
                "credit_id": cid,
                "recording_id": recording_id,
                "artist_id": artist_id,
                "artist_name": artist_name,
                "role": role,
                "function": serialize_functions(
                    [new_function] if new_function else []
                ),
                "artist_order": artist_order,
            })

        evidence_key = (cid, source_role, source, source_url)

        if evidence_key in existing_evidence_keys:
            already_materialized += 1
        elif evidence_key not in proposed_evidence_keys:
            proposed_evidence_keys.add(evidence_key)
            proposed_evidence.append({
                "credit_evidence_id": stable_id(
                    "CRDE", cid, source_role, source, source_url
                ),
                "credit_id": cid,
                "source_role": source_role,
                "source": source,
                "source_url": source_url,
            })

    print("=" * 100)
    print("POST-MIGRATION ARTIST CATALOG CREDIT MATERIALIZER")
    print("=" * 100)
    print("Mode:", "DRY RUN" if args.dry_run else "WRITE")

    print("\nCURRENT")
    print("-" * 100)
    print("Canonical credits:", len(credits))
    print("Credit evidence:", len(evidence))
    print("Eligible catalog assertions:", len(eligible))
    print("Deferred catalog assertions:", len(deferred))

    print("\nPROPOSED")
    print("-" * 100)
    print("New canonical credits:", len(proposed_credits))
    print("New evidence assertions:", len(proposed_evidence))
    print("Catalog assertions already materialized:", already_materialized)
    print("Existing credits receiving new functions:", len(function_updates))
    print("Unmapped eligible source roles:", len(unmapped))
    print("Artist-order conflicts:", len(order_conflicts))

    print("\nEXPECTED AFTER WRITE")
    print("-" * 100)
    print("Canonical credits:", len(credits) + len(proposed_credits))
    print("Credit evidence:", len(evidence) + len(proposed_evidence))

    if unmapped:
        print("\nUNMAPPED")
        for idx, source_role in unmapped:
            print(idx, source_role)

    if order_conflicts:
        print("\nARTIST-ORDER CONFLICTS")
        for key, existing_order, incoming_order, source_url in order_conflicts:
            print(key)
            print("  existing:", existing_order)
            print("  incoming:", incoming_order)
            print("  source:", source_url)

    if unmapped or order_conflicts:
        print("\nFAILED SAFELY.")
        print("No files modified.")
        raise SystemExit(1)

    simulated_credits = credits.copy()
    for key, functions in function_updates.items():
        simulated_credits.at[key_to_index[key], "function"] = serialize_functions(functions)

    if proposed_credits:
        new_rows = [
            {k: v for k, v in row.items() if k != "_key"}
            for row in proposed_credits
        ]
        simulated_credits = pd.concat(
            [simulated_credits, pd.DataFrame(new_rows)],
            ignore_index=True,
        )

    simulated_evidence = evidence.copy()
    if proposed_evidence:
        simulated_evidence = pd.concat(
            [simulated_evidence, pd.DataFrame(proposed_evidence)],
            ignore_index=True,
        )

    if simulated_credits.duplicated(credit_key_cols).any():
        raise SystemExit("FAILED SAFELY: simulation creates duplicate canonical keys.")
    if simulated_credits["credit_id"].duplicated().any():
        raise SystemExit("FAILED SAFELY: simulation creates duplicate credit IDs.")
    if simulated_evidence.duplicated(evidence_key_cols).any():
        raise SystemExit("FAILED SAFELY: simulation creates duplicate evidence assertions.")
    if simulated_evidence["credit_evidence_id"].duplicated().any():
        raise SystemExit("FAILED SAFELY: simulation creates duplicate evidence IDs.")

    simulated_credit_ids = set(simulated_credits["credit_id"].map(clean))
    missing_after = {
        clean(v) for v in simulated_evidence["credit_id"]
        if clean(v) and clean(v) not in simulated_credit_ids
    }
    if missing_after:
        raise SystemExit("FAILED SAFELY: simulation creates missing credit references.")

    print("\nINTEGRITY")
    print("-" * 100)
    print("Duplicate canonical keys after simulation: 0")
    print("Duplicate credit IDs after simulation: 0")
    print("Duplicate evidence assertions after simulation: 0")
    print("Duplicate evidence IDs after simulation: 0")
    print("Missing evidence -> credit references after simulation: 0")

    if args.dry_run:
        print("\nDRY RUN PASSED.")
        print("No files modified.")
        return

    if not proposed_credits and not proposed_evidence and not function_updates:
        print("\nNO CHANGES NEEDED.")
        print("Files already represent all eligible catalog assertions.")
        return

    credits_backup = backup_file(credits_path)
    evidence_backup = backup_file(evidence_path)

    simulated_credits.to_csv(credits_path, index=False)
    simulated_evidence.to_csv(evidence_path, index=False)

    print("\nWRITE COMPLETE")
    print("credits.csv backup:", credits_backup)
    print("credit_evidence.csv backup:", evidence_backup)
    print("Canonical credits after:", len(simulated_credits))
    print("Credit evidence after:", len(simulated_evidence))
    print("PASSED.")


if __name__ == "__main__":
    main()
