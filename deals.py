#! ./venv/bin/python3

import argparse
import requests
import atoma
import re
from sys import stderr
from time import sleep
from urllib.parse import urlencode, quote_plus
from datetime import date, datetime, timezone
from feedgen.feed import FeedGenerator
from ratelimit import limits, sleep_and_retry
from config import (
    ALLOW_VG,
    API,
    CONDITIONS,
    CURRENCIES,
    DEBUG,
    DISCOGS_USER,
    FEED_AUTHOR,
    FEED_URL,
    STANDARD_SHIPPING,
    TOKEN,
    WWW,
)


class DealException(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def log_error(e, entry=None):
    msg = (
        f'{entry.id_}/n' if entry else ''
        f'{entry.title.value}/n' if entry else ''
        f'{e}',
        f' ({e.status_code})\n' if e.status_code else '\n'
    )
    if not e.status_code == 502:
        print(msg, file=stderr)


def debug(x):
    if DEBUG:
        print(x, file=stderr)


@sleep_and_retry
@limits(calls=1, period=1)
def call_api(endpoint, params={}):
    r = requests.get(
        API + endpoint,
        params=params,
        headers={'Authorization': f'Discogs token={TOKEN}'},
        timeout=10,
    )
    calls_remaining = int(r.headers['X-Discogs-Ratelimit-Remaining'])
    debug(f'{calls_remaining} calls remaining')
    if calls_remaining < 5:
        debug('sleeping...')
        sleep(10)
    if not r.status_code == 200:
        raise DealException(f'GET {r.url} failed', r.status_code)
    return r.json()


@sleep_and_retry
@limits(calls=1, period=1)
def get(url):
    r = requests.get(url, timeout=10)
    if not r.status_code == 200:
        raise DealException(f'GET {r.url} failed', r.status_code)
    r.encoding = 'UTF-8'
    return r.text


def get_seller_rating(listing):
    return float(listing['seller']['stats'].get('rating', '0.0'))


def get_total_price(listing):
    price = listing['price'].get('value')
    shipping = listing['shipping_price'].get('value')
    if price and shipping:
        return round(price + shipping, 2)
    else:
        return None


def get_suggested_price(release_id, condition):
    suggestions = call_api(f'/marketplace/price_suggestions/{release_id}')
    return suggestions.get(condition, {}).get('value')


def get_median_price(release_html):
    m = re.search(
        r'<h4>Last Sold:</h4>\n\s+Never',
        release_html
    )
    if m is not None:
        return None
    m = re.search(
        r'<h4>Median:</h4>\n\s+\$((?:\d+,)*\d+\.\d{2})\n',
        release_html
    )
    if m is None:
        raise DealException('median price not found')
    return float(m.group(1).replace(',', ''))


def get_release_year(listing):
    return listing['release'].get('year', date.today().year)


def discount(price, benchmark):
    if benchmark is None:
        return None
    else:
        return int((benchmark - price) / benchmark * 100)


def summarize_discount(discount):
    if discount > 0:
        return f'{discount}% below'
    elif discount < 0:
        return f'{-discount}% above'
    else:
        return 'same as'


def isoformat(dt):
    return dt.isoformat(timespec='seconds')


def now():
    return isoformat(datetime.now(timezone.utc))


def get_deals(conditions, currencies, minimum_discount):

    for condition in args.condition:
        for currency in args.currency:

            wantlist_params = {
                'output': 'rss',
                'user': DISCOGS_USER,
                'condition': condition,
                'currency': currency,
                'hours_range': '0-12',
            }
            wantlist_url = (
                f'{WWW}/sell/mpmywantsrss?'
                f'{urlencode(wantlist_params, quote_via=quote_plus)}'
            )

            feed = atoma.parse_atom_bytes(get(wantlist_url).encode('utf8'))

            for entry in feed.entries:
                try:
                    listing_id = entry.id_.split('/')[-1]
                    listing = call_api(f'/marketplace/listings/{listing_id}')
                    seller_rating = get_seller_rating(listing)
                    price = get_total_price(listing)
                    release_year = get_release_year(listing)
                    release_id = listing['release']['id']
                    release_url = f'{WWW}/release/{release_id}'
                    median_price = get_median_price(get(release_url))
                    suggested_price = get_suggested_price(release_id, condition)

                    if median_price is None:
                        continue

                    if price is None:
                        continue

                    # adjust price for standard domestic shipping
                    price = price - STANDARD_SHIPPING

                    if not price < median_price:
                        continue

                    discount_from_median = discount(price, median_price)
                    discount_from_suggested = discount(price, suggested_price)

                    debug(
                        f'\n{entry.title.value}\n'
                        f'{entry.summary.value}\n'
                        f'price: {price}\n'
                        f'median price: {median_price}\n'
                        f'suggested price: {suggested_price}\n'
                        f'seller rating: {seller_rating}\n'
                        f'release year: {release_year}\n'
                        f'discount from median: {discount_from_median}\n'
                        f'discount from suggested: {discount_from_suggested}\n'
                    )

                    if discount_from_median < minimum_discount:
                        continue

                    release_age = date.today().year - release_year

                    if condition == CONDITIONS['VG+']:
                        if release_age < ALLOW_VG['minimum_age']:
                            continue
                        if seller_rating < ALLOW_VG['minimum_seller_rating']:
                            continue

                    summary = (
                        f'<b>{summarize_discount(discount_from_median)}'
                        f' median ({round(median_price, 2)})</b><br>'
                        f'{summarize_discount(discount_from_suggested)}'
                        f' suggested ({round(suggested_price, 2)})<br>'
                        f'{entry.summary.value}'
                    )

                    yield {
                        'id': entry.id_,
                        'title': entry.title.value,
                        'updated': isoformat(entry.updated),
                        'summary': summary,
                    }

                except DealException as e:
                    log_error(e, entry)
                except requests.exceptions.RequestException as e:
                    debug(e)


def condition(arg):
    if arg == 'all':
        return list(CONDITIONS.values())
    if arg not in CONDITIONS:
        raise argparse.ArgumentTypeError(
            'condition must be one of: %s' % list(CONDITIONS.keys()))
    return [CONDITIONS[arg]]


def currency(arg):
    if arg == 'all':
        return CURRENCIES
    if arg not in CURRENCIES:
        raise argparse.ArgumentTypeError(
            'currency must be one of: %s' % CURRENCIES)
    return [arg]


parser = argparse.ArgumentParser()
parser.add_argument('condition', type=condition)
parser.add_argument('currency', type=currency)
parser.add_argument('minimum_discount', type=int)
parser.add_argument('outfile')

args = parser.parse_args()

try:
    fg = FeedGenerator()
    fg.id(FEED_URL)
    fg.title('Discogs Deals')
    fg.updated(now())
    fg.link(href=FEED_URL, rel='self')
    fg.author(FEED_AUTHOR)

    for deal in get_deals(args.condition, args.currency, args.minimum_discount):
        fe = fg.add_entry()
        fe.id(deal['id'])
        fe.title(deal['title'])
        fe.updated(deal['updated'])
        fe.link(href=deal['id'])
        fe.content(deal['summary'], type='html')

    fg.atom_file(args.outfile, pretty=True)

except DealException as e:
    log_error(e)
except requests.exceptions.RequestException as e:
    debug(e)
