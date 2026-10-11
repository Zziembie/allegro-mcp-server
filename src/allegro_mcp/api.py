"""Thin client for the Allegro REST API."""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from .auth import AllegroAuth
from .user_agent import allegro_headers

MEDIA_TYPE = "application/vnd.allegro.public.v1+json"
MAX_OFFER_NAME_LENGTH = 75

# GET /order/checkout-forms silently ignores unknown query parameters and returns
# the full list instead of an error. The purchase date filter only works with the
# lineItems prefix; there is no payment date filter on this endpoint.
ORDERS_FROM_PARAM = "lineItems.boughtAt.gte"
ORDERS_TO_PARAM = "lineItems.boughtAt.lte"

# Read-only fields returned by GET /sale/product-offers/{id} that POST rejects.
_CREATE_FORBIDDEN = frozenset({
    "publication", "isAiCoCreated", "updatedAt", "createdAt",
    "validation", "warnings", "language",
})

# Template fields copied to a new offer when cloning.
_TEMPLATE_FIELDS = (
    "category", "parameters", "productSet", "delivery", "payments", "location",
    "afterSalesServices", "external", "taxSettings", "sizeTable",
)

PROMO_OPTIONS = ("bold", "emphasize", "highlight", "departmentPage")

IMAGE_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


class AllegroAPIError(RuntimeError):
    """Non-2xx response from the Allegro API. The message includes the response body."""

    def __init__(self, method: str, path: str, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {path} failed with HTTP {status_code}: {body[:2000]}")


# ─── Pure helpers ────────────────────────────────────────────────────────────


def order_timestamp(form: dict[str, Any]) -> str:
    """Best available order date (ISO 8601): purchase, then payment, then last update."""
    line_items = form.get("lineItems") or []
    candidates = (
        line_items[0].get("boughtAt") if line_items else None,
        (form.get("payment") or {}).get("finishedAt"),
        form.get("updatedAt"),
    )
    return next((value for value in candidates if value), "")


def validate_offer_name(name: str) -> str:
    """Strip whitespace and enforce the Allegro title limit instead of truncating silently."""
    name = name.strip()
    if not name:
        raise ValueError("Offer name must not be empty.")
    if len(name) > MAX_OFFER_NAME_LENGTH:
        raise ValueError(
            f"Offer name has {len(name)} characters; Allegro allows {MAX_OFFER_NAME_LENGTH}."
        )
    return name


def strip_for_create(obj: Any) -> Any:
    """Recursively remove read-only keys that POST /sale/product-offers rejects."""
    if isinstance(obj, dict):
        return {k: strip_for_create(v) for k, v in obj.items() if k not in _CREATE_FORBIDDEN}
    if isinstance(obj, list):
        return [strip_for_create(item) for item in obj]
    return obj


def description_payload(html: str) -> dict[str, Any]:
    """Offer description with a single text section."""
    return {"sections": [{"items": [{"type": "TEXT", "content": html}]}]}


def offer_criteria(offer_ids: list[str]) -> list[dict[str, Any]]:
    return [{"offers": [{"id": str(oid)} for oid in offer_ids], "type": "CONTAINS_OFFERS"}]


def promo_options_payload(offer_ids: list[str], options: dict[str, bool]) -> dict[str, Any]:
    """Body for PUT /sale/promo-options-commands/{commandId}."""
    unknown = set(options) - set(PROMO_OPTIONS)
    if unknown:
        raise ValueError(f"Unknown promo option(s): {', '.join(sorted(unknown))}")
    if not options:
        raise ValueError("Pass at least one promo option.")
    return {
        "offerCriteria": offer_criteria(offer_ids),
        "promoOptions": {key: {"enabled": bool(value)} for key, value in options.items()},
    }


def aggregate_billing_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum billing entries by type: [{id, name, entryCount, totalAmount, currency}]."""
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "total": 0.0, "currency": None, "name": None}
    )
    for entry in entries:
        entry_type = entry.get("type") or {}
        key = entry_type.get("id") or entry_type.get("name") or "UNKNOWN"
        value = entry.get("value") or {}
        group = grouped[key]
        group["count"] += 1
        group["total"] += float(value.get("amount") or 0)
        group["currency"] = group["currency"] or value.get("currency")
        group["name"] = group["name"] or entry_type.get("name") or key
    return [
        {
            "id": key,
            "name": data["name"],
            "entryCount": data["count"],
            "totalAmount": round(data["total"], 2),
            "currency": data["currency"],
        }
        for key, data in grouped.items()
    ]


# ─── Client ──────────────────────────────────────────────────────────────────


class AllegroClient:
    def __init__(
        self,
        auth: AllegroAuth,
        api_base: str,
        http: httpx.Client | None = None,
    ):
        self.auth = auth
        self.api_base = api_base.rstrip("/")
        self.http = http or httpx.Client(timeout=60)

    def _headers(self, content_type: str = MEDIA_TYPE) -> dict[str, str]:
        return allegro_headers({
            "Authorization": f"Bearer {self.auth.get_access_token()}",
            "Accept": MEDIA_TYPE,
            "Content-Type": content_type,
        })

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        content_type: str = MEDIA_TYPE,
    ) -> Any:
        resp = self.http.request(
            method,
            f"{self.api_base}{path}",
            headers=self._headers(content_type),
            params=params,
            json=json,
            content=content,
        )
        if resp.is_error:
            raise AllegroAPIError(method, path, resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    # ─── Account ─────────────────────────────────────────────────────────────

    def get_me(self) -> dict:
        return self._get("/me")

    def get_sale_quality(self) -> dict:
        return self._get("/sale/quality")

    # ─── Offers (read) ───────────────────────────────────────────────────────

    def get_offers(
        self,
        limit: int = 20,
        offset: int = 0,
        phrase: str | None = None,
        status: str | None = None,
        sort: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if phrase:
            params["name"] = phrase
        if status:
            params["publication.status"] = status
        if sort:
            params["sort"] = sort
        return self._get("/sale/offers", params)

    def get_offer(self, offer_id: str) -> dict:
        return self._get(f"/sale/product-offers/{offer_id}")

    def get_offer_promo_options(self, offer_id: str) -> dict:
        return self._get(f"/sale/offers/{offer_id}/promo-options")

    def get_shipping_rates(self) -> dict:
        return self._get("/sale/shipping-rates")

    def get_offer_stats(
        self,
        offer_ids: list[str],
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """Offer statistics. Falls back to cumulative counters from the offer list on 404."""
        params: dict[str, Any] = {"offer.id": offer_ids}
        if date_from:
            params["dateRange.from"] = date_from
        if date_to:
            params["dateRange.to"] = date_to
        try:
            return self._get("/sale/offer-stats", params)
        except AllegroAPIError as exc:
            if exc.status_code != 404:
                raise

        wanted = {str(oid) for oid in offer_ids}
        found: dict[str, dict] = {}
        offset = 0
        while wanted - set(found):
            page = self.get_offers(limit=1000, offset=offset).get("offers") or []
            found.update({str(o.get("id")): o for o in page if str(o.get("id")) in wanted})
            if len(page) < 1000:
                break
            offset += 1000

        offers = []
        for oid in offer_ids:
            offer = found.get(str(oid)) or {}
            stats = offer.get("stats") or {}
            stock = offer.get("stock") or {}
            offers.append({
                "offerId": str(oid),
                "name": offer.get("name"),
                "visitsCount": stats.get("visitsCount"),
                "watchersCount": stats.get("watchersCount"),
                "soldCount": stock.get("sold"),
                "availableStock": stock.get("available"),
                "status": (offer.get("publication") or {}).get("status"),
                "found": bool(offer),
            })
        result: dict[str, Any] = {
            "source": "offers_fallback",
            "message": "Offer stats endpoint unavailable; returning counters from the offer list.",
            "offers": offers,
        }
        if date_from or date_to:
            result["dateRangeWarning"] = "Date filters are ignored in fallback mode."
        return result

    # ─── Ads ─────────────────────────────────────────────────────────────────

    def get_sponsored_groups(self) -> dict:
        """Ad campaigns. Falls back to ADVERTISEMENT billing totals on 404."""
        try:
            return self._get("/sale/sponsored-groups")
        except AllegroAPIError as exc:
            if exc.status_code != 404:
                raise
        entries = self.get_billing_entries(limit=100, type_group="ADVERTISEMENT").get(
            "billingEntries", []
        )
        return {
            "source": "billing_fallback",
            "message": "Sponsored groups endpoint unavailable; returning ADVERTISEMENT billing totals.",
            "groups": aggregate_billing_entries(entries),
        }

    def get_sponsored_group(self, group_id: str) -> dict:
        try:
            return self._get(f"/sale/sponsored-groups/{group_id}")
        except AllegroAPIError as exc:
            if exc.status_code != 404:
                raise
        groups = self.get_sponsored_groups().get("groups", [])
        match = next((g for g in groups if g.get("id") == group_id), None)
        if match:
            return {"source": "billing_fallback", "group": match}
        return {
            "source": "billing_fallback",
            "error": f"Group '{group_id}' not found.",
            "availableGroupIds": [g.get("id") for g in groups],
        }

    # ─── Orders ──────────────────────────────────────────────────────────────

    def get_orders(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        bought_from: str | None = None,
        bought_to: str | None = None,
    ) -> dict:
        """Orders, optionally filtered by status and purchase date window."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if bought_from:
            params[ORDERS_FROM_PARAM] = bought_from
        if bought_to:
            params[ORDERS_TO_PARAM] = bought_to
        return self._get("/order/checkout-forms", params)

    def get_order(self, order_id: str) -> dict:
        return self._get(f"/order/checkout-forms/{order_id}")

    # ─── Billing ─────────────────────────────────────────────────────────────

    def get_billing_entries(
        self,
        limit: int = 20,
        offset: int = 0,
        occurred_at_gte: str | None = None,
        occurred_at_lte: str | None = None,
        type_group: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if occurred_at_gte:
            params["occurredAt.gte"] = occurred_at_gte
        if occurred_at_lte:
            params["occurredAt.lte"] = occurred_at_lte
        if type_group:
            params["type.group"] = type_group
        return self._get("/billing/billing-entries", params)

    def get_billing_balance(self) -> dict:
        return self._get("/billing/balance")

    # ─── Offers (write) ──────────────────────────────────────────────────────

    def _patch_offer(self, offer_id: str, payload: dict[str, Any]) -> dict:
        return self._request("PATCH", f"/sale/product-offers/{offer_id}", json=payload)

    def rename_offer(self, offer_id: str, new_name: str) -> dict:
        return self._patch_offer(offer_id, {"name": validate_offer_name(new_name)})

    def set_offer_price(self, offer_id: str, amount: str, currency: str = "PLN") -> dict:
        return self._patch_offer(
            offer_id, {"sellingMode": {"price": {"amount": str(amount), "currency": currency}}}
        )

    def set_offer_description(self, offer_id: str, html: str) -> dict:
        return self._patch_offer(offer_id, {"description": description_payload(html)})

    def set_offer_images(self, offer_id: str, image_urls: list[str]) -> dict:
        return self._patch_offer(offer_id, {"images": image_urls})

    def set_offer_status(self, offer_id: str, status: str) -> dict:
        """Activate or end an offer through a publication command (asynchronous on Allegro's side)."""
        actions = {"ACTIVE": "ACTIVATE", "ENDED": "END"}
        if status not in actions:
            raise ValueError("status must be ACTIVE or ENDED")
        payload = {
            "offerCriteria": offer_criteria([offer_id]),
            "publication": {"action": actions[status]},
        }
        return self._request(
            "PUT", f"/sale/offer-publication-commands/{uuid.uuid4()}", json=payload
        )

    def set_promo_options(self, offer_ids: list[str], options: dict[str, bool]) -> dict:
        return self._request(
            "PUT",
            f"/sale/promo-options-commands/{uuid.uuid4()}",
            json=promo_options_payload(offer_ids, options),
        )

    def upload_image(self, image_path: str, is_ai_co_created: bool = False) -> str:
        """Upload a local JPEG or PNG file to the Allegro image service. Returns its URL."""
        path = Path(image_path).expanduser()
        content_type = IMAGE_CONTENT_TYPES.get(path.suffix.lower())
        if not content_type:
            raise ValueError("Only .jpg, .jpeg and .png files are supported.")
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        params = {"isAiCoCreated": "true"} if is_ai_co_created else None
        data = self._request(
            "POST",
            "/sale/images",
            params=params,
            content=path.read_bytes(),
            content_type=content_type,
        )
        location = data.get("location")
        if not location:
            raise AllegroAPIError("POST", "/sale/images", 200, f"No 'location' in response: {data}")
        return location

    def create_offer_from_template(
        self,
        template_offer_id: str,
        *,
        name: str,
        description_html: str,
        image_urls: list[str],
        price: str,
        currency: str = "PLN",
        stock: int = 1,
        shipping_rate_id: str | None = None,
        handling_time: str | None = None,
        activate: bool = False,
    ) -> dict:
        """Create a new offer by copying category, parameters, product, delivery and
        after-sales settings from an existing offer of the same account.

        The new offer is created as a draft (INACTIVE) unless ``activate`` is True.
        ``shipping_rate_id`` and ``handling_time`` override the template values.
        """
        if not image_urls:
            raise ValueError("Pass at least one image URL (upload images first).")
        template = self.get_offer(template_offer_id)

        payload: dict[str, Any] = {key: template.get(key) for key in _TEMPLATE_FIELDS}
        delivery = dict(payload.get("delivery") or {})
        if shipping_rate_id:
            delivery["shippingRates"] = {"id": shipping_rate_id}
        if handling_time:
            delivery["handlingTime"] = handling_time
        payload.update({
            "name": validate_offer_name(name),
            "description": description_payload(description_html),
            "images": image_urls,
            "sellingMode": {"format": "BUY_NOW", "price": {"amount": str(price), "currency": currency}},
            "stock": {"available": stock, "unit": "UNIT"},
            "delivery": delivery,
        })
        payload = {k: v for k, v in strip_for_create(payload).items() if v is not None}

        result = self._request("POST", "/sale/product-offers", json=payload)
        offer_id = str(result.get("id") or "")
        if not offer_id:
            return result

        # Product data can override the gallery on creation; reapply it if images went missing.
        if len(result.get("images") or []) < len(image_urls):
            result = self.set_offer_images(offer_id, image_urls)

        status = (result.get("publication") or {}).get("status")
        if activate:
            self.set_offer_status(offer_id, "ACTIVE")
        elif status == "ACTIVE":
            # Keep the draft guarantee even if the account auto-publishes new offers.
            self.set_offer_status(offer_id, "ENDED")
        return result
