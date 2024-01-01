import re
import sys

from datetime import date, datetime
from enum import Enum
from httpx import Client, HTTPError
from operator import attrgetter
from ratelimit import limits, sleep_and_retry
from rich.console import Console
from rich.progress import track
from time import sleep
from typing import Any, Iterator, NamedTuple

from .config import config

API_ROOT = "https://api.discogs.com"

UNIQUE_NUM = re.compile(r" \(\d+\)$")
BY_NAME = attrgetter("name")


def log(x: Any) -> None:
    print(x, file=sys.stderr)


class Condition(Enum):
    P = "Poor (P)"
    F = "Fair (F)"
    G = "Good (G)"
    GP = "Good Plus (G+)"
    VG = "Very Good (VG)"
    VGP = "Very Good Plus (VG+)"
    NM = "Near Mint (NM or M-)"
    M = "Mint (M)"


class Artist(NamedTuple):
    id: int
    name: str

    @classmethod
    def from_json(cls, o: dict):
        return cls(o["id"], UNIQUE_NUM.sub("", o["name"]))


class Label(NamedTuple):
    id: int
    name: str

    @classmethod
    def from_json(cls, o: dict):
        return cls(o["id"], UNIQUE_NUM.sub("", o["name"]))


class Release(NamedTuple):
    id: int
    master_id: int | None
    title: str
    artists: list[Artist]
    formats: list[str]
    labels: dict[Label, str | None]
    thumbnail: str
    genres: set[str]
    year: int | None
    country: str | None
    have: int
    want: int
    price_suggestions: dict[Condition, float]

    @classmethod
    def from_json(cls, o: dict, price_suggestions: dict[Condition, float]):
        return Release(
            o["id"],
            o.get("master_id"),
            o["title"],
            sorted({Artist.from_json(d) for d in o["artists"]}, key=BY_NAME),
            sorted({f["name"] for f in o["formats"]}),
            {Label.from_json(d): d.get("catno") for d in o["labels"]},
            o["thumb"],
            set(o["genres"]),
            o.get("year"),
            o.get("country"),
            o["community"]["have"],
            o["community"]["want"],
            price_suggestions,
        )

    def get_age(self) -> int | None:
        return None if self.year is None else date.today().year - self.year

    def get_description(self) -> str:
        return f"{' | '.join([a.name for a in self.artists])} - {self.title}"

    def in_genres(self, genres: set[str]) -> bool:
        return len(self.genres.intersection(genres)) > 0


class Seller(NamedTuple):
    username: str
    rating: float

    @classmethod
    def from_json(cls, o: dict):
        return cls(o["username"], float(o["stats"]["rating"]))


class Listing(NamedTuple):
    id: int
    seller: Seller
    release: Release
    price: float
    shipping_price: float
    allow_offers: bool
    condition: Condition
    sleeve_condition: Condition | None
    posted: datetime
    ships_from: str
    uri: str
    comments: str

    @classmethod
    def from_json(cls, o: dict, release: Release):
        try:
            sleeve_condition = Condition(o["sleeve_condition"])
        except ValueError:
            sleeve_condition = None

        try:
            shipping_price = o["shipping_price"]["value"]
        except KeyError as e:
            raise ValueError(f"Rejected listing {o['id']}") from e

        return cls(
            o["id"],
            Seller.from_json(o["seller"]),
            release,
            o["price"]["value"],
            shipping_price,
            o["allow_offers"],
            Condition(o["condition"]),
            sleeve_condition,
            datetime.fromisoformat(o["posted"]),
            o["ships_from"],
            o["uri"],
            o["comments"],
        )

    def get_adjusted_price(self) -> float:
        return (self.price + self.shipping_price) - config.STANDARD_SHIPPING

    def get_suggested_price(self) -> float:
        suggested_price = self.release.price_suggestions.get(self.condition)
        if suggested_price is None:
            return self.get_adjusted_price()
        else:
            return suggested_price

    def get_discount(self) -> int:
        adjusted_price = self.get_adjusted_price()
        suggested_price = self.get_suggested_price()
        return round((suggested_price - adjusted_price) / suggested_price * 100)


class WantlistItem(NamedTuple):
    release: Release
    date_added: datetime
    notes: str


class APIException(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class API:
    def __init__(self, client: Client, console: Console):
        self.client = client
        self.console = console

    def _handle_api_exception(self, e: APIException) -> None:
        log(e)
        self.console.rule(style="red")
        self.console.print(f"[red]{e}")
        if e.details is not None:
            self.console.print(e.details)
        self.console.rule(style="red")

    @sleep_and_retry
    @limits(calls=1, period=1)
    def call(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        if not endpoint.startswith(API_ROOT):
            endpoint = API_ROOT + endpoint

        attempts_remaining = 5

        r = None
        while attempts_remaining > 0:
            try:
                r = self.client.get(
                    endpoint,
                    params=params,
                    headers={"Authorization": f"Discogs token={config.TOKEN}"},
                    timeout=config.TIMEOUT,
                )

                calls_remaining = int(r.headers.get("X-Discogs-Ratelimit-Remaining", 0))
                if calls_remaining < 2:
                    sleep(10)

                if r.status_code == 200:
                    return r.json()

            except HTTPError as e:
                self.console.print(f"[dim]{e}")

            attempts_remaining -= 1
            sleep(10)

        if r is None:
            raise APIException(f"GET {endpoint} failed")
        else:
            raise APIException(
                f"GET {r.url} failed ({r.status_code})", r.status_code, r.text
            )

    def paginate_api_results(
        self,
        endpoint: str,
        params: dict | None = None,
        first_page: int = 1,
        stop_after_page: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        params = (params or {}) | {"page": first_page, "per_page": 100}

        exceptions_handled = 0
        while exceptions_handled <= 5:
            try:
                results = self.call(endpoint, params)
                yield results
                if (
                    "pagination" in results
                    and "page" in results["pagination"]
                    and "urls" in results["pagination"]
                    and "next" in results["pagination"]["urls"]
                ):
                    page = results["pagination"]["page"]
                    endpoint = results["pagination"]["urls"]["next"]
                    params = None
                    if page == stop_after_page:
                        break
                else:
                    break

            except APIException as e:
                self._handle_api_exception(e)
                exceptions_handled += 1

    def fetch_price_suggestions(self, release_id: int) -> dict[Condition, float]:
        suggestions = {}
        for condition, price in self.call(
            f"/marketplace/price_suggestions/{release_id}"
        ).items():
            suggestions[Condition(condition)] = price["value"]
        return suggestions

    def fetch_release(self, release_id: int) -> Release:
        return Release.from_json(
            self.call(f"/releases/{release_id}"),
            self.fetch_price_suggestions(release_id),
        )

    def fetch_listing(self, listing_id: int, release: Release) -> Listing:
        return Listing.from_json(
            self.call(f"/marketplace/listings/{listing_id}"), release
        )

    def fetch_wantlist(
        self, username: str, first_page: int = 1
    ) -> Iterator[tuple[int, WantlistItem]]:
        for p in self.paginate_api_results(
            f"/users/{username}/wants", first_page=first_page
        ):
            page = p["pagination"]["page"]
            pages = p["pagination"]["pages"]
            for w in track(
                p["wants"],
                description=f"[blue]Loading wantlist page {page} of {pages}...",
            ):
                release = self.fetch_release(w["id"])
                date_added = datetime.fromisoformat(w["date_added"])
                notes = w.get("notes", "")
                yield (page, WantlistItem(release, date_added, notes))
