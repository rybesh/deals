#! ./venv/bin/python3

import argparse
import os
import atoma
import html
import httpx
import logging
import re
import sys
from atoma.atom import AtomEntry, AtomFeed
from datetime import date, datetime, timezone
from feedgen.feed import FeedGenerator
from io import StringIO
from ratelimit import limits, sleep_and_retry
from rich.console import Console
from rich.padding import Padding
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from time import sleep
from tendo.singleton import SingleInstance, SingleInstanceException
from typing import Iterator, NamedTuple, Optional
from config import (
    ALLOW_VG,
    API,
    BLOCKED_SELLERS,
    CONDITIONS,
    CURRENCIES,
    DISCOGS_USER,
    FEED_AUTHOR,
    FEED_DISPLAY_WIDTH,
    FEED_URL,
    MAX_FEED_ENTRIES,
    STANDARD_SHIPPING,
    TIMEOUT,
    TOKEN,
    WWW,
)

GQLVariables = dict[str, str]


class Deal(NamedTuple):
    id: str
    title: str
    updated: datetime
    summary: str


class Benchmark(NamedTuple):
    price: float
    difference: int


class BenchmarkedPrice(NamedTuple):
    median: Benchmark
    suggested: Benchmark
    lowest: Benchmark
    highest: Benchmark


class DealException(Exception):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        json: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.json = json


def is_quiet(console: Console) -> bool:
    return type(console.file) is StringIO


def handle_deal_exception(
    console: Console, e: DealException, entry: Optional[AtomEntry] = None
) -> None:
    if entry is None:
        msg = str(e)
    else:
        msg = f"{entry.id_}\n" f"{entry.title.value}\n" f"{e}"

    if is_quiet(console):
        if e.status_code is None or e.status_code not in (404, 500, 502, 503):
            print(msg, file=sys.stderr)
            if e.json is not None:
                print(f"\n{e.json}", file=sys.stderr)
            print(file=sys.stderr)
    else:
        console.rule(style="red")
        console.print(f"[red]{msg}")
        if e.json is not None:
            console.print(Syntax(e.json, "json"))
        console.rule(style="red")


def handle_http_error(console: Console, e: httpx.HTTPError) -> None:
    console.print(f"[dim]{e}")


@sleep_and_retry
@limits(calls=1, period=1)
def call_public_api(client: httpx.Client, endpoint: str) -> dict:
    r = client.get(
        API + endpoint,
        headers={"Authorization": f"Discogs token={TOKEN}"},
        timeout=TIMEOUT,
    )
    calls_remaining = int(r.headers.get("X-Discogs-Ratelimit-Remaining", 0))
    if calls_remaining < 5:
        sleep(10)
    if r.status_code == 429:
        sleep(10)
    if not r.status_code == 200:
        raise DealException(f"GET {r.url} failed ({r.status_code})", r.status_code)
    return r.json()


@sleep_and_retry
@limits(calls=1, period=1)
def get(client: httpx.Client, url: str, params: Optional[dict] = None) -> str:
    if params is None:
        params = {}
    r = client.get(
        url,
        params=params,
        timeout=TIMEOUT,
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


def get_demand_ratio(release: dict) -> float:
    want = release["community"]["want"]
    have = release["community"]["have"]

    return want / (have if have > 0 else 1)


VALUE = r"(?:(?:\$((?:\d+,)*\d+\.\d{2}))|--)"

patterns = (
    re.compile(rf"Lowest<!-- -->:(?:</span>|</h4>)<span>{VALUE}</span>"),
    re.compile(rf"Median<!-- -->:(?:</span>|</h4>)<span>{VALUE}</span>"),
    re.compile(rf"Highest<!-- -->:(?:</span>|</h4>)<span>{VALUE}</span>"),
)


def float_value_of(price: str) -> float:
    return float(price.replace(",", ""))


def get_price_statistics(
    client: httpx.Client, release_id: str
) -> Optional[tuple[float, float, float]]:
    release_url = f"https://www.discogs.com/release/{release_id}"
    html = get(client, release_url)

    def extract(pattern: re.Pattern):
        m = pattern.search(html)
        if m is None:
            raise DealException(f"cannot find price statistics in {release_url}")
        return m.group(1)

    prices = tuple(extract(pattern) for pattern in patterns)

    if any(price is None for price in prices):
        return None  # not sold yet

    return (
        float_value_of(prices[0]),
        float_value_of(prices[1]),
        float_value_of(prices[2]),
    )


def get_release_year(listing: dict) -> Optional[int]:
    # Discogs API uses 0 for unknown year
    year = listing["release"].get("year", 0)
    return None if year == 0 else year


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


def now() -> datetime:
    return datetime.now(timezone.utc)


def summarize_difference(difference: int) -> str:
    if difference > 0:
        return f"{difference}% below"
    elif difference < 0:
        return f"{-difference}% above"
    else:
        return "same as"


def summarize_benchmarked_price(
    console: Console, benchmarked_price: Optional[BenchmarkedPrice]
) -> None:
    if benchmarked_price is None:
        grid = "[bold]never sold"
    else:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(justify="right", no_wrap=True)
        grid.add_column()
        grid.add_column(justify="right", no_wrap=True)

        for field in benchmarked_price._fields:
            benchmark = getattr(benchmarked_price, field)
            style = None
            if field == "median" and benchmark.difference >= 25:
                style = "bold magenta"
            grid.add_row(
                summarize_difference(benchmark.difference),
                f"{field}",
                f"${benchmark.price:.2f}",
                style=style,
            )

    console.print(Padding(grid, (1, 0)))


FORMAT = "<pre><code>{code}</code></pre>{foreground}{background}{stylesheet}"
SPAN_OPEN = re.compile(r'<span style="[^"]*">')


def export_html(console: Console) -> str:
    html = console.export_html(inline_styles=True, code_format=FORMAT)
    html = SPAN_OPEN.sub("<strong>", html)
    html = html.replace("</span>", "</strong>")
    html = html.replace("#000000#ffffff", "")
    return html


def abbreviate(condition: str) -> str:
    return list(CONDITIONS.keys())[list(CONDITIONS.values()).index(condition)]


def summarize(
    console: Console,
    status: Status,
    entry: AtomEntry,
    price: float,
    accepts_offers: bool,
    demand_ratio: float,
    seller_rating: float,
    release_year: Optional[int],
    condition: str,
    benchmarked_price: Optional[BenchmarkedPrice],
) -> str:
    console.print(Padding(f"[bold blue]{entry.title.value}", (1, 0)))

    status.stop()
    console.record = True

    if entry.summary is not None:
        console.print(html.unescape(entry.summary.value), width=FEED_DISPLAY_WIDTH)

    summarize_benchmarked_price(console, benchmarked_price)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right")
    grid.add_column()

    grid.add_row(
        "price",
        f"${price:.2f}{' [bold](offers)' if accepts_offers else ''}",
    )

    demand_ratio_text = Text(f"{demand_ratio:.1f}")
    if demand_ratio >= 2:
        demand_ratio_text.stylize("bold magenta")
    grid.add_row("demand", demand_ratio_text)

    seller_rating_text = Text(f"{seller_rating:.1f}")
    if seller_rating < 99.0:
        seller_rating_text.stylize("red")
    grid.add_row("rating", seller_rating_text)

    grid.add_row("year", "unknown" if release_year is None else str(release_year))
    grid.add_row("condition", abbreviate(condition))

    console.print(grid, width=FEED_DISPLAY_WIDTH)

    summary = export_html(console)
    console.record = False

    # if we're in (fake) quiet mode, clear the capture buffer
    if is_quiet(console):
        console.file.close()
        console.file = StringIO()

    status.start()

    console.print()
    console.print(f"[dim blue]{entry.id_}")
    if entry.updated is None:
        console.print()
    else:
        console.print(f"[dim]Listed {entry.updated:%B %-d, %Y %-I:%M%p}")
    console.print()

    return summary


def meets_criteria(
    seller: str,
    price: Optional[float],
    condition: str,
    release_age: int,
    release_genres: set[str],
    seller_rating: float,
) -> bool:
    if price is None or seller in BLOCKED_SELLERS:
        return False

    if condition == CONDITIONS["VG+"]:
        if seller_rating < ALLOW_VG["minimum_seller_rating"]:
            return False
        if (
            release_age < ALLOW_VG["minimum_age"]
            and len(release_genres.intersection(ALLOW_VG["genres"])) == 0
        ):
            return False

    return True


def get_deal(
    client: httpx.Client,
    console: Console,
    status: Status,
    entry: AtomEntry,
    release_id: str,
    price: float,
    accepts_offers: bool,
    condition: str,
    seller_rating: float,
    release_year: Optional[int],
    minimum_discount: int,
    skip_never_sold: bool,
    demand_ratio: float,
) -> Optional[Deal]:
    # adjust price for standard domestic shipping
    price = price - STANDARD_SHIPPING

    price_statistics = get_price_statistics(client, release_id)
    suggested_price = get_suggested_price(client, release_id, condition)

    if price_statistics is None or suggested_price is None:
        benchmarked_price = None
        if skip_never_sold:
            return None
    else:
        benchmarked_price = benchmark(price, suggested_price, *price_statistics)
        if benchmarked_price.median.difference < minimum_discount:
            return None

    summary = summarize(
        console,
        status,
        entry,
        price,
        accepts_offers,
        demand_ratio,
        seller_rating,
        release_year,
        condition,
        benchmarked_price,
    )

    return Deal(
        entry.id_,
        entry.title.value,
        entry.updated or now(),
        summary,
    )


def process_listing(
    client: httpx.Client,
    console: Console,
    status: Status,
    condition: str,
    minimum_discount: int,
    skip_never_sold: bool,
    entry: AtomEntry,
) -> Optional[Deal]:
    try:
        listing_id = entry.id_.split("/")[-1]
        listing = call_public_api(client, f"/marketplace/listings/{listing_id}")
        release_id = listing["release"]["id"]
        release = call_public_api(client, f"/releases/{release_id}")
        seller_rating = get_seller_rating(listing)
        price = get_total_price(listing)
        release_year = get_release_year(listing)
        release_genres = set(release["genres"])
        release_age = (
            ALLOW_VG["minimum_age"]
            if release_year is None
            else date.today().year - release_year
        )
        accepts_offers = listing.get("allow_offers", False)
        demand_ratio = get_demand_ratio(release)

        if meets_criteria(
            listing["seller"]["username"],
            price,
            condition,
            release_age,
            release_genres,
            seller_rating,
        ):
            assert price is not None

            return get_deal(
                client,
                console,
                status,
                entry,
                release_id,
                price,
                accepts_offers,
                condition,
                seller_rating,
                release_year,
                minimum_discount,
                skip_never_sold,
                demand_ratio,
            )

    except DealException as e:
        handle_deal_exception(console, e, entry)
    except httpx.HTTPError as e:
        handle_http_error(console, e)


def process_listings_feed(
    client: httpx.Client,
    console: Console,
    status: Status,
    condition: str,
    minimum_discount: int,
    skip_never_sold: bool,
    since: Optional[datetime],
    feed: AtomFeed,
) -> Iterator[Deal]:
    for entry in feed.entries:
        if since is not None and entry.updated is not None and entry.updated <= since:
            continue
        result = process_listing(
            client, console, status, condition, minimum_discount, skip_never_sold, entry
        )
        if result is not None:
            yield result


def get_deals(
    client: httpx.Client,
    console: Console,
    conditions: list[str],
    currencies: list[str],
    minimum_discount: int,
    complete: bool,
    skip_never_sold: bool,
    since: Optional[datetime],
) -> Iterator[Deal]:
    status = None

    for condition in conditions:
        for currency in currencies:
            status_message = (
                f"[blue]Checking {currency} listings in {condition} condition..."
            )
            if status is None:
                status = console.status(status_message)
                status.start()
            else:
                status.update(status_message)

            wantlist_url = f"{WWW}/sell/mpmywantsrss"
            wantlist_params = {
                "output": "rss",
                "user": DISCOGS_USER,
                "condition": condition,
                "currency": currency,
                "limit": "250",
                "sort": "listed,desc",
            }
            if not complete:
                wantlist_params["hours_range"] = "0-12"

            page = 0
            while True:
                page += 1
                wantlist_params["page"] = str(page)
                try:
                    feed = atoma.parse_atom_bytes(
                        get(client, wantlist_url, wantlist_params).encode("utf8")
                    )
                    if len(feed.entries) == 0:
                        break
                    else:
                        for result in process_listings_feed(
                            client,
                            console,
                            status,
                            condition,
                            minimum_discount,
                            skip_never_sold,
                            since,
                            feed,
                        ):
                            yield result

                except DealException as e:
                    handle_deal_exception(console, e)
                except httpx.HTTPError as e:
                    handle_http_error(console, e)


def condition(arg: str) -> list[str]:
    if arg == "all":
        return list(CONDITIONS.values())
    if "," in arg:
        args = arg.split(",")
    else:
        args = [arg]
    for arg in args:
        if arg not in CONDITIONS:
            raise argparse.ArgumentTypeError(
                "condition must be one or more of: %s" % list(CONDITIONS.keys())
            )
    return [CONDITIONS[arg] for arg in args]


def currency(arg: str) -> list[str]:
    if arg == "all":
        return CURRENCIES
    if "," in arg:
        args = arg.split(",")
    else:
        args = [arg]
    for arg in args:
        if arg not in CURRENCIES:
            raise argparse.ArgumentTypeError(
                "currency must be one or more of: %s" % CURRENCIES
            )
    return args


def copy_entry(entry: AtomEntry, fg: FeedGenerator) -> None:
    fe = fg.add_entry(order="append")
    fe.id(entry.id_)
    fe.title(entry.title.value)
    fe.updated(isoformat(entry.updated or now()))
    fe.link(href=entry.id_)
    if entry.content is not None:
        fe.content(entry.content.value, type="html")


def copy_remaining_entries(
    feed: Optional[AtomFeed], fg: FeedGenerator, feed_entries: int, deal_ids: set[str]
) -> None:
    if feed is not None:
        for entry in feed.entries:
            if feed_entries < MAX_FEED_ENTRIES:
                if entry.id_ not in deal_ids:
                    deal_ids.add(entry.id_)
                    copy_entry(entry, fg)
                    feed_entries += 1
            else:
                break


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--condition",
        help="check for deals on items in this condition",
        type=condition,
        default="all",
    )
    parser.add_argument(
        "-$",
        "--currency",
        help="check for deals on items priced in this currency",
        type=currency,
        default="all",
    )
    parser.add_argument(
        "-m",
        "--minimum-discount",
        help="only show items discounted at least this much",
        type=int,
        default=20,
    )
    parser.add_argument(
        "-f",
        "--feed",
        help="generate an Atom feed (or update it if it exists)",
    )
    parser.add_argument(
        "-x",
        "--complete",
        help="check all listings, not only new ones",
        action="store_true",
    )
    parser.add_argument(
        "-s",
        "--skip-never-sold",
        help="skip items that have never been sold",
        action="store_true",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        help="write nothing to the console (except errors)",
        action="store_true",
    )

    fg = None
    feed = None
    feed_entries = 0
    deal_ids = set()
    last_updated = None

    args = parser.parse_args()

    if args.quiet:
        console = Console(file=StringIO())
    else:
        console = Console()

    if args.feed is not None:
        if os.path.exists(args.feed):
            feed = atoma.parse_atom_file(args.feed)
            for entry in feed.entries:
                if entry.updated is not None and (
                    last_updated is None or entry.updated > last_updated
                ):
                    last_updated = entry.updated

        fg = FeedGenerator()
        fg.id(FEED_URL)
        fg.title("Discogs Deals")
        fg.updated(now())
        fg.link(href=FEED_URL, rel="self")
        fg.author(FEED_AUTHOR)

    with httpx.Client() as client:
        for deal in get_deals(
            client,
            console,
            args.condition,
            args.currency,
            args.minimum_discount,
            args.complete,
            args.skip_never_sold,
            last_updated,
        ):
            if fg is not None and feed_entries < MAX_FEED_ENTRIES:
                if deal.id not in deal_ids:
                    deal_ids.add(deal.id)
                    fe = fg.add_entry(order="append")
                    fe.id(deal.id)
                    fe.title(deal.title)
                    fe.updated(isoformat(deal.updated))
                    fe.link(href=deal.id)
                    fe.content(deal.summary, type="html")
                    feed_entries += 1

    if fg is not None:
        copy_remaining_entries(feed, fg, feed_entries, deal_ids)
        fg.atom_file(f"{args.feed}.new", pretty=True)
        os.rename(f"{args.feed}.new", args.feed)


if __name__ == "__main__":
    try:
        logger = logging.getLogger("tendo.singleton")
        logger.setLevel(logging.CRITICAL)
        me = SingleInstance()
        main()
    except SingleInstanceException:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
