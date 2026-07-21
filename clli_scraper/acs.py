"""
Attach American Community Survey demographics to CLLI results.

Fetches ACS 5-year estimates from the Census API per state, caches them, and
joins on the GEOID produced by `enrich.py`.

Two design decisions worth knowing about:

**Margins of error are carried, not discarded.** Place-level ACS estimates for
small towns rest on very few survey responses. A poverty rate for a town of 800
can carry a margin of error of several points, which makes it nearly
meaningless, while the same figure for a large city is tight. Every variable
ships with its MOE and a derived `*_moe_ratio` (MOE / estimate) so unreliable
values can be filtered rather than silently trusted.

**County acts as a fallback tier.** Where a place has no ACS data -- too small,
unincorporated, or unmatched -- the containing county is substituted. County
estimates are far more stable but describe a much larger area. The `geo_level`
column records which tier each row came from so the tradeoff stays visible.

Requires a free Census API key: https://api.census.gov/data/key_signup.html
Supply it via --census-key or the CENSUS_API_KEY environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

API = "https://api.census.gov/data/{year}/acs/acs5"
API_SUBJECT = "https://api.census.gov/data/{year}/acs/acs5/subject"

DEFAULT_YEAR = 2023

# Curated variable set. Subject tables (S-prefixed) are preferred over detailed
# B-tables where both exist, because they publish pre-computed percentages with
# correctly propagated MOEs -- deriving those by hand invites subtle errors.
#
# Each entry: output_name -> (census_variable, is_subject_table)
VARIABLES: dict[str, tuple[str, bool]] = {
    # Core population and income
    "population":            ("B01003_001E", False),
    "median_household_income": ("B19013_001E", False),
    "median_age":            ("B01002_001E", False),

    # Housing
    "median_home_value":     ("B25077_001E", False),
    "median_gross_rent":     ("B25064_001E", False),
    "pct_owner_occupied":    ("S2501_C02_001E", True),

    # Education (subject table gives the percentage directly)
    "pct_bachelors_plus":    ("S1501_C02_015E", True),
    "pct_hs_or_higher":      ("S1501_C02_014E", True),

    # Economic
    "pct_poverty":           ("S1701_C03_001E", True),
    "pct_unemployed":        ("S2301_C04_001E", True),
    "pct_labor_force":       ("S2301_C02_001E", True),

    # Connectivity -- most thematically relevant for telecom work
    "pct_broadband":         ("S2801_C02_014E", True),
    "pct_no_internet":       ("S2801_C02_019E", True),
    "pct_has_computer":      ("S2801_C02_002E", True),

    # Commute
    "mean_commute_minutes":  ("S0801_C01_046E", True),

    # Race / ethnicity. B03002 is used rather than B02001 because it treats
    # Hispanic origin as a separate dimension instead of double-counting it.
    "race_total":            ("B03002_001E", False),
    "race_white_nh":         ("B03002_003E", False),
    "race_black_nh":         ("B03002_004E", False),
    "race_asian_nh":         ("B03002_006E", False),
    "race_hispanic":         ("B03002_012E", False),
}

# Preset bundles, so callers need not name variables individually.
PRESETS: dict[str, list[str]] = {
    "core": [
        "population", "median_household_income", "median_age",
        "pct_bachelors_plus", "pct_poverty", "pct_broadband",
    ],
    "telecom": [
        "population", "median_household_income", "pct_broadband",
        "pct_no_internet", "pct_has_computer", "pct_poverty",
        "median_age", "pct_bachelors_plus",
    ],
    "housing": [
        "population", "median_home_value", "median_gross_rent",
        "pct_owner_occupied", "median_household_income",
    ],
    "demographics": [
        "population", "median_age", "race_total", "race_white_nh",
        "race_black_nh", "race_asian_nh", "race_hispanic",
    ],
    "all": list(VARIABLES),
}

# Census sentinels for suppressed / unavailable cells. Treated as missing.
NULL_SENTINELS = {-666666666, -999999999, -888888888, -555555555, -333333333}

STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72",
}


def resolve_variables(names: list[str] | None) -> list[str]:
    """Expand preset names and validate variable names."""
    if not names:
        return PRESETS["core"]

    out: list[str] = []
    for n in names:
        n = n.strip()
        if n in PRESETS:
            out.extend(PRESETS[n])
        elif n in VARIABLES:
            out.append(n)
        else:
            raise ValueError(
                f"Unknown variable or preset: {n!r}. "
                f"Presets: {', '.join(PRESETS)}. "
                f"Variables: {', '.join(VARIABLES)}"
            )

    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def _moe_var(est_var: str) -> str:
    """Estimate variable -> its margin-of-error counterpart."""
    return est_var[:-1] + "M" if est_var.endswith("E") else est_var


def _clean(series: pd.Series) -> pd.Series:
    """Numeric coercion with Census sentinel values mapped to NaN."""
    s = pd.to_numeric(series, errors="coerce")
    return s.mask(s.isin(NULL_SENTINELS))


def fetch_acs(
    state: str,
    variables: list[str],
    year: int = DEFAULT_YEAR,
    geo: str = "place",  # "place", "county", or "state"
    api_key: str | None = None,
    cache_dir: Path | None = None,
    timeout: int = 120,
) -> pd.DataFrame:
    """Fetch one state's ACS data for `geo` ('place' or 'county').

    Detailed and subject tables live on different endpoints, so requests are
    grouped by table type and merged. Results are cached per
    state/geo/year/variable-set.
    """
    fips = STATE_FIPS.get(state.upper())
    if not fips:
        log.warning("Unknown state code: %s", state)
        return pd.DataFrame()

    cache_dir = Path(cache_dir or "acs_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{year}_{geo}_{state.upper()}_{abs(hash(tuple(sorted(variables)))) % 10**8}"
    cache = cache_dir / f"acs_{tag}.csv"

    if cache.exists():
        log.debug("Cached ACS: %s", cache)
        return pd.read_csv(cache, dtype={"GEOID": str})

    api_key = api_key or os.environ.get("CENSUS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "A Census API key is required. Get one free at "
            "https://api.census.gov/data/key_signup.html then pass "
            "--census-key or set CENSUS_API_KEY."
        )

    detailed = [v for v in variables if not VARIABLES[v][1]]
    subject = [v for v in variables if VARIABLES[v][1]]

    frames = []
    for group, is_subject in ((detailed, False), (subject, True)):
        if not group:
            continue

        cols = []
        for name in group:
            var = VARIABLES[name][0]
            cols += [var, _moe_var(var)]

        url = (API_SUBJECT if is_subject else API).format(year=year)
        params = {
            "get": "NAME," + ",".join(cols),
            "for": f"state:{fips}" if geo == "state" else f"{geo}:*",
            "key": api_key,
        }
        if geo != "state":
            params["in"] = f"state:{fips}"

        for attempt in range(4):
            try:
                r = requests.get(url, params=params, timeout=timeout)
                if r.status_code == 429:
                    wait = 5 * (attempt + 1)
                    log.warning("ACS rate limited; sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                rows = r.json()
                break
            except (requests.RequestException, json.JSONDecodeError) as exc:
                if attempt == 3:
                    log.error("ACS fetch failed for %s/%s: %s", state, geo, exc)
                    return pd.DataFrame()
                time.sleep(3 * (attempt + 1))
        else:
            return pd.DataFrame()

        df = pd.DataFrame(rows[1:], columns=rows[0])

        # GEOID: state+place (2+5), state+county (2+3), or state (2)
        if geo == "state":
            df["GEOID"] = df["state"].astype(str).str.zfill(2)
        else:
            key = "place" if geo == "place" else "county"
            df["GEOID"] = df["state"].astype(str) + df[key].astype(str)

        keep = {"GEOID": "GEOID", "NAME": "acs_name"}
        for name in group:
            var = VARIABLES[name][0]
            keep[var] = name
            keep[_moe_var(var)] = f"{name}_moe"

        df = df[[c for c in keep if c in df.columns]].rename(columns=keep)
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    out = frames[0]
    for f in frames[1:]:
        f = f.drop(columns=["acs_name"], errors="ignore")
        out = out.merge(f, on="GEOID", how="outer")

    for c in out.columns:
        if c not in ("GEOID", "acs_name"):
            out[c] = _clean(out[c])

    out.to_csv(cache, index=False)
    log.info("Fetched %d %s rows for %s -> %s", len(out), geo, state, cache)
    return out


def add_moe_ratios(df: pd.DataFrame, variables: list[str]) -> pd.DataFrame:
    """Add MOE/estimate ratios so unreliable small-area values can be filtered.

    Census guidance treats a coefficient of variation above roughly 30% as
    unreliable; the ratio here is a close proxy usable as a filter.
    """
    out = df.copy()
    for name in variables:
        est, moe = name, f"{name}_moe"
        if est in out.columns and moe in out.columns:
            e = pd.to_numeric(out[est], errors="coerce")
            m = pd.to_numeric(out[moe], errors="coerce")
            out[f"{name}_moe_ratio"] = (m / e.where(e != 0)).abs().round(3)
    return out


def build_county_acs(
    states,
    variables: list[str] | None = None,
    year: int = DEFAULT_YEAR,
    api_key: str | None = None,
    cache_dir: Path | None = None,
    derive_shares: bool = True,
) -> pd.DataFrame:
    """True county-level ACS for every county in the given states.

    Unlike the per-place enrichment, this pulls the actual published county
    figure straight from the Census county tier -- one wildcard call per state
    fetches every county in it -- so the result is the real county number,
    independent of how many rate centers were scraped there. It is the honest
    denominator for a county-grain analysis.

    Returns one row per county, keyed on (state, county) with the 5-digit
    county FIPS retained as `county_geoid` so it joins cleanly to the
    rate-center tables' own `county_geoid`. Column set mirrors the place-level
    ACS: each variable plus its `_moe` and `_moe_ratio`, derived race shares,
    and `land_area_sqkm` / `pop_density_sqkm` when a county gazetteer is joined
    later.
    """
    variables = resolve_variables(variables)
    states = sorted({str(s).upper() for s in states if s and str(s) != "nan"})
    if not states:
        log.warning("build_county_acs: no states to fetch")
        return pd.DataFrame()

    frames = []
    for st in states:
        f = fetch_acs(st, variables, year, "county", api_key, cache_dir)
        if not f.empty:
            f = f.copy()
            f["state"] = st
            frames.append(f)

    if not frames:
        log.warning("build_county_acs: no county ACS returned for %s", states)
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)

    # (state, county) key: county name from the ACS NAME field, county_geoid
    # from the 5-digit FIPS the county fetch already built into GEOID.
    out = out.rename(columns={"GEOID": "county_geoid"})
    out["county_geoid"] = out["county_geoid"].astype(str).str.zfill(5)
    # ACS NAME is "<County>, <State>"; the leading segment is the county label.
    # Subject-only variable sets can arrive without acs_name (the NAME field
    # rides on the detailed table), so fall back to the FIPS code rather than
    # leaving the key column null -- an all-null county would otherwise get the
    # whole table silently dropped downstream.
    if "acs_name" in out.columns:
        out["county"] = (out["acs_name"].astype(str)
                         .str.split(",").str[0].str.strip())
    else:
        out["county"] = None
    blank = out["county"].isna() | (out["county"].astype(str).str.strip() == "") \
        | out["county"].astype(str).str.lower().isin(["none", "nan"])
    out.loc[blank, "county"] = "FIPS " + out.loc[blank, "county_geoid"].astype(str)

    # derived race shares, matching attach_acs
    if derive_shares and "race_total" in out.columns:
        total = pd.to_numeric(out["race_total"], errors="coerce")
        for src, dst in (
            ("race_white_nh", "pct_white_nh"),
            ("race_black_nh", "pct_black_nh"),
            ("race_asian_nh", "pct_asian_nh"),
            ("race_hispanic", "pct_hispanic"),
        ):
            if src in out.columns:
                out[dst] = (
                    pd.to_numeric(out[src], errors="coerce") / total.where(total > 0) * 100
                ).round(2)

    out = add_moe_ratios(out, variables)

    # lead with the key columns
    lead = ["state", "county", "county_geoid", "acs_name"]
    ordered = [c for c in lead if c in out.columns] + \
              [c for c in out.columns if c not in lead]
    return out[ordered].reset_index(drop=True)


def attach_county_land_area(county_acs: pd.DataFrame,
                            county_gaz: pd.DataFrame) -> pd.DataFrame:
    """Add land area and population density to the county ACS frame.

    Joins the county Gazetteer (already downloaded for the ACS county fallback)
    on the 5-digit GEOID and derives `pop_density_sqkm` where population is
    present, matching the place-level density column.
    """
    if county_acs.empty or county_gaz is None or county_gaz.empty:
        return county_acs

    gaz = county_gaz.copy()
    cols = {c.lower(): c for c in gaz.columns}
    geoid_c = cols.get("geoid")
    aland_c = cols.get("aland")  # land area in square meters
    if not geoid_c or not aland_c:
        return county_acs

    gz = pd.DataFrame({
        "county_geoid": gaz[geoid_c].astype(str).str.zfill(5),
        "land_area_sqkm": pd.to_numeric(gaz[aland_c], errors="coerce") / 1e6,
    })
    out = county_acs.merge(gz, on="county_geoid", how="left")
    if "population" in out.columns:
        pop = pd.to_numeric(out["population"], errors="coerce")
        out["pop_density_sqkm"] = (pop / out["land_area_sqkm"].where(
            out["land_area_sqkm"] > 0)).round(2)
    return out


def attach_acs(
    df: pd.DataFrame,
    variables: list[str] | None = None,
    year: int = DEFAULT_YEAR,
    api_key: str | None = None,
    cache_dir: Path | None = None,
    county_fallback: bool = True,
    derive_shares: bool = True,
) -> pd.DataFrame:
    """Join ACS variables onto an enriched CLLI table.

    `df` needs `geoid` (from enrich.py) and `state`. Rows whose place has no
    ACS record fall back to their county when `county_fallback` is set.
    """
    variables = resolve_variables(variables)

    if "geoid" not in df.columns:
        log.error("No geoid column; run --enrich first")
        return df

    work = df.copy()
    work["geoid"] = work["geoid"].astype(str).str.replace(r"\.0$", "", regex=True)
    work["geoid"] = work["geoid"].str.zfill(7).where(work["geoid"].str.len() > 0)

    states = sorted(
        s for s in work.get("state", pd.Series(dtype=str)).dropna().astype(str).str.upper().unique()
        if s in STATE_FIPS
    )
    if not states:
        log.warning("No usable state codes")
        return df

    # --- place tier ---
    place_frames = []
    for st in states:
        f = fetch_acs(st, variables, year, "place", api_key, cache_dir)
        if not f.empty:
            place_frames.append(f)

    if not place_frames:
        log.warning("No ACS place data retrieved")
        return df

    places = pd.concat(place_frames, ignore_index=True).drop_duplicates("GEOID")

    out = work.merge(places, left_on="geoid", right_on="GEOID", how="left")
    out["geo_level"] = out["GEOID"].notna().map({True: "place", False: None})
    out = out.drop(columns=["GEOID"], errors="ignore")

    # --- county fallback tier ---
    if county_fallback:
        need = out["geo_level"].isna() & out.get(
            "county_geoid", pd.Series(index=out.index, dtype=object)
        ).notna()

        if need.any():
            log.info("%d row(s) without place-level ACS; falling back to county",
                     int(need.sum()))

            county_frames = []
            for st in states:
                f = fetch_acs(st, variables, year, "county", api_key, cache_dir)
                if not f.empty:
                    county_frames.append(f)

            if county_frames:
                counties = (pd.concat(county_frames, ignore_index=True)
                            .drop_duplicates("GEOID"))
                cmap = counties.set_index("GEOID")

                idx = out.index[need]
                keys = out.loc[idx, "county_geoid"].astype(str)
                hit = keys.isin(cmap.index)

                for col in counties.columns:
                    if col == "GEOID":
                        continue
                    vals = keys[hit].map(cmap[col])
                    out.loc[idx[hit.values], col] = vals.values

                out.loc[idx[hit.values], "geo_level"] = "county"
                log.info("County fallback filled %d row(s)", int(hit.sum()))
        elif out["geo_level"].isna().any():
            log.info("County fallback skipped: no county_geoid column. "
                     "Run enrich with --county-geoids to enable it.")

    # --- state tier: last resort for rows that never geocoded ---
    if county_fallback and "state_geoid" in out.columns:
        need = out["geo_level"].isna() & out["state_geoid"].notna()
        if need.any():
            log.info("%d row(s) still unfilled; falling back to state", int(need.sum()))

            state_frames = []
            for st in states:
                f = fetch_acs(st, variables, year, "state", api_key, cache_dir)
                if not f.empty:
                    state_frames.append(f)

            if state_frames:
                sdf = (pd.concat(state_frames, ignore_index=True)
                       .drop_duplicates("GEOID"))
                smap = sdf.set_index("GEOID")

                idx = out.index[need]
                keys = out.loc[idx, "state_geoid"].astype(str)
                hit = keys.isin(smap.index)

                for col in sdf.columns:
                    if col == "GEOID":
                        continue
                    out.loc[idx[hit.values], col] = keys[hit].map(smap[col]).values

                out.loc[idx[hit.values], "geo_level"] = "state"
                log.info("State fallback filled %d row(s)", int(hit.sum()))

    out["geo_level"] = out["geo_level"].fillna("none")

    # --- derived shares ---
    if derive_shares and "race_total" in out.columns:
        total = pd.to_numeric(out["race_total"], errors="coerce")
        for src, dst in (
            ("race_white_nh", "pct_white_nh"),
            ("race_black_nh", "pct_black_nh"),
            ("race_asian_nh", "pct_asian_nh"),
            ("race_hispanic", "pct_hispanic"),
        ):
            if src in out.columns:
                out[dst] = (
                    pd.to_numeric(out[src], errors="coerce") / total.where(total > 0) * 100
                ).round(2)

    out = add_moe_ratios(out, variables)
    return out


def summarize_acs(df: pd.DataFrame, variables: list[str] | None = None) -> str:
    """Coverage and reliability report."""
    variables = resolve_variables(variables)
    lines = []

    if "geo_level" in df.columns:
        vc = df["geo_level"].value_counts()
        lines.append(f"{len(df)} row(s) by ACS geography tier:")
        for k, n in vc.items():
            lines.append(f"  {k:<10} {n:>6}  ({n / len(df):.1%})")

    lines.append("\nvariable coverage and reliability:")
    lines.append(f"  {'variable':<28} {'filled':>8} {'median':>12} {'unreliable':>11}")
    for name in variables:
        if name not in df.columns:
            continue
        s = pd.to_numeric(df[name], errors="coerce")
        filled = int(s.notna().sum())
        med = s.median()
        ratio_col = f"{name}_moe_ratio"
        if ratio_col in df.columns:
            r = pd.to_numeric(df[ratio_col], errors="coerce")
            bad = int((r > 0.30).sum())
            bad_s = f"{bad}"
        else:
            bad_s = "-"
        med_s = f"{med:,.1f}" if pd.notna(med) else "-"
        lines.append(f"  {name:<28} {filled:>8} {med_s:>12} {bad_s:>11}")

    lines.append(
        "\n'unreliable' counts rows where the margin of error exceeds 30% of "
        "the estimate.\nThese are typically small places; filter on "
        "<variable>_moe_ratio before analysis."
    )
    return "\n".join(lines)
