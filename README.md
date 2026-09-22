# profesia-mcp

MCP server exposing job search on [profesia.sk](https://www.profesia.sk/) (Slovak job board), built with [FastMCP](https://gofastmcp.com).

## Tools

<details>
<summary><code>search_jobs</code></summary>

Free-text job search by keyword, with an optional location and radius. Returns a markdown table.

**Inputs**

- `keyword` (string, required): free-text term. Space or comma = OR (e.g. `programátor, kurier`).
- `city` (string, optional): Slovak place name, resolved automatically (e.g. `Bratislava`, `Košice`). Omit for a site-wide search. When a name is ambiguous, call `suggest_location` first and pass the exact name here.
- `region_id` (string, optional): pin the exact location instead of resolving by name -- use an id from `suggest_location`. If both `city` and `region_id` are given, `city` wins.
- `radius_km` (integer, optional, default `30`): applies only with a location; snapped to the closest of `0/10/20/30/60/100` (max 100).
- `page` (integer, optional, default `1`): result page number.

**Output**

A markdown table, one row per offer, preceded by a summary line (offer count, the location it was resolved to or \"site-wide\", radius, page).

| Column   | Example                                                  |
| -------- | -------------------------------------------------------- |
| Title    | `Kuriér, Kuriérka`                                       |
| Employer | `JMT Trans s. r. o.`                                     |
| Location | `Bratislava`                                             |
| Salary   | `1 600 - 1 800 EUR/mesiac`                               |
| URL      | `https://www.profesia.sk/praca/jmt-trans/O5347750`       |
| Posted   | `Pred 2 týždňami`                                        |
| Extra    | `Zabezpečené ubytovanie; Можливість для людей з України` |

</details>

<details>
<summary><code>suggest_location</code></summary>

Resolves a city or region name into the concrete locations the search endpoint understands.

**Inputs**

- `city` (string, required): place name to resolve (e.g. `Kosice`, `Bratislava`).

**Output**

- JSON array of `{ region_id, name, region_type }` entries. Use it to disambiguate a place name, then pass the chosen `name` (or its `region_id`) back to `search_jobs`.

</details>

## Run

```bash
# stdio (what MCP clients use)
uv run python -m profesia_mcp

# or, once installed from the package:
uv run profesia-mcp
```

## Connect from an MCP client

Use the config below in your MCP client's servers config. `uvx` installs and runs the server straight from your GitHub repo, so there's nothing to install locally.

```json
{
  "mcpServers": {
    "profesia": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/AyQWERTY/profesia-mcp",
        "profesia-mcp"
      ]
    }
  }
}
```
