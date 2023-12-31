import pickle
import re
import sys

from httpx import Client
from rich.console import Console
from typing import NamedTuple
from urllib.parse import urlencode

from . import wantlist
from .api import API, WantlistItem, Release, Label

NON_WORD_CHARS = re.compile(r"\W+")
SEARCHES_FILENAME = "searches.pickle"


class Category(NamedTuple):
    id: int
    name: str

    def __repr__(self):
        return f"{self.name}/{self.id}"


MUSIC = Category(11233, "Music")
VINYL = Category(176985, "Vinyl-Records")
CASSETTES = Category(176983, "Cassettes")
CDS = Category(176984, "Music-CDs")


def category_for_release(release: Release) -> Category:
    match release.formats:
        case ["Acetate"]:
            return VINYL
        case ["All Media", *rest] | ["Box Set", *rest]:
            if ("CD" in rest) and ("Vinyl" not in rest):
                return CDS
            elif ("CD" not in rest) and ("Vinyl" in rest):
                return VINYL
            elif "Cassette" in rest:
                return CASSETTES
        case ["CD"] | ["CDr"] | ["SACD"] | ["CD", "CDr"] | ["CD", "File"]:
            return CDS
        case ["Cassette"]:
            return CASSETTES
        case ["Flexi-disc", *rest]:
            return VINYL
        case ["Vinyl"] | ["Lathe Cut"]:
            return VINYL

    return MUSIC


def normalize_word(w: str) -> str:
    return "-".join([NON_WORD_CHARS.sub("", p) for p in w.split("-")])


def normalize(s: str) -> str:
    if len(s) == 0:
        return ""

    words = [normalize_word(w) for w in s.lower().split()]

    if words == ["the", "the"]:
        return s

    if words[0] == "the" or words[0] == "a":
        words.pop(0)

    return " ".join(words)


def keywords_for_labels(labels: dict[Label, str | None]) -> set[str]:
    keywords = set()
    for label, catno in labels.items():
        words = normalize(label.name).split()
        if words[-1] == "records" or words[-1] == "music":
            words.pop()
        keywords.add(" ".join(words))
        if not (catno is None or catno == "none"):
            keywords.add(catno)
    return keywords


def keywords_for_release(release: Release) -> list[str]:
    keywords = {normalize(part) for part in release.title.split("/")}
    keywords |= {normalize(a.name) for a in release.artists}
    keywords |= keywords_for_labels(release.labels)
    if not (release.country is None or release.country == "US"):
        keywords.add(release.country)
    return [k for k in keywords if len(k) > 0]


def search_url_for(want: WantlistItem) -> str:
    keywords = keywords_for_release(want.release)
    if len(want.notes) > 0:
        keywords.append(normalize(want.notes))
    query = {
        "_nkw": " ".join(keywords),
        "LH_TitleDesc": 1,
    }
    return f"https://www.ebay.com/sch/{category_for_release(want.release)}/i.html?{urlencode(query)}"


def main() -> None:
    data = []
    console = Console()
    with Client() as client:
        api = API(client, console)
        for want in wantlist.get(api):
            data.append(
                (
                    search_url_for(want),
                    {
                        cond.name: price
                        for cond, price in want.release.price_suggestions.items()
                    },
                )
            )
    with open(SEARCHES_FILENAME, "wb") as f:
        pickle.dump(data, f, pickle.HIGHEST_PROTOCOL)
    print(f"Wrote {len(data)} searches to {SEARCHES_FILENAME}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
