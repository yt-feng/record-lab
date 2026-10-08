# Publication history verification

The publication guard always scans current files, every reachable commit message
and author/committer identity, and every historical file path and mode. Within a
run it scans each immutable blob once through one bounded Git object reader.

For unchanged scanners, a successful main workflow can publish a compact list of
verified blob object IDs. A later run reuses only blob-content verdicts, after
checking the canonical GitHub API for the same repository, an allowed main
workflow event, a completed successful run, the immutable artifact SHA-256 digest
and byte count, and the run identity. PR artifacts and failed runs cannot supply
verdicts. Neither a local JSON file nor a caller-provided artifact URL is trusted.

The proof binds both the API workflow run head and the actual Git checkout.
Both must contain the exact current guard, cache helper, workflow definitions and
requirements; the fingerprint also includes the Python implementation and full
version. The run head must be an ancestor of that checkout, and that checkout an
ancestor of the current head. This also covers publication jobs that check out a
newer main commit than the workflow run head. Changed scanner inputs require a
complete content scan.

Artifact discovery and download share a 20-second budget and bounded byte
readers. Missing, expired, malformed, mismatched or unavailable proofs fall back
to a complete scan. The guard emits a new proof only after all publication checks
pass; consumers accept it only after the entire producing workflow succeeds.
Artifacts expire after 14 days. A cold scan remains the recovery path. The
validation job allows 12 minutes for that path: the measured 5.3 GB cold run took
459 seconds including setup, before the optional lookup and proof upload. Warm
runs still scan current files and all metadata, while historical content work
is proportional to new blobs.

Logs show total historical blobs, cached blobs, freshly scanned blobs, bytes and
elapsed time. The read-only Validate records workflow supports manual dispatch
so reuse can be verified without collecting observations or publishing data.
