"""
Attach hydrological and geological covariates to enriched CLLI results.

Adds, per geocoded rate center, the variables needed to test whether flood
proneness (rather than merely wet weather) predicts install cancellations:

  FEMA NFHL   -- flood zone, SFHA flag, ordinal flood-hazard rank
  USDA SDA    -- annual-minimum water-table depth, soil drainage class
  PRISM       -- 30-year annual precipitation normal (static "wet region")
  Daymet      -- per-job daily precipitation on a supplied job date, plus a
                 lagged rolling sum (only when a dates table is provided)

Each source is queried at the row's `lat`/`lon` (produced by enrich.py) and is
cached to disk keyed by rounded coordinate, so repeated or resumed runs need no
network. A failure in one source nulls only that source's columns; the others
still populate.

Two caveats worth carrying into analysis, mirroring the ones enrich.py already
documents. A rate center is a billing polygon; its matched place centroid is a
single point, so flood zone and soil are point proxies, not areal averages --
a center straddling a flood boundary may be misrepresented. And the static
PRISM normal answers "is this a wet region", whereas the dated Daymet columns
answer "was it raining near the job" -- only the latter separates weather from
geology, and it requires a job-date table this scraper does not itself produce.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
SDA = "https://sdmdataaccess.sc.egov.usda.gov/tabular/post.rest"
PRISM = "https://prism.oregonstate.edu/explorer/dataexplorer/rss.php"
DAYMET = "https://daymet.ornl.gov/single-pixel/api/data"

# FEMA flood-hazard zones, ranked so the ordinal can enter a model directly.
# V* (coastal high hazard, wave action) > A* (1% annual SFHA) > shaded X
# (0.2% annual) > X / minimal > unmapped. See FEMA NFHL FLD_ZONE domain.
_A_ZONES = {"A", "AE", "AH", "AO", "AR", "A99"}


def _flood_rank(zone: str | None, subty: str | None) -> int:
    """Ordinal flood-hazard rank; higher is more hazardous, 0 is none/unmapped."""
    if not zone:
        return 0
    z = zone.upper()
    if z.startswith("V"):
        return 5
    if z in _A_ZONES:
        return 4
    if z == "D":  # undetermined-but-possible hazard
        return 2
    if z == "X":
        return 1 if subty and "0.2" in subty else 0  # shaded vs unshaded X
    return 3  # any other mapped zone: treat as intermediate rather than drop


# --------------------------------------------------------------------------
# Session and disk cache (mirrors the per-source caching in acs.py)
# --------------------------------------------------------------------------

def _make_session(total_retries: int = 4, backoff: float = 2.0) -> requests.Session:
    """Session with urllib3 retry on transient statuses, as scraper.py does."""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update({"User-Agent": "clli-scrape geo-enrich/0.1"})
    retry = Retry(
        total=total_retries, connect=total_retries, read=total_retries,
        status=total_retries, backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"], respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class _Cache:
    """One JSON file per source, keyed by rounded coordinate (or coord|date).

    Written after every miss so a killed run resumes without re-fetching. Error
    results are cached too, so a resume does not re-hit a point that failed for
    a structural reason; delete the file (or its error entries) to force retry.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read cache %s (%s); starting empty",
                            self.path, exc)

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)


def _key(lat: float, lon: float, ndigits: int = 5) -> str:
    return f"{round(lat, ndigits)},{round(lon, ndigits)}"


# --------------------------------------------------------------------------
# Per-source point queries
# --------------------------------------------------------------------------

def query_flood(lat: float, lon: float, session, cache: _Cache,
                timeout: int = 60) -> dict:
    """FEMA NFHL flood zone at a point."""
    k = _key(lat, lon)
    hit = cache.get(k)
    if hit is not None:
        return hit

    params = {
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF", "returnGeometry": "false",
        "f": "json",
    }
    out = {"fld_zone": None, "fld_zone_subty": None, "sfha_tf": None,
           "flood_risk_ord": None}
    try:
        r = session.get(NFHL, params=params, timeout=timeout)
        r.raise_for_status()
        feats = r.json().get("features", [])
        if feats:
            a = feats[0]["attributes"]
            z, sub = a.get("FLD_ZONE"), a.get("ZONE_SUBTY")
            out = {"fld_zone": z, "fld_zone_subty": sub,
                   "sfha_tf": a.get("SFHA_TF"), "flood_risk_ord": _flood_rank(z, sub)}
        else:
            out["flood_risk_ord"] = 0  # queried cleanly; point not in a hazard poly
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.debug("NFHL failed at %s: %s", k, exc)
        out["_err"] = str(exc)
    cache.put(k, out)
    return out


# Extra muaggatt fields the gNATSGO backend can surface that SDA's per-point
# component query does not. All are pre-aggregated to the map unit, so they are
# read directly rather than rolled up here. flodfreqdcd and pondfreqprs are the
# most on-topic for an install-cancellation model: they name flooding and
# ponding frequency at the soil-map-unit level, independent of weather.
GNATSGO_MUAGGATT_FIELDS = {
    "wtdepannmin": "wtdepannmin_cm",       # annual-min water table depth (cm)
    "wtdepaprjunmin": "wtdepaprjunmin_cm",  # Apr-Jun min water table depth (cm)
    "drclassdcd": "drainage_class_dcd",     # dominant drainage class
    "drclasswettest": "drainage_class_wettest",  # wettest component's class
    "flodfreqdcd": "flood_freq_dcd",        # dominant flooding frequency class
    "pondfreqprs": "ponding_freq_pct_class",  # ponding presence class
}


class GNatsgoReader:
    """Point sampler over a local gNATSGO GeoPackage + mukey raster.

    gNATSGO ships the map-unit aggregated table (`muaggatt`) inside a SQLite
    GeoPackage and the map-unit keys as 30 m `muraster` GeoTIFF(s), joined on
    `mukey`. Sampling locally turns one throttled SDA HTTP call per point into a
    fast in-process raster lookup, which is what makes a full CLLI run feasible.

    The raster (or rasters) and the GeoPackage are opened once and reused for
    every point. Coordinates are reprojected from WGS84 to each raster's CRS on
    the fly; a point is tried against each raster until one covers it, so CONUS
    plus the Alaska/Hawaii/island tiles can be passed together.

    Requires `rasterio` and `pyproj`; if either import fails the reader raises
    at construction so the caller can fall back to the online SDA backend.
    """

    def __init__(self, gpkg_path, raster_paths):
        import rasterio  # noqa: F401  (import-time availability check)
        from pyproj import Transformer  # noqa: F401
        import sqlite3

        self._rasterio = __import__("rasterio")
        self._Transformer = Transformer

        self.gpkg_path = Path(gpkg_path)
        if not self.gpkg_path.exists():
            raise FileNotFoundError(f"gNATSGO GeoPackage not found: {self.gpkg_path}")

        paths = [Path(p) for p in raster_paths]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise FileNotFoundError(f"muraster file(s) not found: {missing}")
        if not paths:
            raise ValueError("At least one muraster path is required")

        # open rasters once; cache a WGS84->raster transformer per raster
        self._rasters = []
        for p in paths:
            ds = self._rasterio.open(p)
            tf = self._Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            self._rasters.append((ds, tf))

        # load muaggatt into a mukey -> {field: value} dict, once
        cols = ", ".join(["mukey", *GNATSGO_MUAGGATT_FIELDS])
        con = sqlite3.connect(f"file:{self.gpkg_path}?mode=ro", uri=True)
        try:
            cur = con.execute(f"SELECT {cols} FROM muaggatt")
            names = [d[0] for d in cur.description]
            self._muaggatt = {}
            for rec in cur.fetchall():
                d = dict(zip(names, rec))
                mk = str(d.pop("mukey"))
                self._muaggatt[mk] = d
        finally:
            con.close()
        log.info("gNATSGO: %d muaggatt rows, %d raster(s) loaded",
                 len(self._muaggatt), len(self._rasters))

    def mukey_at(self, lat: float, lon: float):
        """Return the mukey covering a WGS84 point, or None if uncovered."""
        for ds, tf in self._rasters:
            x, y = tf.transform(lon, lat)
            b = ds.bounds
            if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
                continue
            try:
                val = next(ds.sample([(x, y)]))[0]
            except StopIteration:
                continue
            if val is None:
                continue
            # nodata guard
            nod = ds.nodata
            if nod is not None and val == nod:
                continue
            return str(int(val))
        return None

    def lookup(self, lat: float, lon: float) -> dict:
        """Sample mukey at the point and return its muaggatt fields."""
        mk = self.mukey_at(lat, lon)
        if mk is None:
            return {"mukey": None}
        rec = self._muaggatt.get(mk)
        out = {"mukey": mk}
        if rec:
            for src, dst in GNATSGO_MUAGGATT_FIELDS.items():
                out[dst] = rec.get(src)
        return out

    def close(self):
        for ds, _tf in self._rasters:
            try:
                ds.close()
            except Exception:  # noqa: BLE001
                pass


def query_soil_gnatsgo(lat: float, lon: float, reader: "GNatsgoReader",
                       cache: _Cache) -> dict:
    """Offline soil covariates from a local gNATSGO reader, cached by coord.

    Output is a superset of the SDA backend's contract: it keeps
    `wtdepannmin_dom` / `drainage_class_dom` (mapped from the map-unit dominant
    values so downstream code and the summary need no branching) and adds the
    flooding, ponding, and seasonal water-table fields that gNATSGO exposes.
    """
    k = _key(lat, lon)
    hit = cache.get(k)
    if hit is not None:
        return hit

    out = {"wtdepannmin_dom": None, "wtdepannmin_wavg": None,
           "drainage_class_dom": None, "n_soil_components": None}
    try:
        rec = reader.lookup(lat, lon)
        mk = rec.get("mukey")
        if mk is not None:
            # keep the shared-contract columns populated from the aggregated
            # values so summarize_geo and any modelling code stay backend-blind
            out["wtdepannmin_dom"] = rec.get("wtdepannmin_cm")
            out["wtdepannmin_wavg"] = rec.get("wtdepannmin_cm")  # already MU-aggregated
            out["drainage_class_dom"] = rec.get("drainage_class_dcd")
            out["n_soil_components"] = None  # not exposed by the aggregated table
            # richer gNATSGO-only fields
            out["mukey"] = mk
            out["wtdepaprjunmin_cm"] = rec.get("wtdepaprjunmin_cm")
            out["drainage_class_wettest"] = rec.get("drainage_class_wettest")
            out["flood_freq_dcd"] = rec.get("flood_freq_dcd")
            out["ponding_freq_pct_class"] = rec.get("ponding_freq_pct_class")
    except Exception as exc:  # noqa: BLE001  (reader/raster/sqlite errors vary)
        log.debug("gNATSGO lookup failed at %s: %s", k, exc)
        out["_err"] = str(exc)
    cache.put(k, out)
    return out


# gNATSGO-only columns, appended to the output when that backend is used.
GNATSGO_EXTRA_COLUMNS = [
    "mukey", "wtdepaprjunmin_cm", "drainage_class_wettest",
    "flood_freq_dcd", "ponding_freq_pct_class",
]


def query_soil(lat: float, lon: float, session, cache: _Cache,
               timeout: int = 60) -> dict:
    """USDA SSURGO water-table depth and drainage class at a point.

    Categorical drainage class takes the dominant component (highest
    comppct_r); numeric water-table depth is reported both as the dominant
    component's value and as a comppct-weighted average, since averaging a
    continuous depth is more faithful than picking one component.
    """
    k = _key(lat, lon)
    hit = cache.get(k)
    if hit is not None:
        return hit

    sql = (
        "SELECT c.cokey, c.comppct_r, c.drainagecl, muag.wtdepannmin "
        "FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('point(%f %f)') AS p "
        "INNER JOIN mapunit mu ON mu.mukey = p.mukey "
        "INNER JOIN component c ON c.mukey = mu.mukey "
        "LEFT JOIN muaggatt muag ON muag.mukey = mu.mukey "
        "ORDER BY c.comppct_r DESC" % (lon, lat)
    )
    out = {"wtdepannmin_dom": None, "wtdepannmin_wavg": None,
           "drainage_class_dom": None, "n_soil_components": None}
    try:
        r = session.post(SDA, json={"query": sql, "format": "JSON"}, timeout=timeout)
        r.raise_for_status()
        rows = r.json().get("Table", [])
        comps = []
        for row in rows:
            pct = float(row[1]) if row[1] not in (None, "") else 0.0
            wt = float(row[3]) if row[3] not in (None, "") else None
            comps.append((pct, row[2], wt))
        if comps:
            out["n_soil_components"] = len(comps)
            out["drainage_class_dom"] = comps[0][1]  # rows arrive comppct DESC
            out["wtdepannmin_dom"] = comps[0][2]
            num = sum(p * w for (p, _d, w) in comps if w is not None)
            den = sum(p for (p, _d, w) in comps if w is not None)
            out["wtdepannmin_wavg"] = round(num / den, 2) if den > 0 else None
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        log.debug("SDA failed at %s: %s", k, exc)
        out["_err"] = str(exc)
    cache.put(k, out)
    return out


def query_prism(lat: float, lon: float, session, cache: _Cache,
                timeout: int = 60) -> dict:
    """PRISM 30-year annual precipitation normal (mm) at a point."""
    k = _key(lat, lon, 4)
    hit = cache.get(k)
    if hit is not None:
        return hit

    params = {"spn": f"{lon},{lat}", "start": "0", "end": "0", "interp": "idw",
              "stats": "ppt", "units": "metric", "range": "monthly",
              "proc": "gridserv"}
    out = {"prism_ppt_annual_mm": None}
    try:
        r = session.get(PRISM, params=params, timeout=timeout)
        r.raise_for_status()
        vals = []
        for ln in r.text.splitlines():
            parts = ln.strip().split(",")
            if len(parts) >= 4:
                try:
                    vals.append(float(parts[-1]))
                except ValueError:
                    pass
        if vals:  # sum the 12 monthly normals into an annual total
            out["prism_ppt_annual_mm"] = round(sum(vals), 1)
    except (requests.RequestException, ValueError) as exc:
        log.debug("PRISM failed at %s: %s", k, exc)
        out["_err"] = str(exc)
    cache.put(k, out)
    return out


def query_daymet(lat: float, lon: float, job_date: str, lag: int, session,
                 cache: _Cache, timeout: int = 60) -> dict:
    """Daymet daily precip on `job_date` and summed over [job_date-lag, job_date]."""
    from datetime import date, timedelta

    k = f"{_key(lat, lon, 4)}|{job_date}|{lag}"
    hit = cache.get(k)
    if hit is not None:
        return hit

    out = {"precip_jobday_mm": None, f"precip_{lag}d_sum_mm": None}
    try:
        jd = date.fromisoformat(job_date)
        params = {"lat": lat, "lon": lon, "vars": "prcp", "years": jd.year}
        r = session.get(DAYMET, params=params, timeout=timeout)
        r.raise_for_status()

        daily = {}
        for ln in r.text.splitlines():
            p = ln.split(",")
            if len(p) >= 3:
                try:
                    yr, yday, prcp = int(float(p[0])), int(float(p[1])), float(p[2])
                    daily[(yr, yday)] = prcp
                except ValueError:
                    continue

        def yday_of(d):
            return (d - date(d.year, 1, 1)).days + 1

        out["precip_jobday_mm"] = daily.get((jd.year, yday_of(jd)))
        s, got = 0.0, False
        for i in range(lag + 1):
            d = jd - timedelta(days=i)
            v = daily.get((d.year, yday_of(d)))
            if v is not None:
                s += v
                got = True
        out[f"precip_{lag}d_sum_mm"] = round(s, 2) if got else None
    except (requests.RequestException, ValueError) as exc:
        log.debug("Daymet failed at %s|%s: %s", _key(lat, lon, 4), job_date, exc)
        out["_err"] = str(exc)
    cache.put(k, out)
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

BASE_COLUMNS = [
    "fld_zone", "fld_zone_subty", "sfha_tf", "flood_risk_ord",
    "wtdepannmin_dom", "wtdepannmin_wavg", "drainage_class_dom",
    "n_soil_components", "prism_ppt_annual_mm",
]


def load_job_dates(path: Path) -> dict[tuple[str, str], list[str]]:
    """(state, rate_center) -> [job_date, ...] from a CSV with those columns."""
    m: dict[tuple[str, str], list[str]] = {}
    d = pd.read_csv(path, dtype=str)
    need = {"state", "rate_center", "job_date"}
    if not need.issubset(d.columns):
        raise ValueError(f"--geo-dates needs columns {sorted(need)}; got {list(d.columns)}")
    for r in d.itertuples(index=False):
        m.setdefault((str(r.state).upper(), str(r.rate_center)), []).append(str(r.job_date))
    return m


def attach_geo(
    df: pd.DataFrame,
    cache_dir: Path | None = None,
    job_dates: dict[tuple[str, str], list[str]] | None = None,
    lag: int = 3,
    sleep: float = 0.3,
    soil_source: str = "sda",
    gnatsgo_gpkg: Path | None = None,
    gnatsgo_rasters: list[Path] | None = None,
) -> pd.DataFrame:
    """Attach flood, soil, and precipitation covariates to an enriched table.

    `df` needs `lat`/`lon` (from enrich.py). Queries are deduplicated by
    coordinate, so many rate centers sharing a centroid cost one call each.
    When `job_dates` is supplied, rows for a matched (state, rate_center) are
    expanded to one row per job date with dated Daymet precip attached; all
    other rows keep a single row with the dated columns left null.

    `soil_source` selects the soil backend: "sda" (default) queries USDA Soil
    Data Access over HTTP per point, needing no local files; "gnatsgo" samples a
    local gNATSGO GeoPackage + mukey raster instead, which is offline, far
    faster for large inputs, and exposes extra flooding / ponding / seasonal
    water-table fields. If the gNATSGO reader cannot be built (missing files or
    missing rasterio/pyproj) the run falls back to SDA rather than aborting.
    """
    if "lat" not in df.columns or "lon" not in df.columns:
        log.error("No lat/lon columns; run --enrich first")
        return df

    cache_dir = Path(cache_dir or "geo_cache")
    session = _make_session()
    c_flood = _Cache(cache_dir / "flood.json")
    c_soil = _Cache(cache_dir / "soil.json")
    c_prism = _Cache(cache_dir / "prism.json")
    c_daymet = _Cache(cache_dir / "daymet.json")

    # --- optional offline soil backend ---
    reader = None
    extra_soil_cols: list[str] = []
    if soil_source == "gnatsgo":
        try:
            if not gnatsgo_gpkg or not gnatsgo_rasters:
                raise ValueError("gNATSGO backend needs a GeoPackage and at least "
                                 "one muraster path")
            reader = GNatsgoReader(gnatsgo_gpkg, gnatsgo_rasters)
            extra_soil_cols = list(GNATSGO_EXTRA_COLUMNS)
            # gNATSGO soil values differ from SDA, so keep them in a separate
            # cache file to avoid cross-contaminating a prior SDA run's cache.
            c_soil = _Cache(cache_dir / "soil_gnatsgo.json")
        except Exception as exc:  # noqa: BLE001
            log.warning("gNATSGO backend unavailable (%s); falling back to SDA", exc)
            reader = None
            soil_source = "sda"

    lag_col = f"precip_{lag}d_sum_mm"
    dated = job_dates is not None

    # Cache the static (coordinate-only) enrichment per unique coordinate so we
    # query each point once even if many rows share it.
    static_by_coord: dict[str, dict] = {}

    def static_for(lat: float, lon: float) -> dict:
        k = _key(lat, lon)
        if k in static_by_coord:
            return static_by_coord[k]
        rec = {}
        rec.update(query_flood(lat, lon, session, c_flood))
        if reader is not None:
            rec.update(query_soil_gnatsgo(lat, lon, reader, c_soil))
        else:
            rec.update(query_soil(lat, lon, session, c_soil))
        rec.update(query_prism(lat, lon, session, c_prism))
        rec.pop("_err", None)
        static_by_coord[k] = rec
        # Offline gNATSGO reads need no politeness delay; only sleep for the
        # HTTP backends (flood + PRISM are always HTTP, so keep a delay unless
        # everything queried was local -- here flood/PRISM keep it non-zero).
        time.sleep(sleep)
        return rec

    coords_total = df[["lat", "lon"]].dropna().drop_duplicates().shape[0]
    log.info("Geo-enriching %d row(s) across %d unique coordinate(s)%s",
             len(df), coords_total, " with dated precip" if dated else "")

    # gNATSGO adds columns; include them so every row (misses included) is aligned
    soil_cols = BASE_COLUMNS + extra_soil_cols

    out_rows = []
    seen = 0
    # A job date belongs to a (state, rate_center), not to an individual CLLI
    # row. When a rate center appears on several input rows, its dated rows must
    # be emitted only once, or the precip observations get duplicated. Later
    # rows for the same center fall through to a single null-dated row so their
    # own CLLI metadata is still preserved.
    dated_emitted: set[tuple[str, str]] = set()
    for row in df.itertuples(index=False):
        base = row._asdict()
        lat, lon = base.get("lat"), base.get("lon")

        if pd.isna(lat) or pd.isna(lon):
            rec = {c: None for c in soil_cols}
            if dated:
                rec.update({"job_date": None, "precip_jobday_mm": None, lag_col: None})
            out_rows.append({**base, **rec})
            continue

        lat, lon = float(lat), float(lon)
        if _key(lat, lon) not in static_by_coord:
            seen += 1
            if seen % 25 == 0:
                log.info("  %d/%d coordinates done", seen, coords_total)
        stat = static_for(lat, lon)
        rec = {c: stat.get(c) for c in soil_cols}

        djs = None
        if dated:
            st = str(base.get("state", "") or "").upper()
            rc = str(base.get("rate_center", "") or "")
            center = (st, rc)
            if center not in dated_emitted:
                djs = job_dates.get(center)

        if dated and djs:
            dated_emitted.add(center)
            for jd in djs:
                dm = query_daymet(lat, lon, jd, lag, session, c_daymet)
                out_rows.append({**base, **rec, "job_date": jd,
                                 "precip_jobday_mm": dm.get("precip_jobday_mm"),
                                 lag_col: dm.get(lag_col)})
                time.sleep(sleep)
        else:
            if dated:
                rec.update({"job_date": None, "precip_jobday_mm": None, lag_col: None})
            out_rows.append({**base, **rec})

    if reader is not None:
        reader.close()

    return pd.DataFrame(out_rows)


def summarize_geo(df: pd.DataFrame) -> str:
    """Coverage report for the geo layer, in the style of summarize_acs."""
    lines = [f"{len(df)} row(s) geo-enriched:"]

    if "flood_risk_ord" in df.columns:
        r = pd.to_numeric(df["flood_risk_ord"], errors="coerce")
        filled = int(r.notna().sum())
        sfha = int((r >= 4).sum())
        lines.append(f"  flood zone resolved      {filled:>6}  ({filled/len(df):.1%})")
        lines.append(f"  in an SFHA (rank >= 4)   {sfha:>6}  ({sfha/len(df):.1%})")
        if "fld_zone" in df.columns:
            top = df.loc[r.notna(), "fld_zone"].value_counts().head(6)
            for z, n in top.items():
                lines.append(f"      zone {str(z):<6} {n:>6}")

    for name, label in (("wtdepannmin_wavg", "water-table depth"),
                        ("drainage_class_dom", "drainage class"),
                        ("prism_ppt_annual_mm", "PRISM annual precip")):
        if name in df.columns:
            filled = int(df[name].notna().sum())
            lines.append(f"  {label:<24} {filled:>6}  ({filled/len(df):.1%})")

    # gNATSGO-only fields, shown only when the offline backend was used
    for name, label in (("flood_freq_dcd", "soil flood frequency"),
                        ("ponding_freq_pct_class", "soil ponding class"),
                        ("wtdepaprjunmin_cm", "Apr-Jun water table")):
        if name in df.columns:
            filled = int(df[name].notna().sum())
            lines.append(f"  {label:<24} {filled:>6}  ({filled/len(df):.1%})")

    if "precip_jobday_mm" in df.columns:
        filled = int(pd.to_numeric(df["precip_jobday_mm"], errors="coerce").notna().sum())
        lines.append(f"  dated precip (job day)   {filled:>6}  ({filled/len(df):.1%})")

    lines.append(
        "\nflood_risk_ord: 0 none/unmapped, 1 shaded-X (0.2%), 2 zone D, "
        "3 other mapped,\n4 A-zone SFHA (1%), 5 V-zone coastal high hazard. "
        "Values are point proxies at\nthe matched place centroid, not areal "
        "averages over the rate center."
    )
    if "flood_freq_dcd" in df.columns:
        lines.append(
            "flood_freq_dcd / ponding are gNATSGO soil-survey classes: they name "
            "how often\nthe soil itself floods or ponds, independent of any given "
            "storm -- a static\ncomplement to the dated precipitation columns."
        )
    return "\n".join(lines)
