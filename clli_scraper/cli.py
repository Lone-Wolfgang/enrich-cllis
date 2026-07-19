"""Command-line interface for the telcodata CLLI scraper."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from .scraper import (
    FINAL_COLUMNS,
    completed_queries,
    postprocess,
    read_targets,
    scrape,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clli-scrape",
        description="Scrape rate center / state for CLLI codes from telcodata.us.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  clli-scrape -i clli_list.txt -o results.csv\n"
            "  clli-scrape -i clli_list.txt -o results.csv --raw raw.csv --delay 3\n"
            "  clli-scrape -i clli_list.txt -o results.csv --restart\n"
            "\n"
            "Resume is automatic: any clli_query already in the output file is\n"
            "skipped. Interrupt with Ctrl-C and re-run the same command.\n"
        ),
    )
    p.add_argument("-i", "--input", type=Path, default=None,
                   help="newline-separated file of CLLI codes (# comments ok); "
                        "not needed with --enrich alone")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="output CSV; appended to and used for resume")
    p.add_argument("--raw", type=Path, default=None,
                   help="optional CSV for the unaggregated per-prefix rows")

    p.add_argument("--delay", type=float, default=2.0,
                   help="base seconds between requests (default: 2.0)")
    p.add_argument("--jitter", type=float, default=1.0,
                   help="random 0..N seconds added to each delay (default: 1.0)")
    p.add_argument("--cooldown", type=float, default=60.0,
                   help="initial seconds to sleep on rate limit, doubling (default: 60)")
    p.add_argument("--max-cooldowns", type=int, default=5,
                   help="give up on a CLLI after N backoffs (default: 5)")

    p.add_argument("--restart", action="store_true",
                   help="ignore and overwrite existing output instead of resuming")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be scraped, then exit")
    p.add_argument("--no-progress", action="store_true", help="disable progress bar")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--reprocess", action="store_true",
                   help="rebuild output from --raw without any network calls")

    g = p.add_argument_group("enrichment (lat/lon + population density)")
    g.add_argument("--enrich", action="store_true",
                   help="geocode rate centers against the Census Gazetteer; "
                        "with no -i/--input, enriches an existing --output in place")
    g.add_argument("--enrich-out", type=Path, default=None,
                   help="write enriched CSV here (default: <output>_enriched.csv)")
    g.add_argument("--gazetteer-year", type=int, default=2025,
                   help="Census Gazetteer vintage; older years are tried "
                        "automatically if unavailable (default: 2025)")
    g.add_argument("--gazetteer-cache", type=Path, default=None,
                   help="cache path for the downloaded Gazetteer")
    g.add_argument("--population", type=Path, default=None,
                   help="CSV with GEOID + population columns, for density")
    g.add_argument("--min-score", type=float, default=85.0,
                   help="fuzzy match cutoff 0-100; below this is left unmatched "
                        "(default: 85)")
    g.add_argument("--county-geoids", action="store_true",
                   help="also assign nearest county GEOID (enables ACS county "
                        "fallback)")

    a = p.add_argument_group("ACS demographics (requires --enrich)")
    a.add_argument("--acs", nargs="*", metavar="VAR",
                   help="attach ACS variables. Presets: core, telecom, housing, "
                        "demographics, all. Bare --acs uses 'core'.")
    a.add_argument("--acs-year", type=int, default=2023,
                   help="ACS 5-year vintage (default: 2023)")
    a.add_argument("--census-key", default=None,
                   help="Census API key (or set CENSUS_API_KEY)")
    a.add_argument("--acs-cache", type=Path, default=None,
                   help="directory for cached ACS responses (default: acs_cache)")
    a.add_argument("--no-county-fallback", action="store_true",
                   help="do not substitute county data where place data is missing")
    a.add_argument("--list-acs-vars", action="store_true",
                   help="print available ACS variables and presets, then exit")
    return p


def run_enrichment(args, log) -> int:
    from .enrich import (assign_counties, assign_state_geoids,
                         download_county_gazetteer, download_gazetteer,
                         enrich, summarize)

    src = args.output
    if not src.exists():
        log.error("Nothing to enrich: %s not found", src)
        return 2

    df = pd.read_csv(src, dtype=str)
    if "rate_center" not in df.columns:
        log.error("%s has no rate_center column", src)
        return 2

    gaz = download_gazetteer(args.gazetteer_year, args.gazetteer_cache)

    pop = None
    if args.population:
        if args.population.exists():
            pop = pd.read_csv(args.population, dtype=str)
            log.info("Loaded %d population row(s)", len(pop))
        else:
            log.warning("Population file not found: %s", args.population)

    enriched = enrich(df, gaz, min_score=args.min_score, population=pop)

    need_counties = args.county_geoids or (
        args.acs is not None and not args.no_county_fallback
    )
    if need_counties:
        try:
            cgaz = download_county_gazetteer(args.gazetteer_year)
            enriched = assign_counties(enriched, cgaz)
            log.info("Assigned county GEOIDs to %d row(s)",
                     int(enriched["county_geoid"].notna().sum()))
        except Exception as exc:
            log.warning("County assignment failed (%s); ACS county fallback "
                        "will be unavailable", exc)
        enriched = assign_state_geoids(enriched)

    print(summarize(enriched))

    if args.acs is not None:
        from .acs import attach_acs, resolve_variables, summarize_acs
        try:
            variables = resolve_variables(args.acs or None)
            enriched = attach_acs(
                enriched,
                variables=variables,
                year=args.acs_year,
                api_key=args.census_key,
                cache_dir=args.acs_cache,
                county_fallback=not args.no_county_fallback,
            )
            print()
            print(summarize_acs(enriched, variables))
        except (ValueError, RuntimeError) as exc:
            log.error("ACS enrichment failed: %s", exc)
            return 2

    dest = args.enrich_out or src.with_name(src.stem + "_enriched.csv")
    enriched.to_csv(dest, index=False)
    log.info("Enriched -> %s", dest)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    log = logging.getLogger("clli")

    if args.list_acs_vars:
        from .acs import PRESETS, VARIABLES
        print("presets:")
        for k, v in PRESETS.items():
            print(f"  {k:<14} {len(v)} vars")
        print("\nvariables:")
        for k, (var, subj) in VARIABLES.items():
            print(f"  {k:<26} {var:<16} {'subject' if subj else 'detailed'}")
        return 0

    if args.output is None:
        log.error("-o/--output is required")
        return 2

    if args.acs is not None and not args.enrich:
        log.error("--acs requires --enrich (ACS joins on the GEOID it produces)")
        return 2

    # --- reprocess mode: no network -------------------------------------
    if args.reprocess:
        if not args.raw or not args.raw.exists():
            log.error("--reprocess requires an existing --raw file")
            return 2
        raw = pd.read_csv(args.raw, dtype=str)
        final = postprocess(raw)
        final.to_csv(args.output, index=False)
        log.info("Reprocessed %d raw rows -> %d CLLIs -> %s",
                 len(raw), len(final), args.output)
        return run_enrichment(args, log) if args.enrich else 0

    # --- enrich-only: no scraping ---------------------------------------
    if args.enrich and args.input is None:
        return run_enrichment(args, log)

    if args.input is None:
        log.error("-i/--input is required unless using --enrich or --reprocess alone")
        return 2

    # --- targets --------------------------------------------------------
    if not args.input.exists():
        log.error("Input file not found: %s", args.input)
        return 2

    targets = read_targets(args.input)
    if not targets:
        log.error("No CLLI codes found in %s", args.input)
        return 2

    if args.restart:
        for p in (args.output, args.raw):
            if p and p.exists():
                p.unlink()
                log.info("Removed %s", p)
        done = set()
    else:
        done = completed_queries(args.output)

    pending = [c for c in targets if c.upper() not in done]

    log.info("%d target(s), %d already done, %d pending",
             len(targets), len(targets) - len(pending), len(pending))

    if not pending:
        log.info("Nothing to do. Use --restart to force a full re-run.")
        return 0

    if args.dry_run:
        for c in pending[:20]:
            print(c)
        if len(pending) > 20:
            print(f"... and {len(pending) - 20} more")
        return 0

    # --- run ------------------------------------------------------------
    try:
        ok, failed = scrape(
            pending,
            out_path=args.output,
            raw_path=args.raw,
            delay=args.delay,
            jitter=args.jitter,
            cooldown=args.cooldown,
            max_cooldowns=args.max_cooldowns,
            progress=not args.no_progress,
        )
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress saved to %s -- re-run to resume.",
                    args.output)
        return 130

    log.info("Done: %d ok, %d failed -> %s", ok, failed, args.output)

    if args.output.exists():
        df = pd.read_csv(args.output, dtype=str)
        log.info("Output now holds %d CLLI row(s)", len(df))
        if "confidence" in df.columns:
            conf = pd.to_numeric(df["confidence"], errors="coerce")
            low = int((conf < 0.5).sum())
            if low:
                log.info("%d row(s) resolved with confidence < 0.5 "
                         "-- inspect rate_center_fd for these", low)

    if args.enrich:
        rc = run_enrichment(args, log)
        if rc:
            return rc

    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
