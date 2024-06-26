import argparse
import os
import pickle
import sys

from httpx import Client
from io import StringIO
from rich.console import Console
from typing import Any

from .api import API, WantlistItem
from .config import config

CACHE_FILENAME = "wantlist.pickle"


class Cache:
    def __init__(self, page: int, wants: dict[int, WantlistItem]):
        self.page = page
        self.wants = wants

    def update(self, page: int, want: WantlistItem):
        if not self.page == page:
            log(f"Cached wantlist page {self.page}")
            self.page = page
        self.wants[want.release.id] = want


class CustomUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if name == "Cache":
            return Cache
        return super().find_class(module, name)


def log(x: Any) -> None:
    print(x, file=sys.stderr)


def _load_cache() -> Cache:
    try:
        with open(CACHE_FILENAME, "rb") as f:
            return CustomUnpickler(f).load()
    except FileNotFoundError:
        return Cache(1, {})


def _save_cache(cache: Cache) -> None:
    with open(CACHE_FILENAME + ".new", "wb") as f:
        pickle.dump(cache, f, pickle.HIGHEST_PROTOCOL)
        os.rename(CACHE_FILENAME + ".new", CACHE_FILENAME)


def get(api: API, clear_cache=False, refresh_cache=False) -> list[WantlistItem]:
    if clear_cache:
        log("Clearing wantlist cache...")
        cache = Cache(1, {})
    else:
        cache = _load_cache()

    first_page = 1 if refresh_cache else cache.page
    if len(cache.wants) == 0 or refresh_cache:
        log("Refreshing wantlist cache...")
        try:
            for page, want in api.fetch_wantlist(
                config.DISCOGS_USER, first_page=first_page
            ):
                cache.update(page, want)

            # finished iterating wants; reset page to 1
            cache.page = 1
        finally:
            _save_cache(cache)

    return sorted(cache.wants.values(), key=lambda w: w.release.id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-q",
        "--quiet",
        help="write nothing to the console",
        action="store_true",
    )
    parser.add_argument(
        "-c",
        "--clear",
        help="clear the local cache and fetch new wantlist data",
        action="store_true",
    )
    parser.add_argument(
        "-r",
        "--refresh",
        help="fetch new wantlist data and add it to the local cache",
        action="store_true",
    )
    args = parser.parse_args()

    if args.quiet:
        console = Console(file=StringIO())
    else:
        console = Console()

    with Client() as client:
        api = API(client, console)
        try:
            get(api, clear_cache=args.clear, refresh_cache=args.refresh)
        finally:
            cache = _load_cache()
            log(f"{len(cache.wants)} wantlist items cached")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
