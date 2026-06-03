import os
import time
import base64

import httpx
import uvicorn
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

load_dotenv()

mcp = FastMCP("eBay MCP")

# ---------------------------------------------------------------------------
# OAuth2 client-credentials token cache
# ---------------------------------------------------------------------------

_token_cache: dict = {"token": None, "expires_at": 0.0}

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_BROWSE_BASE = "https://api.ebay.com/buy/browse/v1"
MARKETPLACE_ID = "EBAY_US"


async def _get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            EBAY_TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": EBAY_SCOPE},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data["expires_in"])
    return _token_cache["token"]


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

CONDITION_MAP = {
    "new": "NEW",
    "used": "USED",
    "unspecified": "UNSPECIFIED",
    "certified_refurbished": "CERTIFIED_REFURBISHED",
    "excellent_refurbished": "EXCELLENT_REFURBISHED",
    "very_good_refurbished": "VERY_GOOD_REFURBISHED",
    "good_refurbished": "GOOD_REFURBISHED",
}

SORT_OPTIONS = {"bestMatch", "price", "-price", "distance", "newlyListed", "-newlyListed"}


@mcp.tool()
async def search_ebay(
    keyword: str,
    min_price: float | None = None,
    max_price: float | None = None,
    condition: str | None = None,
    sort: str = "bestMatch",
    limit: int = 10,
) -> dict:
    """Search eBay listings using the Browse API.

    Args:
        keyword: Search query string.
        min_price: Minimum price in USD (inclusive).
        max_price: Maximum price in USD (inclusive).
        condition: Item condition — new, used, unspecified,
                   certified_refurbished, excellent_refurbished,
                   very_good_refurbished, good_refurbished.
        sort: Sort order — bestMatch, price, -price (desc),
              distance, newlyListed, -newlyListed.
        limit: Number of results to return (1–50, default 10).

    Returns:
        eBay item_summary/search response dict with itemSummaries list.
    """
    if sort not in SORT_OPTIONS:
        sort = "bestMatch"

    limit = max(1, min(limit, 50))
    token = await _get_access_token()

    params: dict = {"q": keyword, "limit": limit, "sort": sort}

    filters: list[str] = []
    if min_price is not None or max_price is not None:
        lo = str(min_price) if min_price is not None else ""
        hi = str(max_price) if max_price is not None else ""
        filters += [f"price:[{lo}..{hi}]", "priceCurrency:USD"]

    if condition:
        cond_key = condition.lower().replace(" ", "_")
        cond_val = CONDITION_MAP.get(cond_key, condition.upper())
        filters.append(f"conditions:{{{cond_val}}}")

    if filters:
        params["filter"] = ",".join(filters)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{EBAY_BROWSE_BASE}/item_summary/search",
            headers=_auth_headers(token),
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_ebay_item(item_id: str) -> dict:
    """Fetch full details for a single eBay item by its item ID.

    Args:
        item_id: The eBay item ID (e.g. "v1|123456789012|0" or a plain
                 numeric ID string). Use the itemId values returned by
                 search_ebay.

    Returns:
        eBay item detail dict including title, price, condition, seller,
        images, description, shipping options, and return policy.
    """
    token = await _get_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{EBAY_BROWSE_BASE}/item/{item_id}",
            headers=_auth_headers(token),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Health endpoint + ASGI app composition
# ---------------------------------------------------------------------------

async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "ebay-mcp"})


app = Starlette(
    routes=[
        Route("/health", health),
        Mount("/", app=mcp.sse_app()),
    ]
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
