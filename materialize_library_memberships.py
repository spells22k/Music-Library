#!/usr/bin/env python3
import argparse
import hashlib
import secrets
import shutil
from datetime import datetime
from pathlib import Path
import pandas as pd

CONTRIBUTOR_COLUMNS = ["contributor_id", "display_name"]
MEMBERSHIP_COLUMNS = [
    "library_membership_id", "contributor_id", "recording_id",
    "added_via", "import_source_type", "import_source_id", "added_at"
]

def clean(v):
    return "" if pd.isna(v) else str(v).strip()

def new_contributor_id():
    return f"CONTRIB_{secrets.token_hex(8)}"

def membership_id(contributor_id, recording_id):
    raw = f"{contributor_id}|{recording_id}"
    return "LIB_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def backup(path):
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = path.with_name(f"{path.name}.bak_{stamp}")
    shutil.copy2(path, out)
    return out

def read_csv(path, required=True):
    if not path.exists():
        if required:
            raise SystemExit(f"FAILED: required file not found: {path}")
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")

def main():
    p = argparse.ArgumentParser(
        description="Materialize hidden Contributor → Recording Library memberships."
    )
    p.add_argument("--run-dir", required=True)
    p.add_argument("--display-name", required=True)
    p.add_argument("--contributor-id", default="")
    p.add_argument("--library-dir", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    library_dir = Path(args.library_dir)
    contributors_path = library_dir / "contributors.csv"
    memberships_path = library_dir / "library_memberships.csv"

    spotify = read_csv(run_dir / "spotify_tracks.csv")
    recordings = read_csv(library_dir / "recordings.csv")

    for c in ["spotify_track_id"]:
        if c not in spotify.columns:
            raise SystemExit(f"FAILED: spotify_tracks.csv missing {c}")
    for c in ["recording_id", "spotify_track_id"]:
        if c not in recordings.columns:
            raise SystemExit(f"FAILED: recordings.csv missing {c}")

    print("=" * 80)
    print("LIBRARY MEMBERSHIP MATERIALIZER")
    print("=" * 80)
    print("Mode:", "DRY RUN" if args.dry_run else "WRITE")
    print("Run directory:", run_dir)
    print("Shared Library directory:", library_dir)
    print("Display name:", args.display_name)

    seed = spotify.copy()
    seed["_spotify_id"] = seed["spotify_track_id"].map(clean)
    if (seed["_spotify_id"] == "").any():
        raise SystemExit("FAILED: bulk import contains blank Spotify track IDs.")

    canonical = recordings[recordings["spotify_track_id"].map(clean) != ""][
        ["recording_id", "spotify_track_id"]
    ].copy()
    canonical["_spotify_id"] = canonical["spotify_track_id"].map(clean)
    canonical["recording_id"] = canonical["recording_id"].map(clean)

    dup = canonical[canonical.duplicated("_spotify_id", keep=False)]
    if len(dup):
        raise SystemExit("FAILED: duplicate canonical Spotify identities. No files changed.")

    mapped = seed.merge(
        canonical[["_spotify_id", "recording_id"]],
        on="_spotify_id", how="left", validate="many_to_one"
    )
    mapped["recording_id"] = mapped["recording_id"].fillna("").map(clean)
    missing = mapped[mapped["recording_id"] == ""]
    if len(missing):
        cols = [c for c in ["spotify_track_id", "title", "artist_names"] if c in missing.columns]
        print(missing[cols].to_string(index=False))
        raise SystemExit(f"FAILED: {len(missing)} songs lack canonical Recordings. No files changed.")

    if mapped["recording_id"].duplicated().any():
        raise SystemExit("FAILED: bulk import contains duplicate canonical Recordings. No files changed.")

    contributors = read_csv(contributors_path, required=False)
    memberships = read_csv(memberships_path, required=False)
    if contributors.empty and len(contributors.columns) == 0:
        contributors = pd.DataFrame(columns=CONTRIBUTOR_COLUMNS)
    if memberships.empty and len(memberships.columns) == 0:
        memberships = pd.DataFrame(columns=MEMBERSHIP_COLUMNS)

    if list(contributors.columns) != CONTRIBUTOR_COLUMNS:
        missing_cols = set(CONTRIBUTOR_COLUMNS) - set(contributors.columns)
        if missing_cols:
            raise SystemExit(f"FAILED: contributors.csv missing columns: {sorted(missing_cols)}")
        contributors = contributors[CONTRIBUTOR_COLUMNS]
    if list(memberships.columns) != MEMBERSHIP_COLUMNS:
        missing_cols = set(MEMBERSHIP_COLUMNS) - set(memberships.columns)
        if missing_cols:
            raise SystemExit(f"FAILED: library_memberships.csv missing columns: {sorted(missing_cols)}")
        memberships = memberships[MEMBERSHIP_COLUMNS]

    if contributors["contributor_id"].duplicated().any():
        raise SystemExit("FAILED: duplicate contributor IDs already exist.")
    if memberships["library_membership_id"].duplicated().any():
        raise SystemExit("FAILED: duplicate Library membership IDs already exist.")
    if memberships.duplicated(["contributor_id", "recording_id"]).any():
        raise SystemExit("FAILED: duplicate Contributor/Recording memberships already exist.")

    display_name = clean(args.display_name)
    requested_id = clean(args.contributor_id)
    is_new = False

    if requested_id:
        hit = contributors[contributors["contributor_id"].map(clean) == requested_id]
        if len(hit) == 1:
            contributor_id = requested_id
            if clean(hit.iloc[0]["display_name"]) != display_name:
                raise SystemExit(
                    "FAILED: contributor ID exists with a different display name. "
                    "Rename explicitly outside bulk import."
                )
        elif len(hit) == 0:
            contributor_id = requested_id
            is_new = True
        else:
            raise SystemExit("FAILED: contributor ID is ambiguous.")
    else:
        same_name = contributors[contributors["display_name"].map(clean) == display_name]
        if len(same_name):
            ids = ", ".join(sorted(set(same_name["contributor_id"].map(clean))))
            raise SystemExit(
                f"FAILED: {display_name!r} already exists. Rerun with --contributor-id {ids}"
            )
        contributor_id = new_contributor_id()
        is_new = True

    playlist_ids = []
    if "playlist_id" in mapped.columns:
        playlist_ids = sorted({clean(v) for v in mapped["playlist_id"] if clean(v)})
    if len(playlist_ids) > 1:
        raise SystemExit("FAILED: bulk import contains multiple source playlist IDs.")
    source_id = playlist_ids[0] if playlist_ids else ""

    proposed = pd.DataFrame([{
        "library_membership_id": membership_id(contributor_id, rid),
        "contributor_id": contributor_id,
        "recording_id": rid,
        "added_via": "bulk_import",
        "import_source_type": "spotify_playlist",
        "import_source_id": source_id,
        "added_at": "",
    } for rid in mapped["recording_id"]], columns=MEMBERSHIP_COLUMNS)

    existing_pairs = set(zip(
        memberships["contributor_id"].map(clean),
        memberships["recording_id"].map(clean)
    ))
    new_memberships = proposed[
        ~proposed.apply(
            lambda r: (clean(r["contributor_id"]), clean(r["recording_id"])) in existing_pairs,
            axis=1
        )
    ].copy()

    # Existing memberships for this contributor must use deterministic IDs.
    for _, r in memberships[memberships["contributor_id"].map(clean) == contributor_id].iterrows():
        expected = membership_id(clean(r["contributor_id"]), clean(r["recording_id"]))
        if clean(r["library_membership_id"]) != expected:
            raise SystemExit(
                f"FAILED: existing membership ID {clean(r['library_membership_id'])} "
                f"should be {expected}. No files changed."
            )

    add_contributor = pd.DataFrame(
        [{"contributor_id": contributor_id, "display_name": display_name}],
        columns=CONTRIBUTOR_COLUMNS
    ) if is_new else pd.DataFrame(columns=CONTRIBUTOR_COLUMNS)

    final_contributors = pd.concat([contributors, add_contributor], ignore_index=True)
    final_memberships = pd.concat([memberships, new_memberships], ignore_index=True)

    errors = {
        "duplicate contributor IDs": final_contributors["contributor_id"].duplicated().sum(),
        "duplicate membership IDs": final_memberships["library_membership_id"].duplicated().sum(),
        "duplicate contributor/recording pairs":
            final_memberships.duplicated(["contributor_id", "recording_id"]).sum(),
        "missing Recording targets":
            (~final_memberships["recording_id"].isin(set(recordings["recording_id"].map(clean)))).sum(),
        "missing Contributor targets":
            (~final_memberships["contributor_id"].isin(set(final_contributors["contributor_id"].map(clean)))).sum(),
    }

    print("\nContributor ID:", contributor_id)
    print("Bulk-import rows:", len(mapped))
    print("Canonical Recording targets:", mapped["recording_id"].nunique())
    print("New contributors:", int(is_new))
    print("New Library memberships:", len(new_memberships))
    print("Already in contributor Library:", len(proposed) - len(new_memberships))
    print("Entry method: bulk_import")
    print("Technical source type: spotify_playlist")
    print("Technical source ID:", source_id or "(blank)")
    for label, count in errors.items():
        print(f"{label.capitalize()}:", int(count))

    if any(errors.values()):
        raise SystemExit("\nFAILED: final-state integrity checks failed. No files changed.")

    print("\nIntegrity checks: PASSED")

    if args.dry_run:
        print("\nDRY RUN PASSED")
        print("No files were written.")
        if is_new:
            print(
                "\nNOTE: this opaque contributor ID exists only for this dry run. "
                "The write run will generate the permanent ID unless you pass "
                "--contributor-id explicitly."
            )
        return

    if not is_new and len(new_memberships) == 0:
        print("\nNO CHANGES")
        print("Contributor and all imported Library memberships already exist.")
        print("No files were written; no backups were created.")
        return

    library_dir.mkdir(parents=True, exist_ok=True)
    backups = []
    if is_new:
        b = backup(contributors_path)
        if b:
            backups.append(b)
        final_contributors.to_csv(contributors_path, index=False)
    if len(new_memberships):
        b = backup(memberships_path)
        if b:
            backups.append(b)
        final_memberships.to_csv(memberships_path, index=False)

    print("\nWRITE PASSED")
    print("Contributors added:", int(is_new))
    print("Library memberships added:", len(new_memberships))
    print("Canonical music tables modified: 0")
    print("Contributors file:", contributors_path)
    print("Memberships file:", memberships_path)
    if backups:
        print("Backups:")
        for b in backups:
            print(" -", b)
    if is_new:
        print("\nPERMANENT CONTRIBUTOR ID:")
        print(contributor_id)
        print("Use this ID with --contributor-id for this contributor's future imports.")

if __name__ == "__main__":
    main()
