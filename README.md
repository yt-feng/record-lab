# Record Lab

A source-linked workspace for periodic public records. It compares disclosed positions, shows market-price intervals, and links a manager's report commentary to its reporting period.

The browser reads separate **2026Q2 (30 June versus 31 March)** and **2026Q3 (30 September versus 30 June)** observations. Choose a published quarter at the top of the page or use `?quarter=2026Q2` / `?quarter=2026Q3`. GitHub Actions handles directory discovery, collection, document parsing, price-history retrieval, comparison, validation, and publication. The company directory includes every institution returned by the public directory sources; each company and product has an explicit collection state. A directory entry is not a completed observation.

## Use

Open the published page to see all companies with collected records in the selected quarter, then enter a company to browse its records. Overview search locates companies by company, manager, or fund-directory name/code; a directory match does not prove that product's report has been collected. Inside a company, search and CSV export apply to the current page. Every comparison shows the observation date and source. Refresh reloads the generated snapshot; it does not run collection in the browser.

The quarter collection workflow processes bounded batches with independent checkpoints under `web/data/periods/<quarter>/`. It retains the existing Q2 snapshot while collecting Q3. Company partitions keep browser downloads bounded; the published site can use gzip company files while keeping the source JSON in Git. No API key is required for text-layer PDF parsing. A failed or incomplete upstream response remains explicit; an older valid observation retains its original date. A missing Q3 publication never falls back to displaying Q2 as Q3.

Each run records its own information cutoff and collection timestamp. Current manager profiles are labeled as such; report-specific manager attribution requires report or tenure evidence. Quarterly disclosure entrants and exits do not prove purchases or sales. Stock high/low intervals are a reference range, not the portfolio's actual acquisition cost.

The cross-fund analysis panel indexes every observed security in the selected quarter. It separates disclosed CNY market values from same-report NAV × weight estimates, quantity changes from disclosure-list entries/exits, and report-level strategy excerpts from security-specific explanations. Monetary totals include only source-verified independent portfolios; unresolved identities remain visible without being added. Stock detail partitions retain all contributing records and their source links. Strategy counts are based on exact-quarter original excerpts and transparent rule themes. The analysis is partial while collection or identity evidence is incomplete.

Actions writes `analysis.json`, bounded `analysis/details-*.json` partitions, and a compact `audit.json` per quarter. The public site compresses detail partitions and can rebuild missing or stale analysis in Actions after a source update. The audit separates first-pass coverage, field gaps, eligible work, scheduled revisits, and issues requiring a parser or source repair. A finished first pass or an exhausted retry budget is not completion.

When an individual company directory refresh fails, its previously verified ownership remains explicitly cached with the original verification date. The fresh master-directory date does not imply that cached company ownership was rechecked; historical product codes remain available and unmapped products can still be collected by code.

Holdings collection and research completeness are separate counts. Research coverage checks the disclosed holdings table, PDF text, quarter-specific strategy, NAV, the exact comparison quarter, and price coverage. Missing fields remain visible and enter a bounded repair queue. Exhausted retries mean an incomplete result, even if the workflow itself succeeds. All product codes are attempted, including products whose company mapping is not yet resolved.

Each collection wave can schedule 16 batches of 75 products per quarter, with at most 16 batch jobs running concurrently and two collection workers per job. New products, report repairs, source retries, and price repairs receive separate capacity so missing-source work cannot indefinitely block PDF and strategy completion. Where every queue has work, ten slots allocate six new products, two report repairs, one source retry, and one price repair; unused slots move to other queues. Per-product attempt limits still apply. This is task capacity, not a promise that every request will succeed or every report has been published.

A product that did not start before a batch's time budget ended is deferred, preserving its prior state and attempt count. It is not a new collection failure. The display separates these deferrals from attempted requests that did not produce valid observations; deferrals can overlap a product's existing pending or failed state. Workflows continue immediately only while work is currently eligible. `nextRetryAt` records the earliest later check, so a finished daily wave can remain incomplete and await a later scheduled run.

The configured Q3 follow-up window ends on **30 November 2026**. This is an operational collection window, not a statutory report-disclosure deadline. Reaching it does not convert missing data into a complete result.

Products with an explicitly sourced inception date after the target quarter are shown separately as not applicable. They do not count as holdings, no-equity observations, or completed research; the research denominator excludes only these proven exemptions.

## Development

```sh
npm test
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/publication_guard.py --root .
npm run serve
```

Batch collection runs on the hosted runner. Tests use small synthetic fixtures, never fabricated production observations.

Report PDFs are parsed in runner memory. If the repository's Actions secrets include `MINER_U_KEYS`, `MINER_U`, or `MINERU_API_KEY`, the optional MinerU v4 path extracts `full.md`; original page references are retained only when verified page markers exist. Without a secret, the bounded pypdf fallback remains available and reports processed pages, original page count, and truncation. Raw PDFs and MinerU markdown are temporary runner inputs and are never published under `web/data`.

See [Methodology](docs/methodology.md) and [Publication](docs/publication.md).
