#! ./venv/bin/python3

import argparse
from typing import Any, Iterator, NamedTuple, Optional
from atoma.atom import AtomEntry
import httpx
import atoma
import json
from sys import stderr
from time import sleep
from datetime import date, datetime, timezone
from feedgen.feed import FeedGenerator
from ratelimit import limits, sleep_and_retry
from config import (
    ALLOW_VG,
    API,
    BLOCKED_SELLERS,
    GQL_API,
    CONDITIONS,
    CURRENCIES,
    DEBUG,
    DISCOGS_USER,
    FEED_AUTHOR,
    FEED_URL,
    MARKETPLACE_QUERY_HASH,
    STANDARD_SHIPPING,
    TOKEN,
    WWW,
)

Deal = dict[str, str]
GQLVariables = dict[str, str]


class Benchmark(NamedTuple):
    price: float
    difference: int


class BenchmarkedPrice(NamedTuple):
    median: Benchmark
    suggested: Benchmark
    lowest: Benchmark
    highest: Benchmark


class DealException(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def log_error(e: DealException, entry: Optional[AtomEntry] = None):
    if entry is None:
        msg = str(e)
    else:
        msg = f"{entry.id_}\n" f"{entry.title.value}\n" f"{e}\n"
    if e.status_code not in (404, 500, 502, 503):
        print(msg, file=stderr)


def debug(x: Any, noend: bool = False):
    if DEBUG:
        if noend:
            print(x, file=stderr, end="", flush=True)
        else:
            print(x, file=stderr)


@sleep_and_retry
@limits(calls=1, period=1)
def call_public_api(client: httpx.Client, endpoint: str) -> dict:
    r = client.get(
        API + endpoint,
        headers={"Authorization": f"Discogs token={TOKEN}"},
        timeout=10.0,
    )
    calls_remaining = int(r.headers.get("X-Discogs-Ratelimit-Remaining", 0))
    # debug(f"{calls_remaining} calls remaining")
    debug(".", noend=True)
    if calls_remaining < 5:
        # debug("sleeping...")
        sleep(10)
    if not r.status_code == 200:
        raise DealException(f"GET {r.url} failed ({r.status_code})", r.status_code)
    return r.json()


def dump(o: dict) -> str:
    return json.dumps(o, separators=(",", ":"))


@sleep_and_retry
@limits(calls=1, period=1)
def call_graphql_api(
    client: httpx.Client, operation: str, variables: GQLVariables, extensions: dict
) -> dict:
    r = client.get(
        GQL_API,
        params={
            "operationName": operation,
            "variables": dump(variables),
            "extensions": dump(extensions),
        },
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_1_0) "
                + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 "
                + "Safari/537.36"
            ),
            "Origin": "https://www.discogs.com",
            "Referer": "https://www.discogs.com/",
        },
        timeout=10.0,
    )
    if not r.status_code == 200:
        raise DealException(f"GET {r.url} failed ({r.status_code})", r.status_code)
    return r.json()


def get(client: httpx.Client, url: str, params: Optional[dict] = None) -> str:
    if params is None:
        params = {}

    r = client.get(
        url,
        params=params,
        timeout=10.0,
    )
    if not r.status_code == 200:
        raise DealException(f"GET {r.url} failed ({r.status_code})", r.status_code)
    return r.text


def get_seller_rating(listing: dict) -> float:
    return float(listing["seller"]["stats"].get("rating", "0.0"))


def get_total_price(listing: dict) -> Optional[float]:
    price = listing["price"].get("value")
    shipping = listing["shipping_price"].get("value")
    if price and shipping:
        return price + shipping
    else:
        return None


def get_suggested_price(
    client: httpx.Client, release_id: str, condition: str
) -> Optional[float]:
    suggestions = call_public_api(
        client, f"/marketplace/price_suggestions/{release_id}"
    )
    return suggestions.get(condition, {}).get("value")


def get_demand_ratio(client: httpx.Client, release_id: str) -> float:
    o = call_public_api(client, f"/releases/{release_id}")
    want = o["community"]["want"]
    have = o["community"]["have"]

    return want / (have if have > 0 else 1)


def get_price_statistics(
    client: httpx.Client, release_id: str
) -> Optional[tuple[float, float, float]]:
    o = call_graphql_api(
        client,
        "ReleaseMarketplaceData",
        {"discogsId": release_id, "currency": "USD"},
        {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": MARKETPLACE_QUERY_HASH,
            }
        },
    )

    def error(message: str):
        j = json.dumps(o, indent=2, sort_keys=True)
        raise DealException(f"{message}\n\n{j}\n")

    data = o.get("data", {})
    if data is None:
        error("missing data value")

    release = data.get("release", {})
    if release is None:
        error("missing release value")

    statistics = release.get("statistics", {})

    if any(stat not in statistics for stat in ("min", "median", "max")):
        error("missing price statistics")

    prices = [statistics[x] for x in ("min", "median", "max")]

    if any(price is None for price in prices):
        return None  # not sold yet

    def get_amount(price: dict) -> float:
        amount = price.get("converted", {}).get("amount")
        if amount is None:
            error("missing amount")
        return float(amount)

    return (get_amount(prices[0]), get_amount(prices[1]), get_amount(prices[2]))


def get_release_year(listing: dict) -> int:
    # Discogs API uses 0 for unknown year
    year = listing["release"].get("year", 0)
    return date.today().year if year == 0 else year


def difference(price: float, benchmark: float) -> Benchmark:
    return Benchmark(benchmark, round((benchmark - price) / benchmark * 100))


def benchmark(
    price: float, suggested: float, min: float, median: float, max: float
) -> BenchmarkedPrice:
    return BenchmarkedPrice(
        difference(price, median),
        difference(price, suggested),
        difference(price, min),
        difference(price, max),
    )


def isoformat(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def now() -> str:
    return isoformat(datetime.now(timezone.utc))


def summarize_difference(difference: int):
    if difference > 0:
        return f"{difference}% below"
    elif difference < 0:
        return f"{-difference}% above"
    else:
        return "same as"


def summarize_benchmarked_price(benchmarked_price: BenchmarkedPrice) -> str:
    summary = ""
    for field in benchmarked_price._fields:
        benchmark = getattr(benchmarked_price, field)
        s = (
            f"{summarize_difference(benchmark.difference)}"
            f" {field} price (${benchmark.price:.2f})"
        )
        debug(s)
        summary += f"<b>{s}</b><br>"
    debug("")
    return summary


def summarize(
    title: str,
    description: str,
    price: float,
    demand_ratio: float,
    seller_rating: float,
    release_year: int,
    benchmarked_price: Optional[BenchmarkedPrice],
) -> str:
    debug(
        f"\n\n{title}\n"
        f"{description}\n"
        f"price: ${price:.2f}\n"
        f"demand ratio: {demand_ratio:.1f}\n"
        f"seller rating: {seller_rating:.1f}\n"
        f"release year: {release_year}"
    )
    if benchmarked_price is None:
        debug("never sold\n")
        summary = "<b>never sold</b><br>"
    else:
        summary = summarize_benchmarked_price(benchmarked_price)
    summary += f"demand ratio: {demand_ratio:.1f}<br><br>" f"{description}"
    return summary


def meets_criteria(
    seller: str,
    price: Optional[float],
    condition: str,
    release_age: int,
    seller_rating: float,
) -> bool:
    if price is None or seller in BLOCKED_SELLERS:
        return False

    if condition == CONDITIONS["VG+"]:
        if (
            release_age < ALLOW_VG["minimum_age"]
            or seller_rating < ALLOW_VG["minimum_seller_rating"]
        ):
            return False

    return True


def get_deal(
    client: httpx.Client,
    entry: AtomEntry,
    release_id: str,
    price: float,
    condition: str,
    seller_rating: float,
    release_year: int,
    minimum_discount: int,
) -> Optional[Deal]:

    # adjust price for standard domestic shipping
    price = price - STANDARD_SHIPPING

    price_statistics = get_price_statistics(client, release_id)
    suggested_price = get_suggested_price(client, release_id, condition)
    demand_ratio = get_demand_ratio(client, release_id)

    if price_statistics is None or suggested_price is None:
        benchmarked_price = None
    else:
        benchmarked_price = benchmark(price, suggested_price, *price_statistics)
        minimum = minimum_discount if demand_ratio < 2 else 5
        if benchmarked_price.median.difference < minimum:
            return None

    summary = summarize(
        entry.title.value,
        entry.summary.value,
        price,
        demand_ratio,
        seller_rating,
        release_year,
        benchmarked_price,
    )

    return {
        "id": entry.id_,
        "title": entry.title.value,
        "updated": isoformat(entry.updated) if entry.updated is not None else now(),
        "summary": summary,
    }


def process_listing(
    client: httpx.Client, condition: str, minimum_discount: int, entry: AtomEntry
) -> Optional[Deal]:
    try:
        listing_id = entry.id_.split("/")[-1]
        listing = call_public_api(client, f"/marketplace/listings/{listing_id}")
        release_id = listing["release"]["id"]
        seller_rating = get_seller_rating(listing)
        price = get_total_price(listing)
        release_year = get_release_year(listing)
        release_age = date.today().year - release_year

        if meets_criteria(
            listing["seller"]["username"], price, condition, release_age, seller_rating
        ):
            assert price is not None

            return get_deal(
                client,
                entry,
                release_id,
                price,
                condition,
                seller_rating,
                release_year,
                minimum_discount,
            )

    except DealException as e:
        log_error(e, entry)
    except httpx.HTTPError as e:
        debug(e)


def get_deals(
    client: httpx.Client,
    conditions: list[str],
    currencies: list[str],
    minimum_discount: int,
) -> Iterator[Deal]:

    for condition in conditions:
        for currency in currencies:

            wantlist_url = f"{WWW}/sell/mpmywantsrss"
            wantlist_params = {
                "output": "rss",
                "user": DISCOGS_USER,
                "condition": condition,
                "currency": currency,
                "hours_range": "0-12",
            }
            feed = atoma.parse_atom_bytes(
                get(client, wantlist_url, wantlist_params).encode("utf8")
            )

            for entry in feed.entries:
                result = process_listing(client, condition, minimum_discount, entry)
                if result is not None:
                    yield result


def condition(arg: str) -> list[str]:
    if arg == "all":
        return list(CONDITIONS.values())
    if arg not in CONDITIONS:
        raise argparse.ArgumentTypeError(
            "condition must be one of: %s" % list(CONDITIONS.keys())
        )
    return [CONDITIONS[arg]]


def currency(arg: str) -> list[str]:
    if arg == "all":
        return CURRENCIES
    if arg not in CURRENCIES:
        raise argparse.ArgumentTypeError("currency must be one of: %s" % CURRENCIES)
    return [arg]


parser = argparse.ArgumentParser()
parser.add_argument("condition", type=condition)
parser.add_argument("currency", type=currency)
parser.add_argument("minimum_discount", type=int)
parser.add_argument("outfile")

args = parser.parse_args()

try:
    fg = FeedGenerator()
    fg.id(FEED_URL)
    fg.title("Discogs Deals")
    fg.updated(now())
    fg.link(href=FEED_URL, rel="self")
    fg.author(FEED_AUTHOR)

    with httpx.Client() as client:

        for deal in get_deals(
            client, args.condition, args.currency, args.minimum_discount
        ):
            fe = fg.add_entry()
            fe.id(deal["id"])
            fe.title(deal["title"])
            fe.updated(deal["updated"])
            fe.link(href=deal["id"])
            fe.content(deal["summary"], type="html")

    fg.atom_file(args.outfile, pretty=True)

except DealException as e:
    log_error(e)
except httpx.HTTPError as e:
    debug(e)
