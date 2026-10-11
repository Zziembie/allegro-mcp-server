"""MCP server (stdio) exposing Allegro REST API tools.

Nothing is configured at import time. ``run()`` loads the environment, validates
credentials and then either runs the login flow (``--auth``) or serves MCP over stdio.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mcp import types
from mcp.server import Server

from . import __version__
from .api import PROMO_OPTIONS, AllegroClient
from .auth import AllegroAuth
from .config import ConfigError, Settings, load_settings

ISO_DATE = "ISO 8601 date-time, e.g. 2026-01-01T00:00:00Z"
OFFER_ID = {"type": "string", "description": "Allegro offer ID"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[AllegroClient, dict[str, Any]], Any]
    write: bool = False

    def as_tool(self) -> types.Tool:
        description = self.description
        if self.write:
            description += " Modifies data on the Allegro account."
        return types.Tool(
            name=self.name,
            description=description,
            input_schema=self.schema,
            annotations=types.ToolAnnotations(
                read_only_hint=not self.write,
                destructive_hint=self.write,
                open_world_hint=True,
            ),
        )


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return schema


def _paging(default_limit: int, max_limit: int) -> dict[str, Any]:
    return {
        "limit": {
            "type": "integer", "minimum": 1, "maximum": max_limit,
            "description": f"Max items to return (default {default_limit}, max {max_limit})",
        },
        "offset": {"type": "integer", "minimum": 0, "description": "Pagination offset"},
    }


READ_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "allegro_get_me",
        "Get the authenticated Allegro account (login, email, company).",
        _schema(),
        lambda c, a: c.get_me(),
    ),
    ToolSpec(
        "allegro_get_offers",
        "List the seller's offers with ID, name, price, stock and publication status.",
        _schema({
            **_paging(20, 1000),
            "phrase": {"type": "string", "description": "Substring of the offer title"},
            "status": {
                "type": "string",
                "enum": ["INACTIVE", "ACTIVE", "ACTIVATING", "ENDED"],
                "description": "Filter by publication status",
            },
            "sort": {
                "type": "string",
                "description": "Sort expression passed to the API, e.g. '-stock.sold'",
            },
        }),
        lambda c, a: c.get_offers(
            limit=a.get("limit", 20),
            offset=a.get("offset", 0),
            phrase=a.get("phrase"),
            status=a.get("status"),
            sort=a.get("sort"),
        ),
    ),
    ToolSpec(
        "allegro_get_offer",
        "Get full details of a single offer.",
        _schema({"offer_id": OFFER_ID}, ["offer_id"]),
        lambda c, a: c.get_offer(a["offer_id"]),
    ),
    ToolSpec(
        "allegro_get_offer_stats",
        "Get views, watchers and sales for up to 30 offers. Falls back to cumulative "
        "counters from the offer list when the stats endpoint is unavailable.",
        _schema(
            {
                "offer_ids": {
                    "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 30,
                    "description": "Offer IDs",
                },
                "date_from": {"type": "string", "description": ISO_DATE},
                "date_to": {"type": "string", "description": ISO_DATE},
            },
            ["offer_ids"],
        ),
        lambda c, a: c.get_offer_stats(a["offer_ids"], a.get("date_from"), a.get("date_to")),
    ),
    ToolSpec(
        "allegro_get_offer_promo_options",
        "Get the promo options (bold, emphasize, highlight, department page) of an offer.",
        _schema({"offer_id": OFFER_ID}, ["offer_id"]),
        lambda c, a: c.get_offer_promo_options(a["offer_id"]),
    ),
    ToolSpec(
        "allegro_get_shipping_rates",
        "List the seller's shipping rate tables (IDs can be passed to allegro_create_offer).",
        _schema(),
        lambda c, a: c.get_shipping_rates(),
    ),
    ToolSpec(
        "allegro_get_orders",
        "List orders (checkout forms), optionally filtered by status and purchase date. "
        "The date window filters on purchase date; the API has no payment date filter. "
        "Results contain buyer personal data.",
        _schema({
            **_paging(20, 100),
            "status": {
                "type": "string",
                "enum": ["BOUGHT", "FILLED_IN", "READY_FOR_PROCESSING", "CANCELLED"],
                "description": "Filter by order status",
            },
            "bought_from": {"type": "string", "description": f"Purchased at or after ({ISO_DATE})"},
            "bought_to": {"type": "string", "description": f"Purchased at or before ({ISO_DATE})"},
        }),
        lambda c, a: c.get_orders(
            limit=a.get("limit", 20),
            offset=a.get("offset", 0),
            status=a.get("status"),
            bought_from=a.get("bought_from"),
            bought_to=a.get("bought_to"),
        ),
    ),
    ToolSpec(
        "allegro_get_order",
        "Get full details of a single order. The result contains buyer personal data.",
        _schema({"order_id": {"type": "string", "description": "Checkout form ID"}}, ["order_id"]),
        lambda c, a: c.get_order(a["order_id"]),
    ),
    ToolSpec(
        "allegro_get_billing_entries",
        "List billing entries (offer fees, commissions, ad costs, refunds).",
        _schema({
            **_paging(20, 100),
            "occurred_at_gte": {"type": "string", "description": f"From ({ISO_DATE})"},
            "occurred_at_lte": {"type": "string", "description": f"To ({ISO_DATE})"},
            "type_group": {
                "type": "string",
                "description": "Entry type group, e.g. OFFER, ORDER, ADVERTISEMENT, REFUND",
            },
        }),
        lambda c, a: c.get_billing_entries(
            limit=a.get("limit", 20),
            offset=a.get("offset", 0),
            occurred_at_gte=a.get("occurred_at_gte"),
            occurred_at_lte=a.get("occurred_at_lte"),
            type_group=a.get("type_group"),
        ),
    ),
    ToolSpec(
        "allegro_get_billing_balance",
        "Get the current billing balance of the account.",
        _schema(),
        lambda c, a: c.get_billing_balance(),
    ),
    ToolSpec(
        "allegro_get_sponsored_groups",
        "List Allegro Ads campaigns. Falls back to ADVERTISEMENT billing totals "
        "when the campaigns endpoint is unavailable.",
        _schema(),
        lambda c, a: c.get_sponsored_groups(),
    ),
    ToolSpec(
        "allegro_get_sponsored_group",
        "Get a single Allegro Ads campaign.",
        _schema({"group_id": {"type": "string", "description": "Campaign ID"}}, ["group_id"]),
        lambda c, a: c.get_sponsored_group(a["group_id"]),
    ),
    ToolSpec(
        "allegro_get_sale_quality",
        "Get the seller quality score and its metrics, with up to 30 days of history.",
        _schema(),
        lambda c, a: c.get_sale_quality(),
    ),
]

WRITE_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "allegro_rename_offer",
        "Change the title of an offer (max 75 characters).",
        _schema(
            {"offer_id": OFFER_ID, "new_name": {"type": "string", "maxLength": 75}},
            ["offer_id", "new_name"],
        ),
        lambda c, a: c.rename_offer(a["offer_id"], a["new_name"]),
        write=True,
    ),
    ToolSpec(
        "allegro_set_offer_price",
        "Change the Buy Now price of an offer.",
        _schema(
            {
                "offer_id": OFFER_ID,
                "amount": {"type": "string", "description": "Price as a decimal string, e.g. '119.00'"},
                "currency": {"type": "string", "description": "Currency code (default PLN)"},
            },
            ["offer_id", "amount"],
        ),
        lambda c, a: c.set_offer_price(a["offer_id"], a["amount"], a.get("currency", "PLN")),
        write=True,
    ),
    ToolSpec(
        "allegro_set_offer_description",
        "Replace the offer description with a single HTML text section. Allegro accepts "
        "a limited tag set: p, b, ul, ol, li, h1, h2.",
        _schema(
            {"offer_id": OFFER_ID, "html_content": {"type": "string"}},
            ["offer_id", "html_content"],
        ),
        lambda c, a: c.set_offer_description(a["offer_id"], a["html_content"]),
        write=True,
    ),
    ToolSpec(
        "allegro_set_offer_status",
        "Activate (ACTIVE) or end (ENDED) an offer via a publication command.",
        _schema(
            {"offer_id": OFFER_ID, "status": {"type": "string", "enum": ["ACTIVE", "ENDED"]}},
            ["offer_id", "status"],
        ),
        lambda c, a: c.set_offer_status(a["offer_id"], a["status"]),
        write=True,
    ),
    ToolSpec(
        "allegro_set_promo_options",
        "Enable or disable paid promo options on one or more offers. May incur fees.",
        _schema(
            {
                "offer_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                **{key: {"type": "boolean"} for key in PROMO_OPTIONS},
            },
            ["offer_ids"],
        ),
        lambda c, a: c.set_promo_options(
            a["offer_ids"], {k: a[k] for k in PROMO_OPTIONS if k in a}
        ),
        write=True,
    ),
    ToolSpec(
        "allegro_upload_image",
        "Upload a local JPEG or PNG file to the Allegro image service and return its URL.",
        _schema(
            {
                "image_path": {"type": "string", "description": "Path to a .jpg/.jpeg/.png file"},
                "is_ai_co_created": {
                    "type": "boolean",
                    "description": "Declare the image as AI-generated or AI-modified (default false)",
                },
            },
            ["image_path"],
        ),
        lambda c, a: {
            "url": c.upload_image(a["image_path"], bool(a.get("is_ai_co_created", False)))
        },
        write=True,
    ),
    ToolSpec(
        "allegro_create_offer",
        "Create a new offer by cloning category, parameters, product, delivery and "
        "after-sales settings from an existing offer (template_offer_id). Upload images "
        "first. Created as a draft unless status is ACTIVE.",
        _schema(
            {
                "template_offer_id": {"type": "string", "description": "Offer to copy settings from"},
                "name": {"type": "string", "maxLength": 75},
                "description_html": {"type": "string"},
                "image_urls": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "price": {"type": "string", "description": "e.g. '49.90'"},
                "currency": {"type": "string", "description": "Default PLN"},
                "stock": {"type": "integer", "minimum": 1, "description": "Default 1"},
                "shipping_rate_id": {
                    "type": "string",
                    "description": "Shipping rate table to use instead of the template's",
                },
                "handling_time": {
                    "type": "string",
                    "description": "ISO 8601 duration overriding the template, e.g. PT24H",
                },
                "status": {
                    "type": "string",
                    "enum": ["INACTIVE", "ACTIVE"],
                    "description": "INACTIVE (default) keeps a draft, ACTIVE publishes",
                },
            },
            ["template_offer_id", "name", "description_html", "image_urls", "price"],
        ),
        lambda c, a: c.create_offer_from_template(
            a["template_offer_id"],
            name=a["name"],
            description_html=a["description_html"],
            image_urls=a["image_urls"],
            price=a["price"],
            currency=a.get("currency", "PLN"),
            stock=a.get("stock", 1),
            shipping_rate_id=a.get("shipping_rate_id"),
            handling_time=a.get("handling_time"),
            activate=a.get("status") == "ACTIVE",
        ),
        write=True,
    ),
]


def enabled_tools(enable_write_tools: bool) -> list[ToolSpec]:
    return READ_TOOLS + (WRITE_TOOLS if enable_write_tools else [])


def _text_result(payload: Any, is_error: bool = False) -> types.CallToolResult:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


async def dispatch(
    tools: dict[str, ToolSpec],
    client: AllegroClient | None,
    name: str,
    arguments: dict[str, Any] | None,
) -> types.CallToolResult:
    """Run one tool call. Errors are returned to the agent as tool results, not raised."""
    spec = tools.get(name)
    if spec is None:
        if any(name == write.name for write in WRITE_TOOLS):
            message = (
                f"{name} is a write tool and write tools are disabled. "
                "Set ALLEGRO_ENABLE_WRITE_TOOLS=1 to enable them."
            )
        else:
            message = f"Unknown tool: {name}"
        return _text_result({"error": message, "tool": name}, is_error=True)

    arguments = arguments or {}
    missing = [key for key in spec.schema.get("required", []) if key not in arguments]
    if missing:
        return _text_result(
            {"error": f"Missing required argument(s): {', '.join(missing)}", "tool": name},
            is_error=True,
        )
    try:
        # httpx calls are synchronous; keep them off the event loop.
        result = await asyncio.to_thread(spec.handler, client, arguments)
    except Exception as exc:  # noqa: BLE001 - every failure is reported back to the agent
        return _text_result({"error": str(exc), "tool": name}, is_error=True)
    return _text_result(result)


def build_server(client: AllegroClient | None, enable_write_tools: bool) -> Server:
    tools = {spec.name: spec for spec in enabled_tools(enable_write_tools)}

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[spec.as_tool() for spec in tools.values()])

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await dispatch(tools, client, params.name, params.arguments)

    return Server(
        "allegro-mcp-server",
        version=__version__,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def load_env_file() -> None:
    """Load ALLEGRO_ENV_FILE, or the nearest .env from the working directory upwards.

    Variables already set in the process (e.g. by the MCP client) take precedence.
    """
    from dotenv import find_dotenv, load_dotenv

    explicit = (os.getenv("ALLEGRO_ENV_FILE") or "").strip()
    path = Path(explicit).expanduser() if explicit else find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)


def create_client(settings: Settings) -> AllegroClient:
    auth = AllegroAuth(
        settings.client_id,
        settings.client_secret,
        token_file=settings.token_file,
        auth_base=settings.auth_base,
    )
    return AllegroClient(auth, api_base=settings.api_base)


async def serve(settings: Settings) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(create_client(settings), settings.enable_write_tools)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run(argv: list[str] | None = None) -> None:
    """Console entry point: ``allegro-mcp-server [--auth]``."""
    parser = argparse.ArgumentParser(prog="allegro-mcp-server", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--auth", action="store_true",
        help="run the OAuth device code login and store the token, then exit",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    load_env_file()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"allegro-mcp-server: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if args.auth:
        try:
            create_client(settings).auth.device_code_flow()
        except Exception as exc:  # noqa: BLE001 - report any login failure without a traceback
            print(f"allegro-mcp-server: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        return

    asyncio.run(serve(settings))
