"""
scraper/parser.py

Parse car listing HTML files (e.g. saved search results from Cars.com,
CarGurus, AutoTrader, etc.) and extract pricing + mileage data for the
2022 Mazda CX-9.

Usage
-----
    from scraper.parser import parse_listings_from_file

    listings = parse_listings_from_file("data/raw/search_results.html")
    # returns a list of dicts, one per listing
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def load_html_file(path: str | Path) -> BeautifulSoup:
    """Read an HTML file from disk and return a BeautifulSoup object."""
    path = Path(path)
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return BeautifulSoup(fh.read(), "lxml")


def _clean_price(raw: str) -> Optional[float]:
    """Strip currency symbols/commas and return a float, or None."""
    digits = re.sub(r"[^\d.]", "", raw)
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _clean_mileage(raw: str) -> Optional[int]:
    """Parse mileage strings like '27K mi', '27,000 mi', '168K mi'."""
    m = re.search(r'([\d,]+)(K)?\s*mi', raw, re.IGNORECASE)
    if not m:
        return None
    try:
        val = int(m.group(1).replace(',', ''))
        if m.group(2):  # 'K' suffix
            val *= 1_000
        return val
    except ValueError:
        return None


# Known CX-9 trim levels in ascending value order (used by the pricing model).
# Extend if additional trims are encountered in the wild.
TRIM_ORDER = [
    "sport",
    "touring",
    "touring plus",
    "carbon edition",
    "grand touring",
    "signature",
]

# Pattern matching any known trim; also captures free-text for unknown trims.
_TRIM_RE = re.compile(
    r'\b(signature|grand touring|carbon edition|touring plus|touring|sport)\b',
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r'\b(20\d{2})\b')


def _extract_year(text: str) -> Optional[int]:
    """Return the first 4-digit year found in text, or None."""
    m = _YEAR_RE.search(text or "")
    return int(m.group(1)) if m else None


def _extract_trim(text: str) -> Optional[str]:
    """
    Return the canonical lower-case trim name from a listing title or specs
    string, or None if unrecognised.

    Matching is longest-first so "Grand Touring" beats "Touring".
    """
    m = _TRIM_RE.search(text or "")
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------
# Site-specific parsers
# ---------------------------------------------------------------------------

class ListingParser:
    """
    Flexible parser for car listing search-result HTML pages.

    Supports multiple common site layouts via heuristic detection.
    Add a new `_parse_*` method and register it in `_detect_site()` to
    support additional sources.
    """

    MAKE = "mazda"
    MODEL = "cx-9"

    def __init__(self, soup: BeautifulSoup, year: Optional[str] = None):
        """
        Parameters
        ----------
        soup : BeautifulSoup
        year : Optional[str]
            If set (e.g. "2022"), only listings for that model year are kept.
            If None (default), all CX-9 years are returned — useful for
            building a price-vs-mileage model with more training data.
        """
        self.soup = soup
        self.year = year
        self.site = self._detect_site()

    # ------------------------------------------------------------------
    # Site detection
    # ------------------------------------------------------------------

    def _detect_site(self) -> str:
        title = (self.soup.title.string or "").lower() if self.soup.title else ""
        canonical = self.soup.find("link", rel="canonical")
        href = (canonical["href"] if canonical and canonical.get("href") else "").lower()
        combined = title + " " + href

        # og:site_name is the most reliable signal (title may not contain the site name)
        og_site = self.soup.find("meta", attrs={"property": "og:site_name"})
        og_site_name = (og_site["content"] if og_site and og_site.get("content") else "").lower()

        if "cars.com" in combined or "cars.com" in og_site_name:
            return "cars.com"
        if "cargurus" in combined or "cargurus" in og_site_name:
            return "cargurus"
        if "autotrader" in combined or "autotrader" in og_site_name:
            return "autotrader"
        if "carmax" in combined or "carmax" in og_site_name:
            return "carmax"
        # carmax pages sometimes have a generic title; catch by canonical URL host
        if "carmax" in (self.soup.find("link", rel="canonical") or {}).get("href", "").lower():
            return "carmax"
        return "generic"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self) -> list[dict]:
        """Return a list of listing dicts for the target vehicle."""
        dispatch = {
            "cars.com": self._parse_cars_com,
            "cargurus": self._parse_cargurus,
            "autotrader": self._parse_autotrader,
            "carmax": self._parse_carmax,
            "generic": self._parse_generic,
        }
        raw = dispatch[self.site]()
        return [l for l in raw if self._is_target(l)]

    # ------------------------------------------------------------------
    # Site parsers
    # ------------------------------------------------------------------

    def _parse_cars_com(self) -> list[dict]:
        r"""
        Cars.com pages saved as HTML.

        Key selectors (confirmed April 2026):
          <fuse-card data-listing-id="...">  — one web-component card per listing
          <h2>/<h3>/<h4> inside card        — "Used 2023 Mazda CX-9 Touring Plus"
          First r'\$(\d{4,})' in card text  — price (no asterisk)
          First r'(\d+,\d+) mi\.' in text   — exact mileage

        Note: The full text also contains ad markers like "$399/mo" which
        the MIN_PRICE filter in pricing.py will remove; exact mileage uses
        the trailing period pattern ("35,076 mi.") to avoid matching
        dealer distance strings ("13 mi").
        """
        listings = []
        price_re_local   = re.compile(r'\$([\d,]{4,})')
        mileage_re_local = re.compile(r'([\d,]+)\s*mi\.', re.IGNORECASE)

        for card in self.soup.find_all("fuse-card", attrs={"data-listing-id": True}):
            heading = card.find(["h2", "h3", "h4"])
            title = heading.get_text(strip=True) if heading else None

            full_text = card.get_text(" ", strip=True)

            p = price_re_local.search(full_text)
            price = float(p.group(1).replace(",", "")) if p else None

            m = mileage_re_local.search(full_text)
            mileage = int(m.group(1).replace(",", "")) if m else None

            listings.append({
                "title":   title,
                "year":    _extract_year(title or ""),
                "trim":    _extract_trim(title or ""),
                "price":   price,
                "mileage": mileage,
                "source":  "cars.com",
            })
        return listings

    def _parse_cargurus(self) -> list[dict]:
        r"""
        CarGurus pages saved as HTML.

        Key selectors (confirmed April 2026):
          data-cg-ft starts with "srp-listing-blade"  — one card per listing
            (includes both regular and sponsored; filter to cards that actually
             contain a title element to skip empty wrapper divs)
          data-cg-ft="srp-listing-blade-title"  h5[title="YYYY Mazda CX-9"]
          data-cg-ft="vehicle"                   e.g. "Touring Plus AWD"
          data-cg-ft="srp-listing-blade-price"   e.g. "$27,865"
          mileage lives as a plain text node matching r"[\d,]+ mi"
        """
        listings = []
        mileage_re = re.compile(r'[\d,]+\s*mi', re.IGNORECASE)

        for card in self.soup.find_all(attrs={"data-cg-ft": re.compile(r'^srp-listing-blade')}):
            title_el = card.find(attrs={"data-cg-ft": "srp-listing-blade-title"})
            if not title_el:
                continue  # skip empty wrapper divs

            price_el   = card.find(attrs={"data-cg-ft": "srp-listing-blade-price"})
            vehicle_el = card.find(attrs={"data-cg-ft": "vehicle"})

            # Title is the h5 title= attribute (e.g. "2023 Mazda CX-9")
            title = title_el.get("title") or title_el.get_text(strip=True)

            # Trim is the vehicle element text, strip AWD/FWD drivetrain suffix
            trim_raw = vehicle_el.get_text(strip=True) if vehicle_el else ""
            trim_raw = re.sub(r'\s+(AWD|FWD|4WD|RWD|4x4)\b.*', '', trim_raw, flags=re.IGNORECASE)
            trim = _extract_trim(trim_raw) or _extract_trim(title)

            mi_text = next((s for s in card.strings if mileage_re.search(s)), None)

            listings.append({
                "title":   title,
                "year":    _extract_year(title),
                "trim":    trim,
                "price":   _clean_price(price_el.get_text(strip=True) if price_el else ""),
                "mileage": _clean_mileage(mi_text or ""),
                "source":  "cargurus",
            })
        return listings

    def _parse_autotrader(self) -> list[dict]:
        r"""
        AutoTrader pages saved as "Web Page, Complete" carry all listing data
        in the static HTML via data-cmp attributes.  Use the .complete.html
        variant for best results.

        Key selectors (confirmed against saved search results, April 2026):
          data-cmp="itemCard"              — one per listing card
          data-cmp="listing-title-info"    — contains <a title="Used 20XX …">
          data-cmp="firstPrice"            — text like "28,888" (no $ sign)
          data-cmp="listingSpecifications" — contains "<li>27K mi</li>"
        """
        listings = []
        for card in self.soup.find_all(attrs={"data-cmp": "itemCard"}):
            # Title: prefer the <a title="…"> attribute for a clean string
            title = None
            title_el = card.find(attrs={"data-cmp": "listing-title-info"})
            if title_el:
                a = title_el.find("a", title=True)
                title = a["title"] if a else title_el.get_text(strip=True)

            price_el = card.find(attrs={"data-cmp": "firstPrice"})
            specs_el = card.find(attrs={"data-cmp": "listingSpecifications"})

            # Trim is the first <li> in specs (e.g. "Touring Plus"); fall back
            # to extracting it from the title string.
            trim: Optional[str] = None
            if specs_el:
                first_li = specs_el.select_one("li span.text-overflow, li")
                if first_li:
                    trim = _extract_trim(first_li.get_text(strip=True))
            if trim is None:
                trim = _extract_trim(title or "")

            listings.append({
                "title": title,
                "year": _extract_year(title or ""),
                "trim": trim,
                "price": _clean_price(price_el.get_text(strip=True) if price_el else ""),
                "mileage": _clean_mileage(specs_el.get_text() if specs_el else ""),
                "source": "autotrader",
            })
        return listings

    def _parse_carmax(self) -> list[dict]:
        """
        CarMax pages saved as HTML.

        Key selectors (confirmed April 2026):
          div.kmx-car-tile          — top-level card (MuiCard root)
          div.kmx-car-tile__content h3/h2/a  — "2023 Mazda CX-9 Touring Plus"
          div.kmx-car-tile__price-and-mileage — "$28,998* 33K mi"
            Price has a trailing * (no-haggle price indicator); mileage uses K suffix.
        """
        listings = []
        price_re_local   = re.compile(r'\$([\d,]+)\*?')
        mileage_re_local = re.compile(r'([\d,]+K?)\s*mi', re.IGNORECASE)

        for card in self.soup.select("div.kmx-car-tile"):
            title_el = card.select_one(
                "div.kmx-car-tile__content h3, "
                "div.kmx-car-tile__content h2, "
                "div.kmx-car-tile__content a"
            )
            pm_el = card.select_one("div.kmx-car-tile__price-and-mileage")

            title = title_el.get_text(strip=True) if title_el else None

            price   = None
            mileage = None
            if pm_el:
                pm_text = pm_el.get_text(" ", strip=True)
                m = price_re_local.search(pm_text)
                if m:
                    price = float(m.group(1).replace(",", ""))
                m = mileage_re_local.search(pm_text)
                if m:
                    raw = m.group(1).replace(",", "")
                    mileage = int(raw[:-1]) * 1_000 if raw.upper().endswith("K") else int(raw)

            listings.append({
                "title":   title,
                "year":    _extract_year(title),
                "trim":    _extract_trim(title or ""),
                "price":   price,
                "mileage": mileage,
                "source":  "carmax",
            })
        return listings

    def _parse_generic(self) -> list[dict]:
        """
        Last-resort heuristic: look for price-like and mileage-like text
        anywhere in the document, grouped by nearest common ancestor.
        """
        listings = []
        price_re = re.compile(r"\$[\d,]{4,}")
        mileage_re = re.compile(r"[\d,]+\s*mi", re.IGNORECASE)

        price_els = self.soup.find_all(string=price_re)
        for p_el in price_els:
            ancestor = p_el.parent
            mi_el = ancestor.find(string=mileage_re)
            if mi_el:
                title_el = ancestor.find(re.compile(r"^h[1-4]$"))
                title = title_el.get_text(strip=True) if title_el else None
                listings.append({
                    "title": title,
                    "year": _extract_year(title),
                    "trim": _extract_trim(title),
                    "price": _clean_price(p_el),
                    "mileage": _clean_mileage(mi_el),
                    "source": "generic",
                })
        return listings

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------

    def _is_target(self, listing: dict) -> bool:
        title = (listing.get("title") or "").lower()
        # Prefer the extracted year field; fall back to title string search
        listing_year = listing.get("year")
        if self.year is not None:
            year_ok = str(listing_year) == self.year if listing_year else (self.year in title)
        else:
            year_ok = True
        # Use word boundary so "cx-9" doesn't match "cx-90"
        model_ok = bool(re.search(r'\bcx-9\b', title))
        return year_ok and self.MAKE in title and model_ok


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def parse_listings_from_file(path: str | Path, year: Optional[str] = None) -> list[dict]:
    """Load an HTML file and return a list of matching listing dicts.

    Parameters
    ----------
    path : str | Path
        Path to the saved HTML file.
    year : Optional[str]
        If set (e.g. "2022"), only listings for that model year are returned.
        Pass None to include all CX-9 years (better for model training).
    """
    soup = load_html_file(path)
    return ListingParser(soup, year=year).parse()
