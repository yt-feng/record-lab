# Record Lab

A source-linked workspace for periodic public records. It compares disclosed positions, shows market-price intervals, and links a manager's report commentary to its reporting period.

The browser reads separate **2026Q2 (30 June versus 31 March)** and **2026Q3 (30 September versus 30 June)** observations. Choose a published quarter at the top of the page or use `?quarter=2026Q2` / `?quarter=2026Q3`. GitHub Actions handles directory discovery, collection, document parsing, price-history retrieval, comparison, validation, and publication. The company directory includes every institution returned by the public directory sources; each company and product has an explicit collection state. A directory entry is not a completed observation.

## Use

Open the published page, filter by institution, manager, instrument, or period, and export the visible records to CSV. Every comparison shows the observation date and source. Refresh reloads the generated snapshot; it does not run collection in the browser.

The quarter collection workflow processes bounded batches with independent checkpoints under `web/data/periods/<quarter>/`. It retains the existing Q2 snapshot while collecting Q3. Company partitions keep browser downloads bounded; the published site can use gzip company files while keeping the source JSON in Git. No API key is required for text-layer PDF parsing. A failed or incomplete upstream response remains explicit; an older valid observation retains its original date. A missing Q3 publication never falls back to displaying Q2 as Q3.

Each run records its own information cutoff and collection timestamp. Current manager profiles are labeled as such; report-specific manager attribution requires report or tenure evidence. Quarterly disclosure entrants and exits do not prove purchases or sales. Stock high/low intervals are a reference range, not the portfolio's actual acquisition cost.

Holdings collection and research completeness are separate counts. Research coverage checks PDF text, quarter-specific strategy, NAV, the exact comparison quarter, and price coverage. Missing fields remain visible and enter a bounded repair queue. Exhausted retries mean an incomplete result, even if the workflow itself succeeds. All product codes are attempted, including products whose company mapping is not yet resolved.

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
