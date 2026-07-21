"""
Normalize the wide enriched CLLI frame into a small relational schema.

The scraper and enrichment layers naturally produce one wide row per CLLI, with
many CLLIs collapsing onto the same telephone rate center. For analysis keyed on
place -- which is what the cancellation study needs -- the useful primary key is
(state, rate_center), not the CLLI. This module reshapes the wide frame into
separate tables that all join on that key, in the spirit of a relational
database rather than one denormalized sheet:

    rate_center        one row per (state, rate_center): the dimension table,
                       carrying a representative lat/lon and roll-up counts
    clli_resolution    one row per scraped CLLI: how it resolved (method, vote
                       distribution, confidence), foreign-keyed to rate_center
    enrich_gazetteer   one row per rate center: geocoding + land area + ACS
    enrich_soil        one row per rate center: flood zone + soil covariates
    enrich_weather     one row per rate center: precipitation covariates

Every enrichment table and clli_resolution join back to rate_center on
(state, rate_center). The split keeps the messy, per-CLLI resolution audit
trail intact and separate from the one-row-per-place enrichment layers, so a
fan-out join can never silently multiply a demographic or soil value across the
several CLLIs that happen to share a center.

The representative row for a center is chosen by highest scraper `confidence`,
breaking ties by `n_prefixes` (better-sampled centers win). Both the choice and
the number of CLLIs behind it are recorded, so the collapse is auditable.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

KEY = ["state", "rate_center"]

# Column groups. Any column not present in a given frame is simply skipped, so
# this stays correct whether or not --acs / --geo / gNATSGO ran.
CLLI_COLUMNS = [
    "clli", "clli_query", "state", "rate_center",
    "rate_center_fd", "resolution_method", "confidence", "n_prefixes",
]

GAZETTEER_COLUMNS = [
    "lat", "lon", "place_name", "geoid", "land_area_sqkm",
    "geo_match_method", "geo_match_score", "geo_match_candidate",
    "county_geoid", "county_name", "state_geoid", "geo_level",
    "acs_name", "population", "population_moe",
    "median_household_income", "median_household_income_moe",
    "median_age", "median_age_moe",
    "pct_broadband", "pct_broadband_moe",
    "pct_no_internet", "pct_no_internet_moe",
    "pct_has_computer", "pct_has_computer_moe",
    "pct_poverty", "pct_poverty_moe",
    "pct_bachelors_plus", "pct_bachelors_plus_moe",
    "median_home_value", "median_gross_rent", "pct_owner_occupied",
    "pct_hs_or_higher", "pct_unemployed", "pct_labor_force",
    "mean_commute_minutes",
    "race_total", "race_white_nh", "race_black_nh", "race_asian_nh",
    "race_hispanic", "pct_white_nh", "pct_black_nh", "pct_asian_nh",
    "pct_hispanic", "pop_density_sqkm",
    # any *_moe_ratio columns are added dynamically below
]

SOIL_COLUMNS = [
    "fld_zone", "fld_zone_subty", "sfha_tf", "flood_risk_ord",
    "wtdepannmin_dom", "wtdepannmin_wavg", "drainage_class_dom",
    "n_soil_components",
    # gNATSGO-only, present only when that backend ran
    "mukey", "wtdepaprjunmin_cm", "drainage_class_wettest",
    "flood_freq_dcd", "ponding_freq_pct_class",
]

WEATHER_COLUMNS = [
    "prism_ppt_annual_mm",
    "job_date", "precip_jobday_mm",
    # any precip_<N>d_sum_mm column is added dynamically below
]


def _present(df: pd.DataFrame, cols: list[str]) -> list[str]:
    """Keep only columns that exist, preserving order and dropping duplicates."""
    seen, out = set(), []
    for c in cols:
        if c in df.columns and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def choose_representatives(df: pd.DataFrame) -> pd.DataFrame:
    """One representative row index per (state, rate_center).

    Sorts by confidence desc, then n_prefixes desc, and takes the first row of
    each key group. Rows missing a key are dropped (they cannot anchor a
    dimension row). Returns the chosen slice, indexed by the original frame.
    """
    work = df.copy()
    for k in KEY:
        if k not in work.columns:
            raise ValueError(f"Frame is missing key column {k!r}")

    work = work[work["state"].notna() & work["rate_center"].notna()]

    conf = pd.to_numeric(work.get("confidence"), errors="coerce").fillna(-1)
    npref = pd.to_numeric(work.get("n_prefixes"), errors="coerce").fillna(-1)
    work = work.assign(_conf=conf, _npref=npref)

    work = work.sort_values(["_conf", "_npref"], ascending=False, kind="stable")
    rep = work.groupby(KEY, sort=False, as_index=False).first()
    return rep.drop(columns=["_conf", "_npref"], errors="ignore")


def build_rate_center(df: pd.DataFrame, rep: pd.DataFrame) -> pd.DataFrame:
    """Dimension table: one row per center with representative geo + roll-ups.

    Roll-ups summarize the CLLIs behind each center so the collapse is legible:
    how many CLLIs resolved to it, how many distinct rate-center strings the
    resolver considered, and the confidence of the row that was chosen.
    """
    grp = df.groupby(KEY, sort=False)
    rollup = grp.agg(
        n_clli=("clli", "nunique") if "clli" in df.columns else ("state", "size"),
    ).reset_index()

    geo_cols = _present(rep, ["lat", "lon", "place_name", "geoid",
                              "land_area_sqkm", "geo_match_method",
                              "geo_match_score", "county_geoid", "county_name",
                              "state_geoid", "geo_level"])
    rep_slim = rep[_present(rep, KEY + geo_cols
                            + ["confidence", "resolution_method", "n_prefixes"])].copy()
    rep_slim = rep_slim.rename(columns={
        "confidence": "rep_confidence",
        "resolution_method": "rep_resolution_method",
        "n_prefixes": "rep_n_prefixes",
    })

    out = rollup.merge(rep_slim, on=KEY, how="left")
    # stable, human-first ordering
    lead = KEY + ["n_clli", "rep_confidence", "rep_resolution_method",
                  "rep_n_prefixes"]
    ordered = _present(out, lead) + [c for c in out.columns if c not in lead]
    return out[ordered]


def build_clli_resolution(df: pd.DataFrame) -> pd.DataFrame:
    """Fact table: one row per scraped CLLI with its resolution detail.

    Foreign-keyed to rate_center by (state, rate_center). Rows without a CLLI
    (e.g. no_results misses) are kept so the audit trail is complete.
    """
    cols = _present(df, CLLI_COLUMNS)
    out = df[cols].copy()
    if "clli" in out.columns:
        out = out.drop_duplicates(subset=["clli"], keep="first")
    return out.reset_index(drop=True)


def _warn_if_inconsistent(df: pd.DataFrame, cols: list[str], label: str) -> None:
    """Warn if a coordinate-derived value differs across a center's rows.

    Soil and weather normals are functions of the center's representative
    coordinate, so they should be identical across the several CLLIs that share
    a center. If they are not, the representative pick silently keeps one value;
    that is a data-quality signal worth surfacing rather than burying, since on
    a large unattended run it usually means two CLLIs geocoded a center to
    different points.
    """
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return
    sub = df[df["state"].notna() & df["rate_center"].notna()]
    nun = sub.groupby(KEY)[cols].nunique(dropna=True)
    diverging = (nun > 1).any(axis=1)
    n = int(diverging.sum())
    if n:
        keys = nun.index[diverging][:5].tolist()
        log.warning("%s: %d center(s) have divergent values across their CLLIs "
                    "(representative row kept); e.g. %s", label, n, keys)


def _enrichment_table(df: pd.DataFrame, rep: pd.DataFrame,
                      base_cols: list[str], dynamic_suffixes: tuple[str, ...] = (),
                      per_clli_passthrough: list[str] | None = None) -> pd.DataFrame:
    """Generic one-row-per-center enrichment table built from representatives.

    `dynamic_suffixes` pulls in columns whose names are not fixed (MOE ratios,
    the lag-width precip sum). `per_clli_passthrough` is unused here but kept so
    the weather table can special-case dated rows if needed.
    """
    dyn = [c for c in df.columns
           if any(c.endswith(s) for s in dynamic_suffixes)]
    cols = _present(rep, KEY + base_cols + dyn)
    return rep[cols].copy().reset_index(drop=True)


def build_gazetteer(df: pd.DataFrame, rep: pd.DataFrame) -> pd.DataFrame:
    _warn_if_inconsistent(df, ["lat", "lon", "geoid"], "gazetteer")
    return _enrichment_table(df, rep, GAZETTEER_COLUMNS,
                             dynamic_suffixes=("_moe_ratio",))


def build_soil(df: pd.DataFrame, rep: pd.DataFrame) -> pd.DataFrame:
    _warn_if_inconsistent(df, ["flood_risk_ord", "drainage_class_dom",
                               "wtdepannmin_dom"], "soil")
    return _enrichment_table(df, rep, SOIL_COLUMNS)


def build_weather(df: pd.DataFrame, rep: pd.DataFrame) -> pd.DataFrame:
    """Weather table.

    Precipitation normals are per center. Dated precip, if present, is per
    (center, job_date) and therefore does NOT collapse to one row per center;
    when dated columns exist the table is emitted at the (center, job_date)
    grain so no observation is lost, while still joining to rate_center on the
    key. Without dates it is one row per center like the others.
    """
    has_dates = "job_date" in df.columns and df["job_date"].notna().any()

    if not has_dates:
        return _enrichment_table(df, rep, WEATHER_COLUMNS,
                                 dynamic_suffixes=("d_sum_mm",))

    dyn = [c for c in df.columns if c.endswith("d_sum_mm")]
    cols = _present(df, KEY + ["prism_ppt_annual_mm", "job_date",
                               "precip_jobday_mm"] + dyn)
    dated = df[df["job_date"].notna()][cols].copy()

    # centers with no job date still deserve a normals row; add them back with
    # null dated fields so the weather table covers every center.
    covered = dated[KEY].drop_duplicates()
    normals = rep.merge(covered, on=KEY, how="left", indicator=True)
    normals = normals[normals["_merge"] == "left_only"]
    if not normals.empty:
        ncols = _present(normals, KEY + ["prism_ppt_annual_mm"])
        normals = normals[ncols].copy()
        for c in cols:
            if c not in normals.columns:
                normals[c] = None
        dated = pd.concat([dated, normals[cols]], ignore_index=True)

    return dated.reset_index(drop=True)


COUNTY_KEY = ["state", "county"]


def build_county(county_acs: pd.DataFrame) -> pd.DataFrame:
    """County dimension table, keyed on (state, county).

    Takes the already-fetched true county ACS frame (one row per county in the
    states present in the data) and normalizes it: enforces the (state, county)
    key, keeps county_geoid so the rate-center tables join on FIPS, and drops
    any duplicate county rows defensively.
    """
    if county_acs is None or county_acs.empty:
        return pd.DataFrame()
    out = county_acs.copy()
    for k in COUNTY_KEY:
        if k not in out.columns:
            raise ValueError(f"county ACS is missing key column {k!r}")
    out = out[out["state"].notna() & out["county"].notna()]
    out = out.drop_duplicates(subset=COUNTY_KEY, keep="first")
    lead = _present(out, ["state", "county", "county_geoid", "acs_name"])
    ordered = lead + [c for c in out.columns if c not in lead]
    return out[ordered].reset_index(drop=True)


def build_schema(df: pd.DataFrame,
                 county_acs: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Split a wide enriched frame into the relational tables.

    Returns a dict of table_name -> DataFrame. Enrichment tables are included
    only when their columns are present, so this adapts to whichever layers ran.
    When `county_acs` is supplied (true county-level ACS), an `enrich_county`
    table keyed on (state, county) is added; it joins to the rate-center tables
    through their shared `county_geoid`.
    """
    rep = choose_representatives(df)

    tables: dict[str, pd.DataFrame] = {
        "rate_center": build_rate_center(df, rep),
        "clli_resolution": build_clli_resolution(df),
    }

    if _present(df, GAZETTEER_COLUMNS) or any(c.endswith("_moe_ratio") for c in df.columns):
        tables["enrich_gazetteer"] = build_gazetteer(df, rep)
    if _present(df, SOIL_COLUMNS):
        tables["enrich_soil"] = build_soil(df, rep)
    if _present(df, WEATHER_COLUMNS) or any(c.endswith("d_sum_mm") for c in df.columns):
        tables["enrich_weather"] = build_weather(df, rep)

    county = build_county(county_acs) if county_acs is not None else pd.DataFrame()
    if not county.empty:
        tables["enrich_county"] = county

    return tables


def write_schema(tables: dict[str, pd.DataFrame], out_dir, prefix: str = "") -> dict:
    """Write each table to <out_dir>/<prefix><name>.csv. Returns paths written."""
    from pathlib import Path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, tbl in tables.items():
        path = out_dir / f"{prefix}{name}.csv"
        tbl.to_csv(path, index=False)
        written[name] = path
    return written


def summarize_schema(tables: dict[str, pd.DataFrame]) -> str:
    """Report row counts and key-uniqueness for each table."""
    lines = ["relational schema:"]
    for name, tbl in tables.items():
        # enrich_county is keyed on (state, county); everything else on
        # (state, rate_center). Check each table against its own key.
        tbl_key = COUNTY_KEY if name == "enrich_county" else KEY
        keyed = all(k in tbl.columns for k in tbl_key)
        if keyed:
            dupes = int(tbl.duplicated(subset=tbl_key).sum())
            uniq = f"unique {tuple(tbl_key)} key" if dupes == 0 \
                else f"{dupes} dup key row(s)"
        else:
            uniq = f"no {tuple(tbl_key)}"
        lines.append(f"  {name:<18} {len(tbl):>7} row(s)   {uniq}")

    rc = tables.get("rate_center")
    cr = tables.get("clli_resolution")
    if rc is not None and cr is not None and "state" in cr.columns:
        centers = rc[KEY].drop_duplicates().shape[0]
        clli_keys = cr[_present(cr, KEY)].drop_duplicates().shape[0]
        orphans = clli_keys - (
            cr.merge(rc[KEY], on=KEY, how="inner")[KEY].drop_duplicates().shape[0]
        )
        lines.append(
            f"\n{len(cr)} CLLI(s) resolve to {centers} rate center(s); "
            f"{orphans} orphan key(s)."
        )

    cty = tables.get("enrich_county")
    if cty is not None and not cty.empty:
        states = cty["state"].nunique() if "state" in cty.columns else 0
        lines.append(
            f"{len(cty)} county row(s) across {states} state(s), keyed on "
            f"(state, county); join to rate_center on county_geoid."
        )

    lines.append("\nRate-center tables join on (state, rate_center); "
                 "clli_resolution is one row\nper CLLI referencing its center. "
                 "enrich_county is one row per county, joined\nvia county_geoid.")
    return "\n".join(lines)
