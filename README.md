# Record Lab

A source-linked workspace for periodic public records. It compares disclosed positions, shows market-price intervals, and links a manager's report commentary to its reporting period.

The browser reads a generated static snapshot. GitHub Actions handles collection, document parsing, price-history retrieval, comparison, validation, and publication. The initial coverage is a small multi-provider sample; the dashboard reports actual coverage and missing fields.

## Use

Open the published page, filter by institution, manager, instrument, or period, and export the visible records to CSV. Every comparison shows the observation date and source. Refresh reloads the generated snapshot; it does not run collection in the browser.

Run **Update records** in Actions to collect and publish. It also runs twice weekly. Add public product codes to `config/universe.json` to extend coverage. No API key is required. A failed or incomplete upstream response remains explicit; an older valid observation retains its original date.

## Development

```sh
npm test
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/publication_guard.py --root .
npm run serve
```

Batch collection runs on the hosted runner. Tests use small synthetic fixtures, never fabricated production observations.

See [Methodology](docs/methodology.md) and [Publication](docs/publication.md).
