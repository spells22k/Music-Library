from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import csv
import hashlib


REQUIRED_INDEX_COLUMNS = {
    "evidence_type",
    "source_url",
    "sha256",
    "bytes",
    "relative_path",
}


def normalize_url(value):
    """
    Normalize a WhoSampled URL for evidence lookup.

    Normalization:
      - requires a WhoSampled host
      - canonicalizes host to www.whosampled.com
      - canonicalizes scheme to https
      - removes query strings and fragments
      - collapses repeated path slashes
      - ensures a trailing slash

    The URL path itself is otherwise preserved.
    """
    if not value:
        raise ValueError("URL cannot be blank")

    parts = urlsplit(str(value).strip())

    host = parts.netloc.lower()

    if host in {
        "whosampled.com",
        "www.whosampled.com",
    }:
        host = "www.whosampled.com"
    else:
        raise ValueError(
            f"Not a WhoSampled URL: {value}"
        )

    path = parts.path or "/"

    while "//" in path:
        path = path.replace("//", "/")

    if not path.endswith("/"):
        path += "/"

    return urlunsplit(
        ("https", host, path, "", "")
    )


class EvidenceStore:
    """
    Read-only access to the centralized WhoSampled evidence archive.

    The store knows only about its own root directory and index.csv.
    It does not search Bulk Import directories or legacy archives.

    lookup() deliberately returns every matching capture rather than
    choosing one arbitrarily.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.index_path = self.root / "index.csv"

        if not self.index_path.exists():
            raise FileNotFoundError(
                "Missing centralized evidence index: "
                f"{self.index_path}"
            )

        with self.index_path.open(
            "r",
            newline="",
            encoding="utf-8",
        ) as f:
            self.rows = list(csv.DictReader(f))

        if not self.rows:
            raise ValueError(
                f"Evidence index is empty: {self.index_path}"
            )

        actual_columns = set(self.rows[0].keys())

        if actual_columns != REQUIRED_INDEX_COLUMNS:
            raise ValueError(
                "Unexpected evidence index schema.\n"
                f"Expected: {sorted(REQUIRED_INDEX_COLUMNS)}\n"
                f"Actual:   {sorted(actual_columns)}"
            )

        self.by_url = defaultdict(list)

        for row in self.rows:
            normalized = normalize_url(
                row["source_url"]
            )
            self.by_url[normalized].append(row)

    def lookup(
        self,
        source_url,
        evidence_type=None,
    ):
        """
        Return all evidence records matching source_url.

        If evidence_type is supplied, return only that representation.

        An unknown URL returns an empty list.
        """
        normalized = normalize_url(source_url)

        matches = list(
            self.by_url.get(normalized, [])
        )

        if evidence_type is not None:
            matches = [
                row
                for row in matches
                if row["evidence_type"]
                == evidence_type
            ]

        return matches

    def exists(
        self,
        source_url,
        evidence_type=None,
    ):
        """
        Return True when matching evidence exists.
        """
        return bool(
            self.lookup(
                source_url,
                evidence_type,
            )
        )

    def read(self, record):
        """
        Read one indexed evidence record.

        Before returning bytes, verify:
          - the indexed path stays inside the evidence root
          - the file exists
          - SHA-256 matches the index
          - byte count matches the index
        """
        relative_path = record["relative_path"]
        path = self.root / relative_path

        resolved_root = self.root.resolve()
        resolved_path = path.resolve()

        if (
            resolved_path != resolved_root
            and resolved_root
            not in resolved_path.parents
        ):
            raise ValueError(
                f"Unsafe evidence path: {relative_path}"
            )

        if not path.exists():
            raise FileNotFoundError(path)

        data = path.read_bytes()

        actual_hash = hashlib.sha256(
            data
        ).hexdigest()

        expected_hash = record["sha256"]

        if actual_hash != expected_hash:
            raise ValueError(
                "Evidence integrity failure:\n"
                f"  file:     {path}\n"
                f"  expected: {expected_hash}\n"
                f"  actual:   {actual_hash}"
            )

        expected_bytes = int(record["bytes"])

        if len(data) != expected_bytes:
            raise ValueError(
                "Evidence byte-count failure:\n"
                f"  file:     {path}\n"
                f"  expected: {expected_bytes}\n"
                f"  actual:   {len(data)}"
            )

        return data
