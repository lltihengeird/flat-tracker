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

# Moscow region ID = 1, 1-room flat, rent, max 200 000 RUB
def build_query(page: int) -> dict:
    return {
        "jsonQuery": {
            "_type": "flatrent",
            "engine_version": {"type": "term", "value": 2},
            "region": {"type": "terms", "value": [1]},
            "room": {"type": "terms", "value": [1]},
            "price": {"type": "range", "value": {"lte": 200000}},
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

    # Underground
    undergrounds = geo.get("undergrounds", [])
    metro = undergrounds[0].get("name") if undergrounds else None

    # Photo
    photos = offer.get("photos", [])
    photo_url = photos[0].get("fullUrl") if photos else None

    # Flat details
    total_area = offer.get("totalArea")
    floor = offer.get("floorNumber")
    floors_total = offer.get("building", {}).get("floorsCount")

    # Coordinates
    coordinates = geo.get("coordinates") or {}
    lat = coordinates.get("lat")
    lng = coordinates.get("lng")

    return {
        "id": offer.get("id"),
        "url": full_url,
        "price": price,
        "address": address,
        "metro": metro,
        "photo": photo_url,
        "area": total_area,
        "floor": floor,
        "floors_total": floors_total,
        "rooms": offer.get("roomsCount"),
        "description": (offer.get("description") or "")[:300],
        "lat": lat,
        "lng": lng,
    }


def scrape(max_pages: int = 5) -> list:
    session = requests.Session()
    session.headers.update(HEADERS)

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

    return all_offers


def main():
    log.info("Starting Cian parser — 1-room rentals in Moscow up to 200 000 RUB")
    offers = scrape(max_pages=5)

    if not offers:
        log.error("No offers collected. Cian may have blocked the request or changed the API.")
        return

    output = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(offers),
        "offers": offers,
    }

    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Saved %d offers to %s", len(offers), OUTPUT_FILE)


if __name__ == "__main__":
    main()
