import httpx
import pytest

from allegro_mcp.api import (
    ORDERS_FROM_PARAM,
    ORDERS_TO_PARAM,
    AllegroAPIError,
    AllegroClient,
    aggregate_billing_entries,
    order_timestamp,
    promo_options_payload,
    strip_for_create,
    validate_offer_name,
)


class StaticAuth:
    def get_access_token(self) -> str:
        return "test-token"


def make_client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return AllegroClient(StaticAuth(), api_base="https://api.example", http=http)


def test_order_timestamp_prefers_purchase_date():
    form = {
        "lineItems": [{"boughtAt": "2026-01-02T10:00:00Z"}],
        "payment": {"finishedAt": "2026-01-03T10:00:00Z"},
        "updatedAt": "2026-01-04T10:00:00Z",
    }
    assert order_timestamp(form) == "2026-01-02T10:00:00Z"
    assert order_timestamp({"payment": {"finishedAt": "P"}, "updatedAt": "U"}) == "P"
    assert order_timestamp({"updatedAt": "U"}) == "U"
    assert order_timestamp({}) == ""


def test_validate_offer_name():
    assert validate_offer_name("  Poster 30x40  ") == "Poster 30x40"
    assert validate_offer_name("x" * 75) == "x" * 75
    with pytest.raises(ValueError):
        validate_offer_name("x" * 76)
    with pytest.raises(ValueError):
        validate_offer_name("   ")


def test_strip_for_create_removes_read_only_fields_recursively():
    data = {
        "category": {"id": "123"},
        "publication": {"status": "ACTIVE"},
        "productSet": [{"product": {"id": "p1", "isAiCoCreated": False}, "createdAt": "x"}],
    }
    assert strip_for_create(data) == {
        "category": {"id": "123"},
        "productSet": [{"product": {"id": "p1"}}],
    }


def test_promo_options_payload():
    payload = promo_options_payload(["1", "2"], {"bold": True, "highlight": False})
    assert payload["offerCriteria"] == [
        {"offers": [{"id": "1"}, {"id": "2"}], "type": "CONTAINS_OFFERS"}
    ]
    assert payload["promoOptions"] == {"bold": {"enabled": True}, "highlight": {"enabled": False}}
    with pytest.raises(ValueError):
        promo_options_payload(["1"], {"glitter": True})
    with pytest.raises(ValueError):
        promo_options_payload(["1"], {})


def test_aggregate_billing_entries():
    entries = [
        {"type": {"id": "ADS", "name": "Ads"}, "value": {"amount": "-1.50", "currency": "PLN"}},
        {"type": {"id": "ADS", "name": "Ads"}, "value": {"amount": "-2.25", "currency": "PLN"}},
        {"type": {}, "value": {}},
    ]
    groups = {g["id"]: g for g in aggregate_billing_entries(entries)}
    assert groups["ADS"]["entryCount"] == 2
    assert groups["ADS"]["totalAmount"] == -3.75
    assert groups["ADS"]["currency"] == "PLN"
    assert groups["UNKNOWN"]["entryCount"] == 1


def test_get_orders_uses_purchase_date_params_and_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"checkoutForms": []})

    make_client(handler).get_orders(limit=5, bought_from="A", bought_to="B", status="BOUGHT")
    request = seen["request"]
    assert request.url.path == "/order/checkout-forms"
    assert request.url.params[ORDERS_FROM_PARAM] == "A"
    assert request.url.params[ORDERS_TO_PARAM] == "B"
    assert request.url.params["status"] == "BOUGHT"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Accept"] == "application/vnd.allegro.public.v1+json"
    assert request.headers["User-Agent"].startswith("allegro-mcp-server/")


def test_api_error_includes_status_and_body():
    client = make_client(lambda request: httpx.Response(422, json={"errors": [{"message": "bad"}]}))
    with pytest.raises(AllegroAPIError) as exc:
        client.get_offer("1")
    assert exc.value.status_code == 422
    assert "bad" in str(exc.value)


def test_sponsored_groups_fallback_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sale/sponsored-groups":
            return httpx.Response(404, json={})
        assert request.url.params["type.group"] == "ADVERTISEMENT"
        return httpx.Response(200, json={"billingEntries": [
            {"type": {"id": "NSP"}, "value": {"amount": "-4.00", "currency": "PLN"}},
        ]})

    result = make_client(handler).get_sponsored_groups()
    assert result["source"] == "billing_fallback"
    assert result["groups"][0]["totalAmount"] == -4.0


def test_set_offer_status_uses_publication_command():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(201, json={"id": "cmd"})

    make_client(handler).set_offer_status("42", "ENDED")
    request = seen["request"]
    assert request.method == "PUT"
    assert request.url.path.startswith("/sale/offer-publication-commands/")
    assert b'"action":"END"' in request.content.replace(b" ", b"")
    with pytest.raises(ValueError):
        make_client(handler).set_offer_status("42", "PAUSED")


def test_upload_image_rejects_unsupported_extension(tmp_path):
    path = tmp_path / "a.gif"
    path.write_bytes(b"GIF89a")
    with pytest.raises(ValueError):
        make_client(lambda request: httpx.Response(500)).upload_image(str(path))
