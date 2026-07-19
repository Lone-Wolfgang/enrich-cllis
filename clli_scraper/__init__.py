"""Scrape rate center and state data for CLLI codes from telcodata.us."""

__version__ = "0.1.0"

from .acs import PRESETS, VARIABLES, attach_acs, summarize_acs
from .enrich import download_gazetteer, enrich, expand_abbrev
from .scraper import parse_html, postprocess, resolve_rate_center, scrape

__all__ = [
    "parse_html", "postprocess", "resolve_rate_center", "scrape",
    "enrich", "download_gazetteer", "expand_abbrev",
    "attach_acs", "summarize_acs", "VARIABLES", "PRESETS",
]
