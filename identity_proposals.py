import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


VALID_STATUSES = {
    "pending",
    "accepted",
    "rejected",
}


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def proposal_id(
    canonical_recording_id,
    candidate_system,
    candidate_identity,
):
    raw = "|".join([
        clean(canonical_recording_id),
        clean(candidate_system),
        clean(candidate_identity),
    ])

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]

    return f"PROP_{digest}"


def load_proposals(path):
    path = Path(path)

    if not path.exists():
        return {}

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {}


def save_proposals(
    path,
    proposals,
):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            proposals,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def upsert_proposal(
    proposals,
    *,
    canonical_recording_id,
    source_system,
    source_identity,
    candidate_system,
    candidate_identity,
    confidence_score="",
    evidence=None,
    status="pending",
    source_title="",
    source_artist_names="",
    candidate_title="",
    candidate_artist_names="",
    candidate_url="",
):
    if status not in VALID_STATUSES:
        raise ValueError(
            "Invalid proposal status: "
            + status
        )

    pid = proposal_id(
        canonical_recording_id,
        candidate_system,
        candidate_identity,
    )

    existing = proposals.get(
        pid,
        {}
    )

    discovered_at = (
        existing.get("discovered_at")
        or now_iso()
    )

    proposals[pid] = {
        "proposal_id":
            pid,

        "canonical_recording_id":
            clean(canonical_recording_id),

        "source_system":
            clean(source_system),

        "source_identity":
            clean(source_identity),

        "candidate_system":
            clean(candidate_system),

        "candidate_identity":
            clean(candidate_identity),

        "confidence_score":
            clean(confidence_score),

        "evidence":
            (
                evidence
                if evidence is not None
                else {}
            ),

        "status":
            status,

        "source_title":
            clean(source_title),

        "source_artist_names":
            clean(source_artist_names),

        "candidate_title":
            clean(candidate_title),

        "candidate_artist_names":
            clean(candidate_artist_names),

        "candidate_url":
            clean(candidate_url),

        "discovered_at":
            discovered_at,

        "updated_at":
            now_iso(),

        "reviewed_at":
            clean(
                existing.get("reviewed_at")
            ),

        "review_source":
            clean(
                existing.get("review_source")
            ),
    }

    return pid


def proposals_for_candidate(
    proposals,
    *,
    candidate_system,
    candidate_identity,
    statuses=None,
):
    candidate_system = clean(
        candidate_system
    )

    candidate_identity = clean(
        candidate_identity
    )

    if statuses is None:
        statuses = VALID_STATUSES

    return [
        proposal
        for proposal in proposals.values()
        if (
            clean(
                proposal.get(
                    "candidate_system"
                )
            )
            == candidate_system
            and clean(
                proposal.get(
                    "candidate_identity"
                )
            )
            == candidate_identity
            and clean(
                proposal.get("status")
            )
            in statuses
        )
    ]


def pending_proposals_for_spotify_track(
    proposals,
    spotify_track_id,
):
    return proposals_for_candidate(
        proposals,
        candidate_system="spotify",
        candidate_identity=spotify_track_id,
        statuses={"pending"},
    )


def set_proposal_decision(
    proposals,
    proposal_id_value,
    decision,
    review_source="contributor",
):
    if decision not in {
        "accepted",
        "rejected",
    }:
        raise ValueError(
            "Decision must be accepted "
            "or rejected."
        )

    proposal = proposals.get(
        proposal_id_value
    )

    if not isinstance(
        proposal,
        dict,
    ):
        raise KeyError(
            proposal_id_value
        )

    proposal["status"] = decision
    proposal["reviewed_at"] = now_iso()
    proposal["review_source"] = clean(
        review_source
    )
    proposal["updated_at"] = now_iso()

    return proposal
