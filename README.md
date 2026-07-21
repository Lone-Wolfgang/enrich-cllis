# clli_scrape

Scrape rate center and state for CLLI codes from telcodata.us, then enrich the
results with geography, demographics, and hydrological/geological covariates.
The end product is a small relational schema keyed on `(state, rate_center)`,
suitable for a place-level analysis such as modelling fiber-install
cancellation rates against flood proneness, soil, and precipitation.

This guide assumes you have **already scraped** and are starting from the
scraper's output CSV. If you still need to scrape, see [Scraping](#scraping-if-you-dont-have-data-yet)
at the end.

---

## What you start with

The scraper produces one row per CLLI. The columns that matter downstream are:

| column | meaning |
|---|---|
| `clli` | the resolved CLLI code |
| `state` | two-letter state, taken from the CLLI |
| `rate_center` | the resolved rate center (the telephone billing locality) |
| `rate_center_fd` | JSON vote distribution across candidate rate centers |
| `resolution_method` | how the rate center was chosen (`state_filtered`, `majority`, ...) |
| `confidence` | resolver confidence, 0–1 |
| `n_prefixes` | how many NPA-NXX prefixes backed the resolution |

Many CLLIs collapse onto the same `(state, rate_center)`. That key — not the
CLLI — is the grain of the final analysis.

---

## What a full enrichment produces

Enrichment runs in layers on top of the scraped CSV, each adding columns:

1. **Gazetteer** — geocodes each rate center against the Census Gazetteer,
   attaching `lat`/`lon`, `place_name`, `geoid`, and land area.
2. **ACS** — attaches American Community Survey demographics (population,
   income, broadband, poverty, ...) on the GEOID the gazetteer produced.
3. **Geo covariates** — at each geocoded point, attaches FEMA flood zone, USDA
   soil (water-table depth, drainage class, and optionally flooding/ponding
   from gNATSGO), PRISM precipitation normals, and optionally dated Daymet
   precipitation.
4. **Relational split** — reshapes the wide enriched frame into separate tables
   all joinable on `(state, rate_center)`.

Everything can run in a **single command**, or layer by layer. Both are shown
below.

---

## Prerequisites

- Python 3.10+
- Install dependencies:
  ```bash
  pip install pandas requests beautifulsoup4 lxml rapidfuzz tqdm
  ```
- A **free Census API key** for the ACS layer:
  <https://api.census.gov/data/key_signup.html>. Set it once:
  ```bash
  export CENSUS_API_KEY=your_key_here
  ```
- **Only if** you use the offline gNATSGO soil backend, also install:
  ```bash
  pip install rasterio pyproj
  ```
  and download the gNATSGO GeoPackage + `muraster` GeoTIFF(s) from USDA (see
  [Soil backend](#choosing-a-soil-backend)).

The gazetteer, ACS, and geo layers all cache to disk, so re-runs and resumes
need no repeated network calls.

---

## The one-command full enrichment

Starting from `results.csv` (your scraped file), this runs every layer and
writes the relational schema:

```bash
export CENSUS_API_KEY=your_key_here

python -m clli_scrape \
    -o results.csv \
    --enrich \
    --acs telecom \
    --geo \
    --relational
```

What each flag does:

- `-o results.csv` — the scraped CSV. With `--enrich` and no `-i/--input`, the
  file is enriched **in place as the source** (a separate enriched file is
  written; your scraped CSV is not overwritten).
- `--enrich` — run the gazetteer geocoding layer. Required by every layer below.
- `--acs telecom` — attach the `telecom` ACS preset (see [ACS variables](#acs-variables)).
- `--geo` — attach flood zone, soil, and PRISM precipitation normals.
- `--relational` — emit the keyed tables.

### Outputs

```
results_enriched.csv                     # the wide, one-row-per-CLLI frame
results_enriched_relational/
    rate_center.csv                      # PK (state, rate_center): dimension
    clli_resolution.csv                  # one row per CLLI, FK to rate_center
    enrich_gazetteer.csv                 # one row per center: geo + ACS
    enrich_soil.csv                      # one row per center: flood + soil
    enrich_weather.csv                   # one row per center: precip normals
```

`results_enriched.csv` is the flat frame if you want everything in one sheet.
The `_relational/` directory is the keyed schema described in
[The relational schema](#the-relational-schema).

---

## How the layers compose

The layers run **within a single invocation**, on the enriched frame held in
memory: `--enrich` geocodes, then `--acs`, `--geo`, and `--relational` each add
to that same frame before it's written. You choose how much to run by which
flags you pass — you do not re-feed the output back in.

So enrichment starts from the **scraped** CSV every time, and you add or remove
trailing flags to control the depth:

```bash
# gazetteer only
python -m clli_scrape -o results.csv --enrich

# gazetteer + ACS
python -m clli_scrape -o results.csv --enrich --acs telecom

# gazetteer + ACS + geo
python -m clli_scrape -o results.csv --enrich --acs telecom --geo

# everything, including the relational split
python -m clli_scrape -o results.csv --enrich --acs telecom --geo --relational
```

Each of these reads `results.csv` and writes `results_enriched.csv` fresh; the
later commands are supersets of the earlier ones. Because the gazetteer, ACS,
and geo layers all cache to disk, re-running a deeper command after a shallower
one does **not** re-fetch what the shallow run already retrieved — it reuses the
caches and only does the additional work. That is how you "add a layer": re-run
with the extra flag; the cached layers are effectively free.

> **Do not** point `-o` at an already-enriched file to "stack" a layer. Each
> run geocodes from scratch and would duplicate the geo columns. Always start
> from the scraped CSV and select depth with flags.
>
> Every layer beyond the gazetteer requires `--enrich`, because it joins on the
> geography the gazetteer produces. Running `--acs`, `--geo`, or `--relational`
> without `--enrich` exits with an error.

If a layer fails midway (say the ACS API is down), fix the issue and re-run the
same full command: the layers that already completed are served from cache, so
only the failed work repeats.

---

## The relational schema

`--relational` reshapes the wide frame into tables that behave like a small
relational database. `(state, rate_center)` is the primary key.

```
rate_center        PK (state, rate_center)
                   One row per center: a representative lat/lon plus roll-up
                   counts (n_clli, rep_confidence, rep_resolution_method).

clli_resolution    PK clli,  FK (state, rate_center) -> rate_center
                   One row per scraped CLLI: its resolution method, vote
                   distribution, confidence, and prefix count. The full,
                   messy audit trail, kept separate from the clean layers.

enrich_gazetteer   PK (state, rate_center)
                   Geocoding + land area + ACS demographics.

enrich_soil        PK (state, rate_center)
                   FEMA flood zone + USDA/gNATSGO soil covariates.

enrich_weather     PK (state, rate_center)   [see note on dated precip]
                   PRISM annual precipitation normals.
```

Because each enrichment table has a **unique** key, joining any of them to the
per-CLLI `clli_resolution` table is strictly many-to-one: a demographic or soil
value attaches once per center and can never fan out across the several CLLIs
that share it.

### Representative row

When several CLLIs resolve to one center, the dimension row is chosen by
highest scraper `confidence`, breaking ties by `n_prefixes` (the
better-sampled CLLI wins). The choice is recorded — `rate_center.n_clli`,
`rep_confidence`, and `rep_resolution_method` — so the collapse is auditable.

### Joining the tables

```python
import pandas as pd

rc = pd.read_csv("results_enriched_relational/rate_center.csv")
gz = pd.read_csv("results_enriched_relational/enrich_gazetteer.csv")
soil = pd.read_csv("results_enriched_relational/enrich_soil.csv")
wx = pd.read_csv("results_enriched_relational/enrich_weather.csv")

KEY = ["state", "rate_center"]

# one analysis-ready row per center
model_df = (rc.merge(gz, on=KEY, how="left")
              .merge(soil, on=KEY, how="left")
              .merge(wx, on=KEY, how="left"))
```

Attach your cancellation-rate table on the same key and you have a place-level
modelling frame.

### Custom output location

```bash
python -m clli_scrape -o results_enriched.csv --enrich --relational \
    --relational-dir tables/ \
    --relational-prefix clli_
# -> tables/clli_rate_center.csv, tables/clli_enrich_soil.csv, ...
```

---

## ACS variables

`--acs` takes a preset name or a list of variable names. Bare `--acs` uses
`core`.

| preset | variables |
|---|---|
| `core` | population, median household income, median age, bachelor's+, poverty, broadband |
| `telecom` | core connectivity set: broadband, no-internet, has-computer, plus income/poverty/education |
| `housing` | home value, gross rent, owner-occupied, income |
| `demographics` | population, age, race/ethnicity breakdown |
| `all` | every available variable |

List everything, including individual variable names and their Census codes:
```bash
python -m clli_scrape --list-acs-vars
```

Each ACS estimate ships with its **margin of error** and a derived
`<var>_moe_ratio` (MOE ÷ estimate). Small places carry large MOEs; filter on
`<var>_moe_ratio` (Census treats a ratio above ~0.30 as unreliable) before
trusting a value. A `geo_level` column records whether each row's ACS data came
from the place, county, or state tier — county and state are fallbacks used
where place-level data is missing.

Useful ACS options:
- `--acs-year 2023` — ACS 5-year vintage (default 2023).
- `--county-geoids` — assign each center a county GEOID so the county fallback
  can fill places with no ACS data. Recommended for full coverage.
- `--no-county-fallback` — disable that substitution.

---

## Choosing a soil backend

`--geo` fetches soil at each point. Two backends:

**`sda` (default)** — queries USDA Soil Data Access over HTTP, one call per
unique coordinate. No local files, but slower and rate-limited; fine for
modest datasets.
```bash
python -m clli_scrape -o results_enriched.csv --enrich --geo
```

**`gnatsgo`** — samples a **local** gNATSGO GeoPackage + `muraster` GeoTIFF
instead. Offline, far faster on large inputs, and exposes extra fields the SDA
path doesn't: soil-survey flooding frequency (`flood_freq_dcd`), ponding
(`ponding_freq_pct_class`), and a spring water-table depth. These name how
often the ground itself floods, independent of any storm — a static complement
to precipitation.
```bash
python -m clli_scrape -o results_enriched.csv --enrich --geo \
    --soil-source gnatsgo \
    --gnatsgo-gpkg gNATSGO_CONUS.gpkg \
    --gnatsgo-raster muraster_conus.tif \
    --gnatsgo-raster muraster_ak.tif      # repeat for AK/HI/island tiles
```
Requires `rasterio` and `pyproj`. If either is missing, or the files aren't
found, the run logs a warning and falls back to the SDA backend rather than
aborting. Download gNATSGO from the USDA:
<https://www.nrcs.usda.gov/resources/data-and-reports/gridded-national-soil-survey-geographic-database-gnatsgo>

---

## Dated precipitation (optional)

The static PRISM normal answers "is this a wet region". To test whether it was
actually raining near a job — the covariate that separates weather from
geology — supply a dates table and the geo layer adds per-job Daymet
precipitation.

Create a CSV with these exact columns:
```csv
state,rate_center,job_date
LA,NEWORLEANS,2024-06-20
GA,ATLANTA,2024-07-15
```

Then:
```bash
python -m clli_scrape -o results_enriched.csv --enrich --geo \
    --geo-dates jobdates.csv \
    --geo-lag 3
```
`--geo-lag 3` also sums precipitation over the job day plus the 3 prior days
(`precip_3d_sum_mm`), capturing ground still saturated from recent rain.

**Note on grain:** with dated precip, `enrich_weather` is emitted at the
`(center, job_date)` grain — one row per job date — since each date is a
distinct observation. It still joins to `rate_center` on the key; it simply has
multiple rows per center by design. Centers with no job date get a single
normals row.

---

## Caching, resuming, and reruns

Every network layer caches to disk:

| layer | cache | default location |
|---|---|---|
| gazetteer | downloaded Census file | `gazetteer_places_<year>.csv` |
| ACS | per state/geo/year response | `acs_cache/` |
| geo | per-source, per-coordinate | `geo_cache/{flood,soil,prism,daymet}.json` |

Re-running a layer reuses the cache and only fetches what's missing, so an
interrupted run resumes by simply issuing the same command. To force a fresh
fetch, delete the relevant cache file or directory. The geo caches record
failures too (so a resume won't re-hit a structurally bad point); if a whole
batch failed on a transient outage, delete `geo_cache/<source>.json` to retry.

Override cache locations with `--gazetteer-cache`, `--acs-cache`, `--geo-cache`.

---

## Match quality — read the summary

The gazetteer layer prints a match-quality report. Rate centers are truncated,
LERG-style names, so matching is imperfect:

- `exact` / `expanded` / `expanded_nospace` — high-confidence matches.
- `fuzzy` — matched above the `--min-score` threshold (default 85). The report
  lists the lowest-scoring fuzzy matches; **verify these**, since a wrong fuzzy
  match produces a confident wrong location.
- `unmatched` — nothing cleared the threshold; lat/lon left null.

Raise `--min-score` for stricter matching (more unmatched, fewer wrong), lower
it for more coverage (more matches, more risk). A matched place is an
approximation of the rate center's location, not its boundary — treat
point-derived values (flood zone, soil) as proxies at the center's
representative point, not areal averages.

---

## Scraping (if you don't have data yet)

Starting from a newline-separated list of CLLI codes:

```bash
python -m clli_scrape -i clli_list.txt -o results.csv
```

Scraping is polite and resumable: any CLLI already in `results.csv` is skipped,
so interrupt with Ctrl-C and re-run the same command to continue. Tunables:
`--delay` (base seconds between requests), `--jitter`, `--cooldown` (rate-limit
backoff), `--restart` (overwrite instead of resume). Then proceed with the
enrichment above.

---

## Quick reference

```bash
# full pipeline, one command
python -m clli_scrape -o results.csv --enrich --acs telecom --geo --relational

# full pipeline, offline soil + dated precip
python -m clli_scrape -o results.csv --enrich --acs telecom \
    --geo --soil-source gnatsgo \
    --gnatsgo-gpkg gNATSGO_CONUS.gpkg --gnatsgo-raster muraster_conus.tif \
    --geo-dates jobdates.csv --geo-lag 3 \
    --relational

# list ACS presets and variables
python -m clli_scrape --list-acs-vars
```