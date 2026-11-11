# RSNA 2026 exhibitor scraper

Exports the public exhibitor information for RSNA 2026's South Hall and North
Hall to a UTF-8 CSV. It uses the same JSON endpoint as the interactive floor
plan, so it does not automate browser clicks.

## Setup and run

```bash
python3 -m pip install -r requirements.txt
python3 scrape_rsna.py
```

The completed export is `rsna_2026_exhibitors.csv`. Its columns are:

- `company_name`
- `company_address` (country is excluded)
- `country`
- `hall`
- `booth_number`
- `booth_size` (for example, `10x10`)
- `booth_area_sq_ft`
- `website_url`

There is one CSV row per named exhibitor/booth assignment. A company with more
than one booth therefore appears more than once. Irregular booths have a blank
`booth_size`, while their published area is retained in `booth_area_sq_ft`.

The script uses separate connect/read timeouts, retries 429 and transient server
errors with exponential backoff, respects `Retry-After`, and caches every
successful response under `.cache/rsna2026`. If a run is interrupted, run the
same command again to resume. Use `--refresh` when you want to ignore the cache
and retrieve the latest floor-plan data.

Useful commands:

```bash
# Three-row network smoke test
python3 scrape_rsna.py --limit 3 --output smoke_test.csv

# Force a current full export
python3 scrape_rsna.py --refresh

# Run unit tests
python3 -m unittest -v
```

If any company-detail requests still fail after retries, the script writes an
explicit `.partial.csv`, records the failures in the `.summary.json`, exits with
status 2, and retains successful responses for the next run.

## About the hall counts

The hall badges currently report 701 South Hall rows and 220 North Hall rows.
Those figures include available/on-hold booth inventory and repeat companies
with multiple booth assignments; they are not counts of unique exhibitors. The
scraper records all named assignments and reports both assignment and unique
company counts in `rsna_2026_exhibitors.summary.json`.
