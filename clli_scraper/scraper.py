"""
Scrape telcodata.us CLLI search results.

The search form submits via GET to:
    https://www.telcodata.us/search-area-code-exchange-by-clli?cllicode=XXXXXX
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

URL = "https://www.telcodata.us/search-area-code-exchange-by-clli"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": URL,
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

RAW_COLUMNS = [
    "npa_nxx", "state", "company", "frn", "rmd",
    "ocn", "rate_center", "clli", "assign_date", "misc",
]

FINAL_COLUMNS = [
    "clli_query", "clli", "state", "rate_center",
    "rate_center_fd", "resolution_method", "confidence", "n_prefixes",
]

# rate centers the site uses as placeholders, not real locations
PLACEHOLDER_RC = {"XXXXXXXXXX", "", "N/A", "NA"}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class RateLimited(Exception):
    """Server signalled that we are going too fast."""


def make_session(total_retries: int = 5, backoff: float = 2.0) -> requests.Session:
    """Session with urllib3-level retry on transient/rate-limit statuses.

    Backoff is exponential: 2s, 4s, 8s, 16s, 32s. Retry-After is honoured
    when the server sends it.
    """
    s = requests.Session()
    s.headers.update(HEADERS)

    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch(clli: str, session: requests.Session, timeout: int = 90) -> str:
    r = session.get(URL, params={"cllicode": clli}, timeout=timeout)

    if r.status_code == 429:
        raise RateLimited(f"HTTP 429 for {clli}")
    if r.status_code in (403, 503):
        raise RateLimited(f"HTTP {r.status_code} for {clli} (possible block)")

    r.raise_for_status()
    return r.text


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def cell_text(td) -> str:
    """Flatten a <td>, turning <br> into real newlines so Misc stays parseable."""
    for br in td.find_all("br"):
        br.replace_with("\n")
    lines = [ln.strip() for ln in td.get_text().split("\n")]
    return "\n".join(ln for ln in lines if ln)


def parse_html(html: str, clli: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="resultstable")
    if table is None:
        return pd.DataFrame()

    rows = []
    for tr in table.find_all("tr"):
        if "results" not in (tr.get("class") or []):
            continue

        tds = tr.find_all("td")
        if len(tds) < len(RAW_COLUMNS):
            continue

        rec = {col: cell_text(td) for col, td in zip(RAW_COLUMNS, tds)}

        link = tds[7].find("a")
        if link:
            rec["clli"] = link.get_text(strip=True)

        rows.append(rec)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    misc = df["misc"].astype(str)
    df["prefix_type"] = misc.str.extract(r"Prefix Type:\s*([^\n]*)")
    df["switch_name"] = misc.str.extract(r"Switch Name:\s*([^\n]*)")
    df["switch_type"] = misc.str.extract(r"Switch Type:\s*([^\n]*)")
    df["lata"] = misc.str.extract(r"LATA:\s*([^\n]*)")
    df["tandem"] = misc.str.extract(r"Tandem:\s*([^\n]*)")

    df.insert(0, "clli_query", clli)
    return df.drop(columns=["misc"]).reset_index(drop=True)


# --------------------------------------------------------------------------
# Resolution / post-processing
# --------------------------------------------------------------------------

def npa_sort_key(v):
    """Sort NPA-NXX strings numerically: '214-200' before '214-200-0XXX'."""
    parts = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in parts) if parts else (9999,)


def resolve_rate_center(grp: pd.DataFrame, clli: str):
    """Pick the true rate center for one CLLI.

    Precedence:
      1. state_filtered  -- restrict to rows whose state matches clli[4:6],
                            authoritative by CLLI definition, then vote
      2. majority        -- plain vote (when the state filter empties the set)
      3. place_prefix    -- tie broken by matching clli[0:4] against the name
      4. lowest_npa      -- last resort: row with the smallest NPA-NXX
    """
    clli_state = clli[4:6].upper() if len(clli) >= 6 else None
    place = clli[:4].upper() if len(clli) >= 4 else ""

    pool, method = grp, "majority"
    if clli_state:
        in_state = grp[grp["state"].str.upper() == clli_state]
        if not in_state.empty:
            pool, method = in_state, "state_filtered"

    counts = pool["rate_center"].value_counts()
    if counts.empty:
        return None, {}, "empty", 0.0, 0

    top = counts.max()
    tied = list(counts[counts == top].index)

    if len(tied) == 1:
        winner = tied[0]
    else:
        pref = [rc for rc in tied if rc.upper().startswith(place[:3])]
        if len(pref) == 1:
            winner, method = pref[0], "place_prefix"
        else:
            cands = pref if pref else tied
            sub = pool[pool["rate_center"].isin(cands)]
            if "npa_nxx" in sub.columns and not sub.empty:
                winner = sub.loc[sub["npa_nxx"].map(npa_sort_key).idxmin(), "rate_center"]
                method = "lowest_npa"
            else:
                winner, method = sorted(cands)[0], "alphabetical"

    n = int(counts.sum())
    return winner, counts.to_dict(), method, round(top / n, 3), n


def postprocess(df: pd.DataFrame, drop_placeholders: bool = True) -> pd.DataFrame:
    """Collapse raw prefix rows to one row per CLLI."""
    if df.empty:
        return pd.DataFrame(columns=FINAL_COLUMNS)

    work = df.copy()
    work["rate_center"] = work["rate_center"].astype(str).str.strip()
    work["state"] = work["state"].astype(str).str.strip()

    if drop_placeholders:
        work = work[~work["rate_center"].str.upper().isin(PLACEHOLDER_RC)]

    rows = []
    for (clli_query, clli), grp in work.groupby(["clli_query", "clli"], sort=True):
        winner, fd, method, conf, n = resolve_rate_center(grp, clli)
        if winner is None:
            continue

        clli_state = clli[4:6].upper() if len(clli) >= 6 else None
        observed = sorted(grp["state"].str.upper().unique())
        if len(observed) > 1:
            log.debug("%s: states %s -> using %s from CLLI", clli, observed, clli_state)

        rows.append({
            "clli_query": clli_query,
            "clli": clli,
            "state": clli_state,
            "rate_center": winner,
            "rate_center_fd": json.dumps(fd, ensure_ascii=False),
            "resolution_method": method,
            "confidence": conf,
            "n_prefixes": n,
        })

    return pd.DataFrame(rows, columns=FINAL_COLUMNS)


# --------------------------------------------------------------------------
# Resume support
# --------------------------------------------------------------------------

def read_targets(path: Path) -> list[str]:
    """Newline-separated CLLI list; blanks and #-comments ignored, deduped."""
    seen, out = set(), []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        c = line.strip().upper()
        if not c or c.startswith("#"):
            continue
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def completed_queries(out_path: Path) -> set[str]:
    """CLLI queries already present in the output file."""
    p = Path(out_path)
    if not p.exists() or p.stat().st_size == 0:
        return set()
    try:
        df = pd.read_csv(p, usecols=["clli_query"], dtype=str)
        return set(df["clli_query"].dropna().str.upper())
    except Exception as e:
        log.warning("Could not read %s for resume (%s); starting fresh.", p, e)
        return set()


def append_rows(df: pd.DataFrame, out_path: Path) -> None:
    """Append to CSV, writing the header only on first write. Flushed per call
    so a kill -9 loses at most the current query."""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = not p.exists() or p.stat().st_size == 0
    df.to_csv(p, mode="a", header=header, index=False)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def scrape(
    targets: list[str],
    out_path: Path,
    raw_path: Path | None = None,
    delay: float = 2.0,
    jitter: float = 1.0,
    cooldown: float = 60.0,
    max_cooldowns: int = 5,
    progress: bool = True,
):
    """Scrape each CLLI, appending results incrementally.

    On a rate-limit signal, sleeps `cooldown` (doubling each time) and retries
    the same CLLI. Aborts after `max_cooldowns` consecutive failures so an
    unattended run does not hammer a blocked endpoint.
    """
    from tqdm import tqdm

    session = make_session()
    bar = tqdm(targets, unit="clli", disable=not progress, dynamic_ncols=True)

    ok = failed = 0
    consecutive = 0

    for clli in bar:
        bar.set_postfix_str(f"{clli} ok={ok} fail={failed}")

        wait = cooldown
        for attempt in range(max_cooldowns):
            try:
                html = fetch(clli, session)
                break
            except RateLimited as e:
                log.warning("%s -- backing off %.0fs (attempt %d/%d)",
                            e, wait, attempt + 1, max_cooldowns)
                time.sleep(wait)
                wait *= 2
            except requests.RequestException as e:
                log.error("[%s] request failed: %s", clli, e)
                html = None
                break
        else:
            log.error("[%s] giving up after %d cooldowns", clli, max_cooldowns)
            html = None

        if html is None:
            failed += 1
            consecutive += 1
            if consecutive >= max_cooldowns:
                log.error("Aborting: %d consecutive failures. "
                          "Re-run later; completed work is saved.", consecutive)
                break
            continue

        consecutive = 0
        raw = parse_html(html, clli)

        if raw.empty:
            log.info("[%s] no results", clli)
            # Record the miss so resume does not retry it forever.
            append_rows(
                pd.DataFrame([{**{c: None for c in FINAL_COLUMNS},
                               "clli_query": clli,
                               "resolution_method": "no_results"}]),
                out_path,
            )
            ok += 1
        else:
            if raw_path:
                append_rows(raw, raw_path)
            final = postprocess(raw)
            append_rows(final, out_path)
            log.info("[%s] %d prefixes -> %d CLLIs", clli, len(raw), len(final))
            ok += 1

        time.sleep(delay + random.uniform(0, jitter))

    bar.close()
    return ok, failed
