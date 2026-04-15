import json
import time
import random
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

API_URL = "https://api.cian.ru/search-offers/v2/search-offers-desktop/"
OUTPUT_FILE = Path("data.json")
COOKIES_FILE = Path(__file__).parent / "cookies.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Origin": "https://www.cian.ru",
    "Referer": "https://www.cian.ru/",
}

# Moscow region ID = 1, 1-2-3-room flats, rent, no price limit
def build_query(page: int) -> dict:
    return {
        "jsonQuery": {
            "_type": "flatrent",
            "engine_version": {"type": "term", "value": 2},
            "region": {"type": "terms", "value": [1]},
            "room": {"type": "terms", "value": [1, 2, 3]},
            "for_day": {"type": "term", "value": "!1"},  # long-term only
            "page": {"type": "term", "value": page},
        }
    }


def fetch_page(session: requests.Session, page: int) -> Optional[dict]:
    payload = build_query(page)
    try:
        resp = session.post(API_URL, json=payload, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        log.error("HTTP %s on page %d: %s", e.response.status_code, page, e)
    except requests.RequestException as e:
        log.error("Request error on page %d: %s", page, e)
    return None


def parse_offer(offer: dict) -> dict:
    """Extract the fields we care about from a raw offer object."""
    full_url = offer.get("fullUrl") or f"https://www.cian.ru/rent/flat/{offer.get('id', '')}/"

    # Price
    bargain = offer.get("bargainTerms", {})
    price = bargain.get("priceRur") or bargain.get("price")

    # Address
    geo = offer.get("geo", {})
    address_parts = [item.get("name", "") for item in geo.get("address", [])]
    address = ", ".join(p for p in address_parts if p)

    # Underground — берём первую станцию из списка
    undergrounds = geo.get("undergrounds", [])
    if undergrounds:
        ug = undergrounds[0]
        metro           = ug.get("name")
        metro_time      = ug.get("travelTime")       # минуты
        metro_transport = ug.get("transportType")    # "walk" | "transport"
        walk_time       = metro_time if metro_transport == "walk" else None
    else:
        metro = metro_time = metro_transport = walk_time = None

    # Commission from bargainTerms (agentFee or clientFee, in percent)
    commission = bargain.get("agentFee") or bargain.get("clientFee") or 0

    # Photo
    photos = offer.get("photos", [])
    photo_url = photos[0].get("fullUrl") if photos else None

    # Flat details
    total_area = offer.get("totalArea")
    floor = offer.get("floorNumber")
    floors_total = offer.get("building", {}).get("floorsCount")

    # Coordinates — Cian может класть в "point" или "coordinates"
    point = geo.get("point") or geo.get("coordinates") or {}
    lat = point.get("lat")
    lng = point.get("lng")

    return {
        "id": offer.get("id"),
        "url": full_url,
        "price": price,
        "address": address,
        "metro": metro,
        "metro_time": metro_time,
        "metro_transport": metro_transport,
        "walk_time": walk_time,
        "commission": commission,
        "photo": photo_url,
        "area": total_area,
        "floor": floor,
        "floors_total": floors_total,
        "rooms": offer.get("roomsCount"),
        "description": (offer.get("description") or "")[:300],
        "lat": lat,
        "lng": lng,
    }


def load_cookies(session: requests.Session) -> None:
    """Load cookies from cookies.json and inject them into the session."""
    if not COOKIES_FILE.exists():
        log.warning("cookies.json not found at %s — skipping", COOKIES_FILE)
        return
    try:
        cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        if not isinstance(cookies, list):
            log.warning("cookies.json must contain a JSON array — skipping")
            return
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if name and value is not None:
                session.cookies.set(name, str(value), domain=c.get("domain", ".cian.ru"))
        log.info("Loaded %d cookies from %s", len(session.cookies), COOKIES_FILE)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read cookies.json: %s — skipping", e)


def scrape(max_pages: int = 5) -> list:
    session = requests.Session()
    session.headers.update(HEADERS)
    load_cookies(session)

    # Warm up the session: fetch the main page to obtain cookies Cian requires
    try:
        log.info("Warming up session (fetching main page for cookies) …")
        warm = session.get("https://www.cian.ru/", timeout=20)
        warm.raise_for_status()
        log.info("  → cookies obtained: %s", list(session.cookies.keys()))
    except requests.RequestException as e:
        log.warning("Could not warm up session: %s — proceeding anyway", e)

    all_offers = []

    for page in range(1, max_pages + 1):
        log.info("Fetching page %d / %d …", page, max_pages)
        data = fetch_page(session, page)

        if data is None:
            log.warning("Empty response on page %d, stopping.", page)
            break

        offers_raw = data.get("data", {}).get("offersSerialized", [])
        if not offers_raw:
            log.info("No offers on page %d, stopping.", page)
            break

        parsed = [parse_offer(o) for o in offers_raw]
        all_offers.extend(parsed)
        log.info("  → got %d offers (total so far: %d)", len(parsed), len(all_offers))

        # Polite delay to avoid rate-limiting
        if page < max_pages:
            delay = random.uniform(1.5, 3.0)
            time.sleep(delay)

    # Deduplicate by id, keeping first occurrence
    seen = set()
    unique_offers = []
    for o in all_offers:
        if o["id"] not in seen:
            seen.add(o["id"])
            unique_offers.append(o)
    removed = len(all_offers) - len(unique_offers)
    if removed:
        log.info("Removed %d duplicates, %d unique offers remain", removed, len(unique_offers))
    return unique_offers


def load_existing(path: Path) -> dict:
    """Load existing data.json, return dict with 'offers' list (or empty)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "offers" in data:
            return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read existing %s: %s — starting fresh", path, e)
    return {}


def merge_offers(existing: list, fresh: list) -> tuple[list, int]:
    """Merge fresh offers into existing, keyed by id. Returns (merged list, new count)."""
    index = {o["id"]: o for o in existing}
    new_count = 0
    for o in fresh:
        if o["id"] not in index:
            index[o["id"]] = o
            new_count += 1
    return list(index.values()), new_count


def main():
    log.info("Starting Cian parser — 1-2-3-room rentals in Moscow, no price limit")
    fresh = scrape(max_pages=20)

    if not fresh:
        log.error("No offers collected. Cian may have blocked the request or changed the API.")
        return

    existing_data = load_existing(OUTPUT_FILE)
    existing_offers = existing_data.get("offers", [])
    merged, new_count = merge_offers(existing_offers, fresh)

    log.info(
        "Merge: %d existing + %d fresh → %d new added, %d total unique",
        len(existing_offers), len(fresh), new_count, len(merged),
    )

    output = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(merged),
        "offers": merged,
    }

    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Saved %d offers to %s", len(merged), OUTPUT_FILE)


if __name__ == "__main__":
    main()
