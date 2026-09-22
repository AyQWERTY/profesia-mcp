"""Scraping client for https://www.profesia.sk/ (Slovak job board).

Design notes
------------
The site renders everything server-side. There is no public JSON API for
listings and no JSON-LD; the only reliable transport is scraping the
server-rendered HTML of the search-results page.

Search model (verified against the live site)
---------------------------------------------
  * Basic search is served by ``GET /praca/``.
  * Location is selected with ``region_id`` (a numeric id) -- the server
    resolves it internally to the region slug, so we never slugify names.
  * Free-text matching uses the scalar ``search_anywhere`` param
    (space/comma = OR). It must NOT be sent as an array (that 500s).
  * Radius is one of the fixed tokens ``radius0/10/20/30/60/100``.
  * Each row is ``li.list-row`` with ``.title`` / ``.employer`` /
    ``.job-location``. Label chips live in ``.label-group``: the
    ``icon money`` badge is the salary, every other badge is an
    ``extra`` tag. The ``.list-footer-right .info strong`` text is the
    relative posting time (e.g. "Pred 2 týždňami").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi import requests
from selectolax.lexbor import LexborHTMLParser

BASE_URL = "https://www.profesia.sk"

# The fixed radius buckets the site understands (km).
AVAILABLE_RADIUS_KM = [0, 10, 20, 30, 60, 100]
DEFAULT_RADIUS_KM = 30


@dataclass
class Offer:
    title: str
    employer: str
    location: str
    salary: str | None = None
    url: str | None = None
    # Relative posting time from the search results, e.g. "Pred 2 týždňami".
    posted: str | None = None
    extra: list[str] = field(default_factory=list)


@dataclass
class LocationSuggestion:
    region_id: str
    name: str
    region_type: str

    def asdict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "name": self.name,
            "region_type": self.region_type,
        }


def snap_radius(km: int | None) -> str:
    """Map a caller-friendly km value to the closest radius token."""
    if km is None:
        km = DEFAULT_RADIUS_KM
    return "radius" + str(min(AVAILABLE_RADIUS_KM, key=lambda r: abs(r - km)))


def _strip_search_id(url: str) -> str:
    """Drop the ``search_id`` tracking param profesia.sk appends to links."""
    scheme, netloc, path, query, fragment = urlsplit(url)
    clean = [
        (k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "search_id"
    ]
    return urlunsplit((scheme, netloc, path, urlencode(clean), fragment))


class ProfesiaError(RuntimeError):
    """Raised when the site cannot be reached or a request fails."""


class Profesia:
    """Thin, cache-friendly client over the profesia.sk search endpoints."""

    def __init__(self, timeout: int = 30, impersonate: str = "chrome") -> None:
        self._timeout = timeout
        self._impersonate = impersonate
        self._session = requests.Session(impersonate=impersonate)
        self._token: str | None = None
        self._token_expires: datetime | None = None

    # -- token management -------------------------------------------------
    def _ensure_token(self) -> None:
        # Token is a per-session anti-bot nonce. Keep it for ~25 min and
        # refetch on the first auth failure afterwards.
        if (
            self._token
            and self._token_expires
            and datetime.now(UTC) < self._token_expires
        ):
            return
        resp = self._session.get(
            f"{BASE_URL}/praca/", timeout=self._timeout, impersonate=self._impersonate
        )
        if resp.status_code != 200:
            raise ProfesiaError(f"could not load home page (status {resp.status_code})")
        doc = LexborHTMLParser(resp.text)
        token_node = doc.css_first("[region-api-token]")
        token = (
            token_node.attributes.get("region-api-token")
            if token_node is not None
            else None
        )
        if not token:
            raise ProfesiaError("region-api-token not found on home page")
        self._token = token
        self._token_expires = datetime.now(UTC) + timedelta(minutes=25)

    def _suggest_url(self, query: str) -> str:
        self._ensure_token()
        return f"{BASE_URL}/sk/regions/suggest?q={query}&limit=10&token={self._token}"

    # -- public API -------------------------------------------------------
    def suggest_location(self, query: str) -> list[LocationSuggestion]:
        """Resolve a city/region name to possible locations.

        Requires a session token; returns suggestions with the numeric
        ``region_id`` we feed straight into :meth:`search`.
        """
        if not query.strip():
            return []
        resp = self._session.get(self._suggest_url(query), timeout=self._timeout)
        if resp.status_code == 401:
            # Token expired/rotated -- refresh once and retry.
            self._token, self._token_expires = None, None
            resp = self._session.get(self._suggest_url(query), timeout=self._timeout)
        if resp.status_code != 200:
            raise ProfesiaError(f"suggest endpoint failed (status {resp.status_code})")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProfesiaError("suggest response was not valid JSON") from exc
        results: list[LocationSuggestion] = []
        for item in payload.get("data", []):
            rid = item.get("region_id")
            if rid is None:
                continue
            results.append(
                LocationSuggestion(
                    region_id=str(rid),
                    name=item.get("region_name", ""),
                    region_type=item.get("region_type", ""),
                )
            )
        return results

    def search(
        self,
        keyword: str,
        region_id: str | None = None,
        radius_km: int | None = None,
        sort_by: str = "relevance",
        page: int = 1,
    ) -> list[Offer]:
        """Search jobs, optionally scoped to one location.

        Pass a ``region_id`` (from :meth:`suggest_location`) to scope the
        search; omit it for a site-wide search. ``radius_km`` only applies
        with a location. Returns the offers on the requested page (the site
        fixes the per-page count).
        """
        params = {
            "search_anywhere": keyword,
            "sort_by": sort_by,
            "page_num": page,
        }
        if region_id is not None:
            params["region_id"] = region_id
            params["radius"] = snap_radius(radius_km)
        resp = self._session.get(
            f"{BASE_URL}/praca/", params=params, timeout=self._timeout
        )
        if resp.status_code != 200:
            raise ProfesiaError(f"search failed (status {resp.status_code})")
        doc = LexborHTMLParser(resp.text)
        offers = []
        for row in doc.css("li.list-row"):
            offer = self._parse_row(row)
            if offer is not None:
                offers.append(offer)
        return offers

    # -- parsing helpers --------------------------------------------------
    def _parse_row(self, row: Any) -> Offer | None:
        # Rows without an offer link are the "email subscription / search
        # recap" footer, not a real job -- skip them.
        link = row.css_first("a[href*='/praca/']")
        if link is None:
            return None
        href = link.attributes.get("href")
        url = _strip_search_id(f"{BASE_URL}{href}") if href else None

        title_node = row.css_first(".title")
        employer_node = row.css_first(".employer")
        location_node = row.css_first(".job-location")

        # Only the money badge is a salary; every other badge
        # (accommodation, languages, perks, ...) is a generic extra tag.
        salary: str | None = None
        extra: list[str] = []
        for lab in row.css(".label"):
            svg = lab.css_first("svg.icon")
            is_salary = svg is not None and any(
                tok == "money" for tok in svg.attributes.get("class", "").split()
            )
            text = (lab.text() or "").strip()
            if is_salary:
                salary = text
            elif text:
                extra.append(text)

        posted_node = row.css_first(".list-footer-right .info strong")
        posted = posted_node.text() if posted_node is not None else None

        return Offer(
            title=title_node.text() if title_node is not None else "",
            employer=employer_node.text() if employer_node is not None else "",
            location=location_node.text() if location_node is not None else "",
            salary=salary,
            url=url,
            posted=posted,
            extra=extra,
        )

    @staticmethod
    def to_markdown(jobs: list[Offer]) -> str:
        if not jobs:
            return "No matching jobs found."
        header = "| Title | Employer | Location | Salary | URL | Posted | Extra |"
        sep = "| --- | --- | --- | --- | --- | --- | --- |"
        lines = [header, sep]
        for o in jobs:
            lines.append(
                f"| {o.title} | {o.employer} | {o.location} "
                f"| {o.salary or ''} | {o.url or ''} "
                f"| {o.posted or ''} | {'; '.join(o.extra)} |"
            )
        return "\n".join(lines)
