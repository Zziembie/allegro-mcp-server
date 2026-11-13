# allegro-mcp-server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives AI agents
(Claude Desktop, Claude Code and other MCP clients) access to the
[Allegro REST API](https://developer.allegro.pl): offers, orders, billing, ads and seller
quality. It runs locally over stdio and talks to Allegro with your own OAuth token.

I built it to let an agent answer day-to-day seller questions ("what sold this week",
"how much did ads cost in March", "which offers have no views") and, when explicitly
allowed, make small edits to offers.

## Tools

Read tools are always available.

| Tool | What it does |
| --- | --- |
| `allegro_get_me` | Authenticated account (login, email, company) |
| `allegro_get_offers` | List offers, filter by title substring or publication status |
| `allegro_get_offer` | Full details of one offer |
| `allegro_get_offer_stats` | Views, watchers and sales for up to 30 offers |
| `allegro_get_offer_promo_options` | Promo options of one offer |
| `allegro_get_shipping_rates` | Shipping rate tables of the account |
| `allegro_get_orders` | List orders, filter by status and purchase date |
| `allegro_get_order` | Full details of one order |
| `allegro_get_billing_entries` | Fees, commissions, ad costs, refunds |
| `allegro_get_billing_balance` | Current billing balance |
| `allegro_get_sponsored_groups` | Allegro Ads campaigns |
| `allegro_get_sponsored_group` | One Allegro Ads campaign |
| `allegro_get_sale_quality` | Seller quality score and metrics |

Write tools are hidden and rejected unless `ALLEGRO_ENABLE_WRITE_TOOLS=1` is set.

| Tool | What it does |
| --- | --- |
| `allegro_rename_offer` | Change an offer title (max 75 characters) |
| `allegro_set_offer_price` | Change the Buy Now price |
| `allegro_set_offer_description` | Replace the description with one HTML section |
| `allegro_set_offer_status` | Activate or end an offer |
| `allegro_set_promo_options` | Turn paid promo options on or off (may incur fees) |
| `allegro_upload_image` | Upload a local JPEG/PNG and return its Allegro URL |
| `allegro_create_offer` | Create a draft offer by cloning settings from an existing offer |

`allegro_create_offer` copies category, parameters, product, delivery, payments and
after-sales settings from a `template_offer_id` you pass (any offer on your account),
then applies the new title, description, images, price and stock. You can override the
shipping rate table (`shipping_rate_id`) and handling time (`handling_time`). The offer is
created as a draft unless you pass `status: "ACTIVE"`.

## Quick start

### 1. Register an application

Go to [developer.allegro.pl](https://developer.allegro.pl), open the application
management page and register a new application of type **device** (the server uses the OAuth device code
flow, so no redirect URL is needed). Copy the Client ID and Client Secret.

For testing, you can register a separate application in the Allegro sandbox
environment (described on developer.allegro.pl) and set `ALLEGRO_SANDBOX=1`. Sandbox
tokens are stored in a separate file by default.

### 2. Install

```bash
git clone https://github.com/Zziembie/allegro-mcp-server.git
cd allegro-mcp-server
python3 -m venv .venv
.venv/bin/pip install -e .
```

Python 3.10 or newer is required.

### 3. Configure

```bash
cp .env.example .env
```

Fill in `ALLEGRO_CLIENT_ID`, `ALLEGRO_CLIENT_SECRET` and the User-Agent fields.

**User-Agent.** Allegro requires every API request to carry a User-Agent in the form
`AppName/Version (+URL)`, and the name should match the application you registered at
developer.allegro.pl. The default, `allegro-mcp-server/0.1.0 (+https://github.com/)`, is
only a placeholder. Set `ALLEGRO_APP_NAME`, `ALLEGRO_APP_VERSION` and `ALLEGRO_APP_URL`
to your own values before using the server against a real account.

The server reads environment variables in this order: variables already set in the
process (for example by the MCP client config), then the file named in
`ALLEGRO_ENV_FILE`, then the nearest `.env` found from the working directory upwards.

| Variable | Required | Default |
| --- | --- | --- |
| `ALLEGRO_CLIENT_ID` | yes | |
| `ALLEGRO_CLIENT_SECRET` | yes | |
| `ALLEGRO_APP_NAME` | recommended | `allegro-mcp-server` |
| `ALLEGRO_APP_VERSION` | recommended | package version |
| `ALLEGRO_APP_URL` | recommended | `https://github.com/` |
| `ALLEGRO_TOKEN_FILE` | no | `~/.config/allegro-mcp/token.json` |
| `ALLEGRO_ENABLE_WRITE_TOOLS` | no | off |
| `ALLEGRO_SANDBOX` | no | off |
| `ALLEGRO_ENV_FILE` | no | |

### 4. Log in once

```bash
.venv/bin/allegro-mcp-server --auth
```

The command prints a verification link and code. Open the link, confirm the code while
logged in to your Allegro seller account, and the token is saved to the token file. The
server refreshes the access token automatically; run `--auth` again only if the refresh
token expires or is revoked.

### 5. Add the server to your MCP client

**Claude Code**

```bash
claude mcp add allegro \
  --env ALLEGRO_ENV_FILE=/absolute/path/to/allegro-mcp-server/.env \
  -- /absolute/path/to/allegro-mcp-server/.venv/bin/allegro-mcp-server
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "allegro": {
      "command": "/absolute/path/to/allegro-mcp-server/.venv/bin/allegro-mcp-server",
      "env": {
        "ALLEGRO_ENV_FILE": "/absolute/path/to/allegro-mcp-server/.env"
      }
    }
  }
}
```

You can also put the variables directly in the `env` block instead of pointing to a
`.env` file. Restart the client after editing the config.

## Security

- **Write tools are off by default.** Without `ALLEGRO_ENABLE_WRITE_TOOLS=1` the server
  does not list them and rejects calls to them. Enable them only in sessions where you
  review every tool call, and consider testing against the sandbox first.
- **The token stays on your machine.** It is stored as JSON with file mode `0600` in a
  directory with mode `0700`. Nothing is sent anywhere except the Allegro API.
  `ALLEGRO_TOKEN_FILE` may be a symlink; the token is written atomically to the file it
  points to and the link is kept.
- **One refresh at a time.** Allegro refresh tokens are single use, so the server takes
  an exclusive lock (`token.json.lock` next to the token, POSIX only) while it refreshes.
  Several processes can share one token file without revoking each other's token.
- **Order data contains personal data** (names, addresses, phone numbers, emails of
  buyers). Anything a tool returns is passed to the language model you use, so check
  that this is acceptable under your privacy obligations.
- **`allegro_upload_image` reads local files.** It only accepts `.jpg`, `.jpeg` and
  `.png`, but it uploads whatever file path the agent passes. It is a write tool for
  that reason.
- Keep `.env` and the token file out of version control. The included `.gitignore`
  covers the defaults.

## Limitations

- Covers a subset of the Allegro API that a single seller uses most often. There is no
  support for returns, disputes, messaging, shipments or invoices.
- Tool calls are synchronous HTTP requests with no retry or rate limit handling. An
  HTTP error is returned to the agent with the status code and the response body.
- `allegro_get_offer_stats` and `allegro_get_sponsored_groups` fall back to other data
  when their endpoints return 404: cumulative counters from the offer list, and
  ADVERTISEMENT billing totals grouped by entry type.
- The `allegro_get_orders` date window filters on purchase date. The API has no filter
  by payment date.
- `allegro_set_offer_status` and `allegro_set_promo_options` send asynchronous commands.
  The response confirms that the command was accepted, not that it finished.
- `allegro_upload_image` does not resize or recompress images.
- `allegro_get_billing_balance` returns HTTP 404 ("Feature unavailable") on accounts where Allegro does not offer the balance endpoint. The other read tools were checked against a live seller account.
- Offers created by `allegro_create_offer` inherit product, delivery and marketplace
  visibility settings from the template offer. Review the draft in Allegro Sales Center
  before publishing.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

The tests do not use the network; HTTP calls go through `httpx.MockTransport`.

## Author

Piotr Zięba

## License

MIT, see [LICENSE](LICENSE).
