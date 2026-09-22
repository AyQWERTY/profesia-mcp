"""MCP server exposing profesia.sk job search (built on FastMCP).

Tools
-----
* ``search_jobs`` -- free-text search in a city with a km radius, returns a
  markdown table of matching offers.
* ``suggest_location`` -- resolve a city/region name to the concrete
  locations (with numeric ``region_id``) the search endpoint understands.
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from .searcher import DEFAULT_RADIUS_KM, Profesia

mcp = FastMCP(
    name="profesia-mcp",
    version="0.1.0",
    instructions=(
        "Search jobs on profesia.sk (Slovak job board). Usually pass keyword "
        "plus a Slovak city to search_jobs directly; it resolves the city "
        "automatically and reports which location it used. Only call "
        "suggest_location when a place name is ambiguous, then pass the exact "
        "name (or its region_id) back to search_jobs."
    ),
)


def _resolve_region_id(client: Profesia, city: str) -> tuple[str, str]:
    """Resolve a place name to (region_id, resolved_name).

    Prefers an exact municipality match; otherwise returns the top
    suggestion. Raises ValueError if nothing matches.
    """
    suggestions = client.suggest_location(city)
    if not suggestions:
        raise ValueError(f"could not locate any place named {city!r}")

    lowered = city.strip().lower()
    for s in suggestions:
        if s.region_type == "municipality" and s.name.lower() == lowered:
            return s.region_id, s.name

    chosen = suggestions[0]
    return chosen.region_id, chosen.name


@mcp.tool(
    description=(
        "Resolve a place name into the locations profesia.sk understands. "
        "Returns a JSON array of {region_id, name, region_type}. Pass a "
        "place name to search_jobs.city, or a region_id from here to "
        "search_jobs.region_id, to pin the exact location."
    )
)
def suggest_location(city: str) -> str:
    client = Profesia()
    suggestions = client.suggest_location(city)
    payload = [s.asdict() for s in suggestions]
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool(
    description=(
        "Search profesia.sk jobs. Keyword accepts space or comma for OR "
        "matches. Location is optional: pass a Slovak place `city` "
        "(resolved automatically) or a `region_id` from suggest_location "
        "to pin the exact spot; omit both for a site-wide search. "
        "`radius_km` only applies with a location and snaps to "
        "0/10/20/30/60/100 (max 100). Returns a markdown table."
    )
)
def search_jobs(
    keyword: str,
    city: str | None = None,
    region_id: str | None = None,
    radius_km: int = DEFAULT_RADIUS_KM,
    page: int = 1,
) -> str:
    client = Profesia()

    rid, where = region_id, None
    if city is not None:
        # A place name takes precedence over an explicit region_id.
        rid, where = _resolve_region_id(client, city)
    elif rid is not None:
        where = f"region_id {rid}"

    offers = client.search(
        keyword=keyword,
        region_id=rid,
        radius_km=radius_km,
        page=page,
    )

    if where is None:
        head = (
            f"Found {len(offers)} offer(s) for {keyword!r} (site-wide), page {page}.\n"
        )
    else:
        head = (
            f"Found {len(offers)} offer(s) for {keyword!r} near {where}, "
            f"radius={radius_km}km, page {page}.\n"
        )
    return head + Profesia.to_markdown(offers)


def main() -> None:
    # The banner is printed to stdout; over stdio that stream is the JSON-RPC
    # channel, so a banner would corrupt it. Keep it disabled for server runs.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
