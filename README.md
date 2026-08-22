# Music Library

How to discover music without an algorithm.

## Current status

Phase 1 — Interactive Enrichment Pipeline: validated offline.

## Pipeline

Spotify playlist
      ↓
Spotify metadata
      ↓
WhoSampled resolution
      ↓
Contributor review
      ↓
Archived WhoSampled HTML
      ↓
Recording metadata + credits + media
      ↓
Relationship extraction
      ↓
Spotify enrichment
      ↓
recordings.csv
credits.csv
relationships.csv

## Core principle

Canonical music knowledge is separate from contributor taste. The canonical research database may contain information that an individual contributor rejects from their personal graph.

See ARCHITECTURE.md, PHASE1.md, and DATA_SCHEMA.md.
