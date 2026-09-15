# 0002. Event timeranges are stored as PostgreSQL ranges

Status: accepted, 2026-09-15.

## Context

The `events` table held the timerange only inside the JSONB document, indexed by track alone. Every
listing, overlap check and coverage calculation loaded all events of a track into Python. A batch
write with overlaps disabled re-read the whole track for each event in the batch.

TADS timestamps carry 48-bit seconds and nanosecond fractions: up to 24 decimal digits of
nanoseconds. That exceeds a 64-bit integer, so an `int8range` cannot hold the validated range, and
the existing tests already use values past that limit.

## Decision

`events` gains a `numrange` column over nanoseconds. `NUMERIC` holds the full range exactly, and
`numrange` applies the same rule as the BBC library: a zero-length half-open range is empty. Four
sort-key columns hold the start and end with their inclusivity flags, using negative and positive
infinity for unbounded ends, so keyset comparisons never meet a NULL.

A GiST index on `(track_id, timerange)`, enabled by the `btree_gist` extension, serves overlap and
containment queries. Two btree indexes serve keyset paging forwards and backwards. Empty event
ranges are rejected on write because they cannot be positioned.

Migrations are an ordered list in the package, applied in order under an advisory lock by
`tadoss migrate`. Readiness fails until the database matches the build.

## Consequences

Paging cursors change format; TADS defines them as opaque. External databases must allow
`CREATE EXTENSION btree_gist`, a trusted extension the database owner can create.

Edit-rate grid checks remain in Python: SQL `mod()` does not reproduce the library's normalisation
for drop-frame rates.
