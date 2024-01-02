import argparse
import os
import logging
import pickle
import sys
import re

from atoma import parse_atom_file
from atoma.atom import AtomEntry, AtomFeed
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone, timedelta
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from httpx import Client
from io import StringIO
from tendo.singleton import SingleInstance, SingleInstanceException
from time import time
from typing import Iterator, Any

from . import wantlist
from .feeds import Feeds
from .api import API, Listing
from .config import config
from .criteria import meets_criteria, BLOCKED_SELLERS

PROGRESS_FILENAME = "progress.pickle"
FORMAT = "<pre><code>{code}</code></pre>{foreground}{background}{stylesheet}"
SPAN_OPEN = re.compile(r'<span style="[^"]*">')


def log(x: Any) -> None:
    print(x, file=sys.stderr)


def isoformat(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def now() -> datetime:
    return datetime.now(timezone.utc)


def is_quiet(console: Console) -> bool:
    return type(console.file) is StringIO


def export_html(console: Console) -> str:
    html = console.export_html(inline_styles=True, code_format=FORMAT)
    html = SPAN_OPEN.sub("<strong>", html)
    html = html.replace("</span>", "</strong>")
    html = html.replace("#000000#ffffff", "")
    return html


def summarize_discount(discount: int) -> str:
    if discount > 0:
        return f"{discount}% below"
    elif discount < 0:
        return f"{-discount}% above"
    else:
        return "same as"


def summarize_price(console: Console, listing: Listing) -> None:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column()
    grid.add_column(justify="right", no_wrap=True)

    discount = listing.get_discount()

    style = None
    if discount >= 25:
        style = "bold magenta"

    grid.add_row(
        f"{summarize_discount(discount)} suggested price",
        f"${listing.get_suggested_price():.2f}",
        style=style,
    )

    console.print(Padding(grid, (1, 0)))


def summarize(
    console: Console,
    listing: Listing,
) -> str:
    console.print(
        Padding(
            f"[bold blue]{listing.release.get_description()}",
            (1, 0),
        )
    )

    console.record = True

    console.print(
        f"{listing.seller.username} - {listing.comments}",
        width=config.FEED_DISPLAY_WIDTH,
    )

    summarize_price(console, listing)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right")
    grid.add_column()

    grid.add_row(
        "price",
        f"${listing.price:.2f}{' [bold](offers)' if listing.allow_offers else ''}",
    )
    grid.add_row(
        "shipping",
        f"${listing.shipping_price:.2f}",
    )

    demand_ratio = listing.release.want / (
        listing.release.have if listing.release.have > 0 else 1
    )
    demand_ratio_text = Text(f"{demand_ratio:.1f}")
    if demand_ratio >= 2:
        demand_ratio_text.stylize("bold magenta")
    grid.add_row("demand", demand_ratio_text)

    seller_rating_text = Text(f"{listing.seller.rating:.1f}")
    if listing.seller.rating < 99.0:
        seller_rating_text.stylize("red")
    grid.add_row("rating", seller_rating_text)

    grid.add_row(
        "year", "unknown" if listing.release.year is None else str(listing.release.year)
    )
    grid.add_row("condition", listing.condition.name)
    if listing.sleeve_condition is not None:
        grid.add_row("sleeve condition", listing.sleeve_condition.name)

    console.print(grid, width=config.FEED_DISPLAY_WIDTH)

    summary = export_html(console)
    console.record = False

    # if we're in (fake) quiet mode, clear the capture buffer
    if is_quiet(console):
        console.file.close()
        console.file = StringIO()

    console.print()
    console.print(f"[dim blue]{listing.uri}")
    console.print(f"[dim]Listed {listing.posted:%B %-d, %Y %-I:%M%p}")
    console.print()

    return summary


def load_last_release_id() -> int:
    try:
        with open(PROGRESS_FILENAME, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return 0


def save_last_release_id(release_id: int) -> None:
    with open(PROGRESS_FILENAME, "wb") as f:
        pickle.dump(release_id, f, pickle.HIGHEST_PROTOCOL)


def get_listings(
    client: Client, console: Console, since: datetime, minutes: int | None
) -> Iterator[Listing]:
    api = API(client, console)
    feeds = Feeds(client, console)
    last_release_id = load_last_release_id()
    log(f"Checking wantlist starting with release {last_release_id}...")
    try:
        start = time()
        for want in wantlist.get(api):
            if want.release.id <= last_release_id:
                continue
            with console.status(f"[dim]{want.release.get_description()}") as status:
                for entry in feeds.listings_for_release(want.release.id, since):
                    assert entry.summary is not None
                    listing_id = int(entry.id_.split("/")[-1])
                    seller_username = entry.summary.value.split(" - ")[1]
                    if seller_username not in BLOCKED_SELLERS:
                        try:
                            listing = api.fetch_listing(listing_id, want.release)
                            if meets_criteria(listing):
                                status.stop()
                                yield listing
                                status.start()
                            else:
                                console.print(f"[dim]Rejected listing {listing.id}")
                        except ValueError as e:
                            console.print(f"[dim]{e}")
            last_release_id = want.release.id
            elapsed = time() - start
            if minutes and (elapsed / 60 > minutes):
                break
        # finished iterating wants; reset last release ID to 0
        log("Finished checking wantlist")
        last_release_id = 0
    finally:
        save_last_release_id(last_release_id)
        log(f"Stopping; next time we'll start with release {last_release_id}")


def copy_entry(entry: AtomEntry, fg: FeedGenerator) -> None:
    fe = fg.add_entry(order="append")
    fe.id(entry.id_)
    fe.title(entry.title.value)
    fe.updated(isoformat(entry.updated or now()))
    fe.link(href=entry.id_)
    if entry.content is not None:
        fe.content(entry.content.value, type="html")


def copy_remaining_entries(
    feed: AtomFeed | None, fg: FeedGenerator, feed_entries: int, deal_ids: set[str]
) -> None:
    if feed is not None:
        for entry in feed.entries:
            if feed_entries < config.MAX_FEED_ENTRIES:
                if entry.id_ not in deal_ids:
                    deal_ids.add(entry.id_)
                    copy_entry(entry, fg)
                    feed_entries += 1
            else:
                break


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-q",
        "--quiet",
        help="write nothing to the console",
        action="store_true",
    )
    parser.add_argument(
        "-f",
        "--feed",
        help="generate an Atom feed (or update it if it exists)",
    )
    parser.add_argument(
        "-m",
        "--minutes",
        type=int,
        help="number of minutes to run before exiting",
    )

    fg = None
    feed = None
    feed_entries = 0
    listing_ids = set()
    last_updated = None

    args = parser.parse_args()

    if args.quiet:
        console = Console(file=StringIO())
    else:
        console = Console()

    if args.feed is not None:
        if os.path.exists(args.feed):
            feed = parse_atom_file(args.feed)
            for entry in feed.entries:
                if entry.updated is not None and (
                    last_updated is None or entry.updated > last_updated
                ):
                    last_updated = entry.updated

        fg = FeedGenerator()
        fg.id(config.FEED_URL)
        fg.title("Discogs Deals")
        fg.updated(now())
        fg.link(href=config.FEED_URL, rel="self")
        fg.author({"name": config.FEED_AUTHOR_NAME, "email": config.FEED_AUTHOR_EMAIL})

    try:
        with Client() as client:
            for listing in get_listings(
                client,
                console,
                since=(last_updated or (now() - timedelta(days=1))),
                minutes=args.minutes,
            ):
                summary = f'<img src="{listing.release.thumbnail}"/>\n' + summarize(
                    console, listing
                )

                if fg is not None and feed_entries < config.MAX_FEED_ENTRIES:
                    if listing.id not in listing_ids:
                        listing_ids.add(listing.id)
                        fe = fg.add_entry(order="append")
                        fe.id(f"https://www.discogs.com/sell/item/{listing.id}")
                        fe.title(listing.release.get_description())
                        fe.updated(isoformat(listing.posted))
                        fe.link(href=listing.uri)
                        fe.content(summary, type="html")
                        feed_entries += 1

    finally:
        if fg is not None:
            log(f"Added {feed_entries} new items to feed")
            copy_remaining_entries(feed, fg, feed_entries, listing_ids)
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
