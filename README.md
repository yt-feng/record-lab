# Record Lab

A source-linked workspace for periodic public records. It compares disclosed positions, shows market-price intervals, and links a manager's report commentary to its reporting period.

The browser reads generated observations for **2026Q2 (30 June), compared with 31 March**. GitHub Actions handles directory discovery, collection, document parsing, price-history retrieval, comparison, validation, and publication. The company directory includes every institution returned by the public directory sources; each company and product has an explicit collection state. A directory entry is not a completed observation.

## Use

Open the published page, filter by institution, manager, instrument, or period, and export the visible records to CSV. Every comparison shows the observation date and source. Refresh reloads the generated snapshot; it does not run collection in the browser.

Run **Traverse records** in Actions to discover the directory and process bounded batches. It runs twice weekly and can queue continuation runs until the current traversal finishes or reaches its retry limit. Company partitions keep the browser download bounded. **Update records** refreshes the initial validation selection in `config/universe.json`. No API key is required. A failed or incomplete upstream response remains explicit; an older valid observation retains its original date.

The date boundary is 29 September 2026. Current manager profiles are labeled as such; report-specific manager attribution requires report or tenure evidence. Quarterly disclosure entrants and exits do not prove purchases or sales. Stock high/low intervals are a reference range, not the portfolio's actual acquisition cost.

## Development

```sh
npm test
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/publication_guard.py --root .
npm run serve
```

Batch collection runs on the hosted runner. Tests use small synthetic fixtures, never fabricated production observations.

See [Methodology](docs/methodology.md) and [Publication](docs/publication.md).
