"""
Enrich CLLI results with lat/lon and population density.

Joins `rate_center` + `state` against the Census Bureau Gazetteer Places file,
which carries name, state, lat/lon, land area, and (via a second file) is the
lowest-effort source that provides geography and population together without
an API key or rate limit.

Matching strategy, in order:

  1. `exact`      -- normalized rate center equals a normalized place name
  2. `expanded`   -- after expanding LERG-style abbreviations, exact match
  3. `fuzzy`      -- rapidfuzz over in-state candidates, above --min-score
  4. `unmatched`  -- nothing above threshold; lat/lon left null and the best
                     candidate recorded in geo_match_candidate for review

Rate centers are telephone billing polygons, not municipal boundaries. A
matched place is an approximation of the rate center's location, not its
extent. Treat density as indicative rather than exact.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "{year}_Gazetteer/{year}_gaz_place_national.zip"
)

COUNTY_GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "{year}_Gazetteer/{year}_gaz_counties_national.zip"
)

DEFAULT_YEAR = 2025

# LERG rate center names are truncated to ~10 chars with predictable
# abbreviations. Expanding these before fuzzy matching converts a large
# share of would-be fuzzy matches into exact ones, which matters because
# fuzzy matching on a 10-char truncation is where silent errors come from.
ABBREV = {
    "SPG": "SPRINGS", "SPGS": "SPRINGS", "SPRG": "SPRINGS", "SPR": "SPRINGS",
    "PT": "POINT", "PNT": "POINT", "PRT": "PORT",
    "HTS": "HEIGHTS", "HGTS": "HEIGHTS", "HT": "HEIGHTS",
    "FLS": "FALLS", "FL": "FALLS",
    "MT": "MOUNT", "MTN": "MOUNTAIN", "MTNS": "MOUNTAINS",
    "FT": "FORT", "FRT": "FORT",
    "ST": "SAINT", "STE": "SAINTE",
    "GRV": "GROVE", "GRDN": "GARDEN", "GDNS": "GARDENS",
    "VLY": "VALLEY", "VLG": "VILLAGE", "VL": "VILLE",
    "CTR": "CENTER", "CTY": "CITY", "CO": "COUNTY",
    "BCH": "BEACH", "BRG": "BRIDGE", "BR": "BRANCH",
    "CRK": "CREEK", "CK": "CREEK",
    "LK": "LAKE", "LKS": "LAKES",
    "RDG": "RIDGE", "RVR": "RIVER", "RIV": "RIVER",
    "JCT": "JUNCTION", "JCTN": "JUNCTION",
    "PK": "PARK", "PRK": "PARK", "PKWY": "PARKWAY",
    "HLS": "HILLS", "HL": "HILL",
    "ISL": "ISLAND", "IS": "ISLAND",
    "SPRNG": "SPRING", "PLNS": "PLAINS", "PLN": "PLAIN",
    "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
    "NW": "NORTHWEST", "NE": "NORTHEAST",
    "SW": "SOUTHWEST", "SE": "SOUTHEAST",
    "PRARI": "PRAIRIE", "PRAIRI": "PRAIRIE", "PRA": "PRAIRIE",
    "CTRL": "CENTRAL", "TWP": "TOWNSHIP",
}

# Rate centers that are not places at all. Flagged rather than force-matched.
NON_PLACE_PAT = re.compile(
    r"\b(ARPT|AIRPORT|XXXX+|TANDEM|TOLL|WIRELESS|UNKNOWN|N/?A)\b"
)

# Suffixes the Gazetteer appends that rate centers never carry.
PLACE_SUFFIX = re.compile(
    r"\s+(CITY|TOWN|VILLAGE|BOROUGH|CDP|MUNICIPALITY|TOWNSHIP|"
    r"\(BALANCE\)|COMPREHENSIVE MUNICIPALITY|URBAN COUNTY|METRO(POLITAN)? GOVERNMENT|"
    r"CONSOLIDATED GOVERNMENT|UNIFIED GOVERNMENT)$"
)


# --------------------------------------------------------------------------
# Gazetteer loading
# --------------------------------------------------------------------------

def _read_gazetteer_zip(content: bytes) -> pd.DataFrame:
    """Parse a Gazetteer zip.

    The delimiter changed from tab to pipe in the 2025 vintage, so it is
    sniffed from the header rather than assumed.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = z.namelist()[0]
        raw = z.read(name)

    head = raw[:4096].decode("latin-1", errors="replace").splitlines()[0]
    sep = "|" if head.count("|") > head.count("\t") else "\t"

    df = pd.read_csv(
        io.BytesIO(raw), sep=sep, dtype={"GEOID": str}, encoding="latin-1"
    )
    df.columns = [c.strip() for c in df.columns]
    return df


def _try_download(url_tmpl: str, years: list[int], timeout: int = 180):
    """Try each vintage in turn, returning (year, DataFrame) for the first hit.

    Census publishes a new vintage annually and does not keep the URL pattern
    stable forever, so a missing year is expected rather than exceptional.
    """
    last_error = None
    for yr in years:
        url = url_tmpl.format(year=yr)
        try:
            log.info("Trying %s", url)
            r = requests.get(url, timeout=timeout)
            if r.status_code == 404:
                log.debug("Not published: %d", yr)
                continue
            r.raise_for_status()
            return yr, _read_gazetteer_zip(r.content)
        except requests.RequestException as exc:
            last_error = exc
            log.debug("Failed %d: %s", yr, exc)

    raise RuntimeError(
        f"Could not download a Gazetteer file from any of {years}. "
        f"Last error: {last_error}. Check "
        "https://www.census.gov/geographies/reference-files/time-series/geo/"
        "gazetteer-files.html for the current vintage and pass --gazetteer-year."
    )


def _candidate_years(requested: int) -> list[int]:
    """Requested vintage first, then recent years newest-first as fallbacks."""
    import datetime
    now = datetime.date.today().year
    order = [requested] + [y for y in range(now, 2019, -1) if y != requested]
    return order


def download_gazetteer(year: int = DEFAULT_YEAR, cache: Path | None = None) -> pd.DataFrame:
    """Fetch and parse the Census Gazetteer Places file.

    Cached to disk so repeat enrichment runs need no network.
    """
    cache = Path(cache) if cache else Path(f"gazetteer_places_{year}.csv")
    if cache.exists():
        log.info("Using cached gazetteer: %s", cache)
        return pd.read_csv(cache, dtype={"GEOID": str})

    got, df = _try_download(GAZETTEER_URL, _candidate_years(year))
    if got != year:
        log.info("Vintage %d unavailable; using %d instead", year, got)

    df.to_csv(cache, index=False)
    log.info("Cached %d places to %s", len(df), cache)
    return df


def prepare_gazetteer(gaz: pd.DataFrame) -> pd.DataFrame:
    """Reduce the Gazetteer to the columns we need, normalized for matching."""
    col = {c.upper(): c for c in gaz.columns}

    def pick(*names):
        for n in names:
            if n in col:
                return col[n]
        raise KeyError(f"None of {names} in gazetteer columns {list(gaz.columns)}")

    out = pd.DataFrame({
        "geoid": gaz[pick("GEOID")].astype(str),
        "place_name": gaz[pick("NAME")].astype(str),
        "state": gaz[pick("USPS")].astype(str).str.upper(),
        "lat": pd.to_numeric(gaz[pick("INTPTLAT")], errors="coerce"),
        "lon": pd.to_numeric(gaz[pick("INTPTLONG", "INTPTLONG ")], errors="coerce"),
        "land_area_sqm": pd.to_numeric(gaz[pick("ALAND")], errors="coerce"),
    })

    out["land_area_sqkm"] = out["land_area_sqm"] / 1e6
    out["norm"] = out["place_name"].map(normalize_place)
    out["nospace"] = out["norm"].str.replace(" ", "", regex=False)
    return out.dropna(subset=["lat", "lon"])


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

# Abbreviations safe to split off the end of a run-together token. Short or
# common letter sequences (ST, PT, N, BR, IS...) are excluded because they
# appear as ordinary word endings: BIRTHRIGHT would become "BIRTHRIG HEIGHTS".
SPLITTABLE = {
    "SPG": "SPRINGS", "SPGS": "SPRINGS", "SPRG": "SPRINGS",
    "HTS": "HEIGHTS", "HGTS": "HEIGHTS",
    "PRARI": "PRAIRIE", "PRAIRI": "PRAIRIE",
    "JCT": "JUNCTION", "JCTN": "JUNCTION",
    "VLY": "VALLEY", "VLG": "VILLAGE",
    "CTR": "CENTER", "MTN": "MOUNTAIN",
    "PKWY": "PARKWAY",
}

# Whole words worth splitting out of run-together names ('WILLSPOINT').
# Only unambiguous, reasonably long words to avoid false positives.
SPLIT_WORDS = [
    "SPRINGS", "MOUNTAIN", "JUNCTION", "PRAIRIE", "HEIGHTS", "VILLAGE",
    "VALLEY", "ISLAND", "CENTER", "BRANCH", "BRIDGE", "CREEK", "POINT",
    "BEACH", "FALLS", "GROVE", "RIDGE", "RIVER", "PLAINS", "FOREST",
    "MEADOW", "GARDEN", "HARBOR", "LANDING", "STATION", "CROSSING",
]


def normalize_place(name: str) -> str:
    """Uppercase, strip Gazetteer suffixes and punctuation."""
    s = str(name).upper().strip()
    s = re.sub(r"[.'`]", "", s)
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(2):  # e.g. "... METROPOLITAN GOVERNMENT (BALANCE)"
        s = PLACE_SUFFIX.sub("", s).strip()
    return s


def expand_abbrev(name: str) -> str:
    """Expand LERG-style truncations token by token.

    Handles three cases: standalone abbreviations ('WALNUT SPG'), run-together
    names ending in a safe abbreviation ('SULPHURSPG'), and run-together names
    ending in a full word ('WILLSPOINT'). Deliberately conservative -- a wrong
    expansion produces a confident wrong match, which is worse than a miss.
    """
    s = normalize_place(name)
    if not s:
        return s

    out = []
    for tok in s.split():
        if tok in ABBREV:
            out.append(ABBREV[tok])
            continue

        if len(tok) > 7:
            split = None

            # trailing full word, e.g. WILLSPOINT -> WILLS POINT
            for word in SPLIT_WORDS:
                if tok.endswith(word) and len(tok) - len(word) >= 4:
                    split = (tok[: -len(word)], word)
                    break

            # trailing safe abbreviation, e.g. SULPHURSPG -> SULPHUR SPRINGS
            if split is None:
                for abbr, full in SPLITTABLE.items():
                    if tok.endswith(abbr) and len(tok) - len(abbr) >= 5:
                        split = (tok[: -len(abbr)], full)
                        break

            if split:
                out.extend(split)
                continue

        out.append(tok)

    return " ".join(out)


def match_keys(rate_center: str) -> tuple[str, str, str]:
    """Return (normalized, expanded, expanded-without-spaces) for a rate center."""
    norm = normalize_place(rate_center)
    exp = expand_abbrev(rate_center)
    return norm, exp, exp.replace(" ", "")


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def enrich(
    df: pd.DataFrame,
    gaz: pd.DataFrame,
    min_score: float = 85.0,
    population: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach lat/lon, land area, and (if supplied) population + density."""
    from rapidfuzz import fuzz, process

    gz = prepare_gazetteer(gaz)

    # Index candidates by state so fuzzy matching searches ~1k names, not ~30k.
    by_state: dict[str, pd.DataFrame] = {s: g for s, g in gz.groupby("state")}

    records = []
    for row in df.itertuples(index=False):
        rc = getattr(row, "rate_center", None)
        st = str(getattr(row, "state", "") or "").upper()

        base = {
            "lat": None, "lon": None, "place_name": None, "geoid": None,
            "land_area_sqkm": None,
            "geo_match_method": "unmatched",
            "geo_match_score": None,
            "geo_match_candidate": None,
        }

        if not rc or pd.isna(rc):
            base["geo_match_method"] = "no_rate_center"
            records.append(base)
            continue

        rc = str(rc)
        if NON_PLACE_PAT.search(rc.upper()):
            base["geo_match_method"] = "non_place"
            records.append(base)
            continue

        cand = by_state.get(st)
        if cand is None or cand.empty:
            base["geo_match_method"] = "no_state_candidates"
            records.append(base)
            continue

        norm, exp, nospace = match_keys(rc)

        hit, method, score = None, None, None

        # 1. exact on the raw normalized name
        m = cand[cand["norm"] == norm]
        if not m.empty:
            hit, method, score = m.iloc[0], "exact", 100.0

        # 2. exact after abbreviation expansion
        if hit is None and exp != norm:
            m = cand[cand["norm"] == exp]
            if not m.empty:
                hit, method, score = m.iloc[0], "expanded", 100.0

        # 3. exact ignoring spaces (handles run-together truncations)
        if hit is None:
            m = cand[cand["nospace"] == nospace]
            if not m.empty:
                hit, method, score = m.iloc[0], "expanded_nospace", 100.0

        # 4. fuzzy over in-state candidates
        if hit is None:
            choices = cand["norm"].tolist()
            best = process.extractOne(exp, choices, scorer=fuzz.WRatio)
            if best:
                name, sc, idx = best
                base["geo_match_candidate"] = name
                if sc >= min_score:
                    hit, method, score = cand.iloc[idx], "fuzzy", round(float(sc), 1)
                else:
                    base["geo_match_score"] = round(float(sc), 1)

        if hit is not None:
            base.update({
                "lat": float(hit["lat"]),
                "lon": float(hit["lon"]),
                "place_name": hit["place_name"],
                "geoid": hit["geoid"],
                "land_area_sqkm": round(float(hit["land_area_sqkm"]), 4),
                "geo_match_method": method,
                "geo_match_score": score,
                "geo_match_candidate": hit["norm"],
            })

        records.append(base)

    enr = pd.DataFrame(records, index=df.index)
    out = pd.concat([df.reset_index(drop=True), enr.reset_index(drop=True)], axis=1)

    if population is not None:
        out = attach_population(out, population)

    return out


def attach_population(df: pd.DataFrame, pop: pd.DataFrame) -> pd.DataFrame:
    """Join a population table on GEOID and compute density.

    `pop` needs a GEOID-like column and a population column. Census place
    population comes from the ACS 5-year table B01003 or the PEP place file.
    """
    p = pop.copy()
    p.columns = [c.lower() for c in p.columns]

    geo_col = next((c for c in p.columns if "geoid" in c or c in ("id", "place")), None)
    pop_col = next(
        (c for c in p.columns
         if "pop" in c or c in ("b01003_001e", "value", "estimate")),
        None,
    )
    if geo_col is None or pop_col is None:
        log.warning("Population table needs a GEOID and a population column; "
                    "found %s. Skipping.", list(p.columns))
        return df

    p = p[[geo_col, pop_col]].rename(columns={geo_col: "geoid", pop_col: "population"})
    p["geoid"] = p["geoid"].astype(str).str.replace(r"^\D*", "", regex=True).str[-7:]
    p["population"] = pd.to_numeric(p["population"], errors="coerce")

    out = df.copy()
    out["_g"] = out["geoid"].astype(str).str[-7:]
    out = out.merge(p, left_on="_g", right_on="geoid", how="left",
                    suffixes=("", "_pop")).drop(columns=["_g", "geoid_pop"],
                                                errors="ignore")

    # Density is only meaningful where both sides are present and non-zero.
    out["pop_density_sqkm"] = (
        out["population"] / out["land_area_sqkm"].where(out["land_area_sqkm"] > 0)
    ).round(2)
    return out


def download_county_gazetteer(year: int = DEFAULT_YEAR,
                              cache: Path | None = None) -> pd.DataFrame:
    """Fetch the Census county Gazetteer, used for the ACS county fallback."""
    cache = Path(cache) if cache else Path(f"./data/csv/gazetteer_counties_{year}.csv")
    if cache.exists():
        log.info("Using cached county gazetteer: %s", cache)
        return pd.read_csv(cache, dtype={"GEOID": str})

    got, df = _try_download(COUNTY_GAZETTEER_URL, _candidate_years(year))
    if got != year:
        log.info("County vintage %d unavailable; using %d instead", year, got)

    df.to_csv(cache, index=False)
    log.info("Cached %d counties to %s", len(df), cache)
    return df


def assign_counties(df: pd.DataFrame, counties: pd.DataFrame) -> pd.DataFrame:
    """Attach the nearest county centroid's GEOID to each geocoded row.

    Nearest-centroid is an approximation -- a place near a county line may be
    assigned to its neighbour. It exists only to give the ACS layer a coarse
    fallback where place-level data is missing, so the imprecision is
    acceptable relative to having no data at all.
    """
    col = {c.upper(): c for c in counties.columns}

    def pick(*names):
        for n in names:
            if n in col:
                return col[n]
        raise KeyError(f"None of {names} in {list(counties.columns)}")

    cty = pd.DataFrame({
        "county_geoid": counties[pick("GEOID")].astype(str).str.zfill(5),
        "county_name": counties[pick("NAME")].astype(str),
        "state": counties[pick("USPS")].astype(str).str.upper(),
        "clat": pd.to_numeric(counties[pick("INTPTLAT")], errors="coerce"),
        "clon": pd.to_numeric(counties[pick("INTPTLONG", "INTPTLONG ")], errors="coerce"),
    }).dropna(subset=["clat", "clon"])

    out = df.copy()
    out["county_geoid"] = None
    out["county_name"] = None

    if "lat" not in out.columns:
        return out

    for st, grp in out.groupby(out.get("state", pd.Series(dtype=str)).astype(str).str.upper()):
        pool = cty[cty["state"] == st]
        if pool.empty:
            continue

        sub = grp[grp["lat"].notna() & grp["lon"].notna()]
        if sub.empty:
            continue

        lat = pd.to_numeric(sub["lat"], errors="coerce").to_numpy()[:, None]
        lon = pd.to_numeric(sub["lon"], errors="coerce").to_numpy()[:, None]
        clat = pool["clat"].to_numpy()[None, :]
        clon = pool["clon"].to_numpy()[None, :]

        # Equirectangular approximation: adequate for nearest-neighbour within
        # a single state, and avoids a scipy dependency.
        import numpy as np
        x = (clon - lon) * np.cos(np.radians((lat + clat) / 2))
        y = clat - lat
        nearest = np.argmin(x ** 2 + y ** 2, axis=1)

        out.loc[sub.index, "county_geoid"] = pool["county_geoid"].to_numpy()[nearest]
        out.loc[sub.index, "county_name"] = pool["county_name"].to_numpy()[nearest]

    return out


def assign_state_geoids(df: pd.DataFrame) -> pd.DataFrame:
    """Attach a state FIPS for rows that never got coordinates.

    Rows whose rate center failed to geocode have no lat/lon, so nearest-county
    assignment cannot reach them -- precisely the rows most in need of a
    fallback. State-level ACS is coarse but is real data rather than a blank,
    and `geo_level` records that it came from the state tier.
    """
    from .acs import STATE_FIPS

    out = df.copy()
    if "state" not in out.columns:
        out["state_geoid"] = None
        return out

    out["state_geoid"] = (
        out["state"].astype(str).str.upper().map(STATE_FIPS)
    )
    return out


def summarize(df: pd.DataFrame) -> str:
    """Human-readable match-quality report."""
    lines = []
    if "geo_match_method" in df.columns:
        vc = df["geo_match_method"].value_counts()
        total = len(df)
        lines.append(f"{total} row(s):")
        for method, n in vc.items():
            lines.append(f"  {method:<20} {n:>6}  ({n / total:.1%})")

        fuzzy = df[df["geo_match_method"] == "fuzzy"]
        if not fuzzy.empty:
            lines.append(f"\nfuzzy matches, lowest scores (verify these):")
            cols = [c for c in ("rate_center", "place_name", "geo_match_score")
                    if c in fuzzy.columns]
            for r in fuzzy.nsmallest(min(8, len(fuzzy)), "geo_match_score")[cols].itertuples(index=False):
                lines.append("  " + "  ".join(str(x) for x in r))

        un = df[df["geo_match_method"] == "unmatched"]
        if not un.empty:
            lines.append(f"\n{len(un)} unmatched; nearest rejected candidates:")
            cols = [c for c in ("rate_center", "geo_match_candidate", "geo_match_score")
                    if c in un.columns]
            for r in un.head(8)[cols].itertuples(index=False):
                lines.append("  " + "  ".join(str(x) for x in r))

    return "\n".join(lines)
