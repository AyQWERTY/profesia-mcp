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

import re
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
class OfferDetail:
    """Full detail-page data for a single job offer."""

    title: str
    employer: str
    location: str
    employment_type: str | None = None
    salary: str | None = None
    posted: str | None = None
    badges: list[str] = field(default_factory=list)
    # Rendered as whole blocks, not per-field data: unknown offer-specific sections must survive.
    description: str = ""
    company: str = ""

    def to_markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        meta = [
            f"- **Employer:** {self.employer}",
            f"- **Location:** {self.location}",
        ]
        if self.employment_type:
            meta.append(f"- **Employment type:** {self.employment_type}")
        if self.salary:
            meta.append(f"- **Salary:** {self.salary}")
        if self.posted:
            meta.append(f"- **Published:** {self.posted}")
        if self.badges:
            meta.append(f"- **Badges:** {'; '.join(self.badges)}")
        lines += meta + [""]
        if self.description:
            lines += ["## Description", "", self.description, ""]
        if self.company:
            lines += ["## Company", "", self.company, ""]
        return "\n".join(lines).rstrip()


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


BLOCK_TAGS = {"div", "p", "ul", "ol", "table"}


def _render_children(node: Any, out: list[str]) -> None:
    """Flatten one element subtree into readable text (in place into ``out``).

    h3/h4 become bold labels, ``li`` items become bullets, ``br`` becomes a
    line break, and every block-level element ends with a newline; anything
    else is recursed, so offer-specific sections we don't know about are kept
    intact.
    """
    child = node.first_child
    while child is not None:
        if child.is_text_node:
            out.append(child.text() or "")
        elif child.tag == "br":
            out.append("\n")
        elif child.tag in ("h3", "h4"):
            out.append(f"\n\n**{(child.text() or '').strip()}**\n")
        elif child.tag == "li":
            out.append(f"- {(child.text() or '').strip()}\n")
        elif child.tag in ("script", "style"):
            pass
        else:
            _render_children(child, out)
            if child.tag in BLOCK_TAGS:
                out.append("\n")
        child = child.next


def _overall_info_value(main: Any, label: str) -> str | None:
    """Read a value from the Slovak meta block ``.overall-info``.

    That block is always rendered in Slovak, so ``label`` (e.g.
    ``"Spoločnosť:"``) is stable even when the offer page is served in
    another language.
    """
    ov = main.css_first(".overall-info")
    if ov is None:
        return None
    for strong in ov.css("strong"):
        if not (strong.text() or "").startswith(label):
            continue
        nxt = strong.next
        while nxt is not None and nxt.is_text_node and not (nxt.text() or "").strip():
            nxt = nxt.next
        if nxt is not None:
            return (nxt.text() or "").strip() or None
    return None


def _render_section(node: Any) -> str:
    """Render one ``details-section`` subtree into clean plain text."""
    out: list[str] = []
    _render_children(node, out)
    text = "".join(out)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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

    def get_offer(self, url: str) -> OfferDetail:
        """Fetch one offer detail page and parse its content.

        ``url`` is the offer URL from :meth:`search`.
        """
        resp = self._session.get(url, timeout=self._timeout)
        if resp.status_code != 200:
            raise ProfesiaError(f"offer page failed (status {resp.status_code})")
        doc = LexborHTMLParser(resp.text)
        main = doc.css_first("main#detail")
        if main is None:
            raise ProfesiaError("offer detail page not found (main#detail missing)")

        # The site serves multiple templates (plain, itemprop-less, and
        # employer custom designs), so each field lists fallbacks below.
        def text_of(*selectors: str) -> str | None:
            for css in selectors:
                node = main.css_first(css)
                if node is not None:
                    text = (node.text() or "").strip()
                    if text:
                        return text
            return None

        badges: list[str] = []
        for lab in doc.css("#content .label-bordered"):
            text = (lab.text() or "").strip()
            if text:
                badges.append(text)

        description_sections: list[Any] = []
        company_sections: list[Any] = []
        # The three ``details-section`` blocks live inside main; the company
        # block is a sibling of the description container, not inside it.
        for sec in main.css(".details-section"):
            if "company-info" in (sec.attributes.get("class") or ""):
                company_sections.append(sec)
            else:
                description_sections.append(sec)

        return OfferDetail(
            title=text_of("h1[itemprop='title']", "h1") or "",
            employer=(
                text_of("h2[itemprop='hiringOrganization']")
                or _overall_info_value(main, "Spoločnosť:")
                or text_of("h2")
            )
            or "",
            location=(
                text_of("span[itemprop='jobLocation']", "span[itemprop='address']")
                or _overall_info_value(main, "lokalita:")
            )
            or "",
            employment_type=text_of("span[itemprop='employmentType']"),
            salary=text_of("span.salary-range"),
            posted=text_of("span[itemprop='datePosted']"),
            badges=badges,
            description="\n\n".join(_render_section(s) for s in description_sections),
            company="\n\n".join(_render_section(s) for s in company_sections),
        )

    # -- parsing helpers --------------------------------------------------
    def _parse_row(self, row: Any) -> Offer | None:
        # Prefer the offer anchor (id="offer{digits}"): a row's first
        # /praca/ link can be the employer's logo link. Rows without an
        # offer link are the "email subscription / search recap" footer,
        # not a real job -- skip them.
        link = row.css_first("a[id^='offer']") or row.css_first("a[href*='/praca/']")
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
