# Publication contract

Only source code, generic operational documentation, public configuration, and validated public observations are published. Local credentials, identity fields, private configuration, environment files, raw response bodies, session transcripts, and machine paths are excluded.

The allowlist and content scanner run before publication. They inspect source files, generated snapshots, and public batch-plan artifacts; publication additionally scans Git history. Logs report fixed error categories and counts. Source documents stay at their original public URLs; only necessary structured observations and brief extracts enter the snapshot.

The workflow fails when no valid observations are available, when identity/date/schema validation fails, or when a publication check fails. It never fills unavailable fields with synthetic observations. Deployment occurs in the same workflow that generates the snapshot, so a bot commit is not relied upon to trigger another workflow.

When observations were already persisted but site preparation or deployment failed,
run `Validate records` manually on `main` with `publish_site=true`. It checks the
saved snapshot, site package, and complete publication history before deploying.
This operation shares the publication writer lock and makes no collection or
market-data requests. Ordinary validation runs never deploy.
