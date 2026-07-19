# clli-scraper

Scrapes rate center and state data for CLLI codes from
[telcodata.us](https://www.telcodata.us/search-area-code-exchange-by-clli).

Plain HTTP — no Selenium, no browser dependency.

## Install

```bash
conda create -n clli python=3.11 -y
conda activate clli
python -m pip install -e .
```

Or without installing the package:

```bash
python -m pip install -r requirements.txt
python -m clli_scraper -i clli_list.txt -o results.csv
```

## Usage

```bash
clli-scrape -i clli_list.txt -o results.csv
```

Input is one CLLI per line; blank lines and `#` comments are ignored, and
duplicates are collapsed. Partial CLLIs are allowed but must be at least six
characters (`DLLSTX` matches every Dallas, TX switch).

```
# clli_list.txt
DLLSTX
NYCMNY
CHCGIL
```

### Options

| Flag | Default | Purpose |
|---|---|---|
| `-i, --input` | required | Newline-separated CLLI list |
| `-o, --output` | required | Output CSV; appended to, and used for resume |
| `--raw` | none | Also write unaggregated per-prefix rows |
| `--delay` | 2.0 | Base seconds between requests |
| `--jitter` | 1.0 | Random 0–N seconds added per request |
| `--cooldown` | 60.0 | Initial backoff on rate limit, doubles each retry |
| `--max-cooldowns` | 5 | Give up on a CLLI after N backoffs |
| `--restart` | off | Overwrite output instead of resuming |
| `--dry-run` | off | List what would be scraped, then exit |
| `--reprocess` | off | Rebuild output from `--raw`, no network calls |
| `--no-progress` | off | Disable the progress bar |
| `-v, --verbose` | off | Debug logging |

### Resume

Resume is automatic. Every CLLI is appended to the output as soon as it
completes, and on startup the scraper reads back the `clli_query` column and
skips anything already present. Interrupt with Ctrl-C and re-run the identical
command:

```bash
clli-scrape -i clli_list.txt -o results.csv    # Ctrl-C partway
clli-scrape -i clli_list.txt -o results.csv    # picks up where it stopped
```

Queries that return no results are recorded with
`resolution_method = no_results` so they aren't retried forever. Use
`--restart` to force a clean run.

### Rate limiting

Three layers:

1. `urllib3.Retry` on 429/500/502/503/504 with exponential backoff, honouring
   `Retry-After`.
2. Application-level cooldown that sleeps and retries the same CLLI, doubling
   each time (60s → 120s → 240s …).
3. An abort after `--max-cooldowns` consecutive failures, so an unattended run
   won't hammer a blocked endpoint. Completed work is already on disk.

Raise `--delay` for large lists. Requests are jittered by default.

## Output

| Column | Description |
|---|---|
| `clli_query` | The search term you supplied |
| `clli` | Full 11-character CLLI |
| `state` | Two-letter state, from `clli[4:6]` |
| `rate_center` | Resolved rate center |
| `rate_center_fd` | JSON frequency distribution of all observed values |
| `resolution_method` | Which rule decided it (see below) |
| `confidence` | Winning share of the voting pool, 0–1 |
| `n_prefixes` | Prefixes in the voting pool |

### How `rate_center` is resolved

A single CLLI can appear across many NPA-NXX prefixes with conflicting rate
centers, and occasionally conflicting states. Resolution order:

1. **`state_filtered`** — characters 5–6 of a CLLI encode the state by
   definition, so rows from other states are dropped, then the remainder is
   majority-voted. This is what keeps `DLLSTX37DSH` in Texas despite stray
   Michigan and Nebraska prefixes in its result set.
2. **`majority`** — plain vote, when the state filter leaves nothing.
3. **`place_prefix`** — ties broken by matching characters 1–4 against the
   rate center name.
4. **`lowest_npa`** — remaining ties broken by the smallest NPA-NXX.

Placeholder rate centers (`XXXXXXXXXX`, `N/A`) are dropped before voting.

### Confidence

`confidence` is the winner's share of its voting pool. High-fanout switches —
tandems and aggregation points — legitimately serve dozens of rate centers and
score low:

```
DLLSTXALDS4  DALLAS  confidence 0.114  n_prefixes 1116
```

The winner there is consistent with the CLLI but not meaningful as a specific
location. Filter on `confidence` and `n_prefixes` to separate clean
single-switch CLLIs from these, and read `rate_center_fd` for the full picture.

## Note on city and ZIP

This page exposes **state** and **rate center** only. There is no city column
and no ZIP code anywhere in the results. Rate center (`DALLAS`, `ARLINGTON`)
is the closest available proxy for a city.

## Enrichment: lat/lon and population density

```bash
# enrich an existing results file
clli-scrape -o results.csv --enrich

# scrape and enrich in one pass
clli-scrape -i clli_list.txt -o results.csv --enrich

# with population for density
clli-scrape -o results.csv --enrich --population acs_place_pop.csv
```

Rate centers are matched against the Census Bureau
[Gazetteer Places file](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html),
downloaded automatically and cached to disk. No API key, no rate limit.

### Added columns

| Column | Description |
|---|---|
| `lat`, `lon` | Internal point of the matched place |
| `place_name` | Matched Gazetteer place |
| `geoid` | Census GEOID, for joining other Census products |
| `land_area_sqkm` | Land area of the matched place |
| `geo_match_method` | Which rule matched (below) |
| `geo_match_score` | Fuzzy score 0–100; 100 for exact |
| `geo_match_candidate` | Best candidate, *including when rejected* |
| `population` | Only with `--population` |
| `pop_density_sqkm` | population / land_area_sqkm |

### Match methods

1. **`exact`** — normalized rate center equals a place name.
2. **`expanded`** — matches after expanding LERG abbreviations.
   `SULPHURSPG` → Sulphur Springs, `GRANDPRARI` → Grand Prairie,
   `WILLSPOINT` → Wills Point, `HONEYGROVE` → Honey Grove.
3. **`expanded_nospace`** — matches ignoring spaces.
   `FLOWERMOUND` → Flower Mound, `RISINGSTAR` → Rising Star.
4. **`fuzzy`** — rapidfuzz over in-state candidates only, above `--min-score`
   (default 85). Restricting by state cuts the candidate pool from ~32,000
   national places to roughly a thousand, which is what makes fuzzy matching
   on truncated names viable.
5. **`unmatched`** / **`non_place`** — left null rather than guessed.

Expansion is deliberately conservative. Splitting run-together names only
happens for unambiguous long words, so `BIRTHRIGHT` is not mangled into
"Birthrig Heights". Airport and placeholder codes (`DLFTWTARPT`,
`XXXXXXXXXX`) are flagged `non_place` or left unmatched instead of being
force-fitted — in testing `DLFTWTARPT` scored 50 against Fort Worth and was
correctly rejected.

Every run prints a match-quality summary, listing the lowest-scoring fuzzy
matches to verify and the unmatched rows with their rejected candidates.

### Population

The Gazetteer supplies geometry but not population. Supply it with
`--population`, pointing at any CSV with a GEOID column and a population
column — ACS 5-year table `B01003` at place level is the usual source. Both
`population` and `land_area_sqkm` are carried through alongside the density
ratio, since density alone can't distinguish a dense small town from a
sprawling city.

### Accuracy caveat

A rate center is a **telephone billing polygon**. It does not align with
municipal boundaries, block groups, or any Census geography. A matched place
approximates *where* a rate center is, not its extent, so density figures are
indicative rather than exact. Block-group apportionment would add precision
against the wrong shape.

This matters most for the high-fanout CLLIs: a tandem with
`confidence = 0.11` across 1,100 prefixes has no meaningful single location,
and its enriched coordinates should be treated accordingly. Cross-reference
`confidence` and `n_prefixes` with `geo_match_method` before trusting any
individual row.

## Reprocessing

Resolution logic can be re-run without re-scraping if you kept `--raw`:

```bash
clli-scrape -i clli_list.txt -o results.csv --raw raw.csv --reprocess
```

## Library use

```python
from clli_scraper import parse_html, postprocess

raw = parse_html(open("dump.html").read(), "DLLSTX")
final = postprocess(raw)
```


## ACS demographics

With a GEOID in hand, the American Community Survey opens up.

```bash
export CENSUS_API_KEY=...        # free: https://api.census.gov/data/key_signup.html

clli-scrape -o results.csv --enrich --acs                 # 'core' preset
clli-scrape -o results.csv --enrich --acs telecom
clli-scrape -o results.csv --enrich --acs pct_poverty median_household_income
clli-scrape --list-acs-vars                               # show everything
```

`--acs` requires `--enrich`, since it joins on the GEOID that produces.
Responses are cached per state/geography/year under `acs_cache/`.

### Presets

| Preset | Contents |
|---|---|
| `core` | population, median income, median age, % bachelor's+, % poverty, % broadband |
| `telecom` | core plus % no internet, % has computer |
| `housing` | median home value, median rent, % owner-occupied, median income |
| `demographics` | median age plus race/ethnicity counts and shares |
| `all` | all 20 variables |

Individual variables can be named directly and mixed with presets.

### Margins of error

Place-level ACS estimates for small towns rest on few survey responses. A
poverty rate for a town of 800 can carry a margin of error of several points;
the same figure for Dallas is tight. So every variable ships with:

- `<var>` — the estimate
- `<var>_moe` — the margin of error
- `<var>_moe_ratio` — MOE / estimate

Census guidance treats a coefficient of variation above ~30% as unreliable.
Filter before analysis:

```python
df = df[df["pct_poverty_moe_ratio"] < 0.30]
```

The run summary counts unreliable rows per variable so the problem is visible
rather than buried.

### Geography tiers

Not every rate center resolves to a place with ACS data. Three tiers are tried
in order, and `geo_level` records which one supplied each row:

1. **`place`** — the matched Gazetteer place. Most precise.
2. **`county`** — nearest county centroid, used when the place has no ACS
   record. More stable estimates, much larger area.
3. **`state`** — for rows that never geocoded at all (airport codes,
   unmatched rate centers). Coarse, but real data rather than a blank.

In testing against the Dallas result set, 111 of 132 CLLIs resolved at place
level and 21 fell through to state. **Always group by `geo_level` before
drawing conclusions** — mixing a city median with a state median in one column
is the easiest way to produce a misleading number here.

### Notes on variable choice

Subject tables (`S`-prefixed) are preferred over detailed `B` tables wherever
both exist, because they publish pre-computed percentages with correctly
propagated MOEs; deriving those by hand invites subtle errors.

Race and ethnicity use `B03002` rather than `B02001`, since it treats Hispanic
origin as a separate dimension instead of double-counting it. Counts are
carried through with `pct_*` shares derived alongside.
