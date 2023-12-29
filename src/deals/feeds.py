import sys

from atoma import parse_atom_bytes
from atoma.atom import AtomEntry, AtomFeed
from datetime import datetime, timezone, timedelta
from httpx import Client, HTTPError
from ratelimit import limits, sleep_and_retry
from rich.console import Console
from time import sleep
from typing import Iterator

from .config import config

WWW_ROOT = "https://www.discogs.com"


class FeedException(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code


class Feeds:
    def __init__(self, client: Client, console: Console):
        self.client = client
        self.console = console

    def _handle_feed_exception(self, e: FeedException) -> None:
        self.console.rule(style="red")
        self.console.print(f"[red]{e}")
        self.console.rule(style="red")

    @sleep_and_retry
    @limits(calls=1, period=1)
    def get(self, url: str, params: dict | None = None) -> AtomFeed:
        if not url.startswith(WWW_ROOT):
            url = WWW_ROOT + url

        attempts_remaining = 5

        r = None
        while attempts_remaining > 0:
            try:
                r = self.client.get(
                    url,
                    params=params,
                    timeout=config.TIMEOUT,
                )

                if r.status_code == 200:
                    return parse_atom_bytes(r.text.encode("utf8"))

            except HTTPError as e:
                self.console.print(f"[dim]{e}")

            attempts_remaining -= 1
            sleep(10)

        if r is None:
            raise FeedException(f"GET {url} failed")
        else:
            raise FeedException(f"GET {r.url} failed ({r.status_code})", r.status_code)

    def paginate_feed(
        self,
        url: str,
        params: dict | None = None,
        first_page: int = 1,
        since: datetime | None = None,
    ) -> Iterator[AtomEntry]:
        params = (params or {}) | {
            "page": first_page,
            "limit": 250,
            "sort": "listed,asc",
        }

        exceptions_handled = 0
        while exceptions_handled <= 1:
            try:
                feed = self.get(url, params)

                if len(feed.entries) == 0:
                    done = True
                    break

                done = False

                for entry in feed.entries:
                    if (
                        since is not None
                        and entry.updated is not None
                        and entry.updated < since
                    ):
                        done = True
                        break
                    yield entry

                if done:
                    break
                else:
                    params["page"] += 1

            except FeedException as e:
                self._handle_feed_exception(e)
                exceptions_handled += 1
                sleep(10)

    def listings_for_release(
        self,
        release_id: int,
        since: datetime | None = None,
    ) -> Iterator[AtomEntry]:
        for entry in self.paginate_feed(
            "/sell/mplistrss", {"release_id": release_id}, since=since
        ):
            yield entry


def main() -> None:
    console = Console()
    with Client() as client:
        feeds = Feeds(client, console)
        for i, entry in enumerate(
            feeds.listings_for_release(
                152946, datetime.now(timezone.utc) - timedelta(days=7)
            )
        ):
            console.rule(style="gray")
            console.print(i)
            console.print(entry.id_)
            console.print(entry.title.value)
            console.print(entry.updated)
            if entry.summary is not None:
                console.print(entry.summary.value)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
