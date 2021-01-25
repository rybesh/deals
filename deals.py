#! ./venv/bin/python3

import argparse
import requests
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
    GQL_API,
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
    if entry is None:
        msg = str(e)
    else:
        msg = (
            f'{entry.id_}\n'
            f'{entry.title.value}\n'
            f'{e}\n'
        )
    if e.status_code not in (404, 500, 502, 503):
        print(msg, file=stderr)


def debug(x):
    if DEBUG:
        print(x, file=stderr)


@sleep_and_retry
@limits(calls=1, period=1)
def call_public_api(endpoint, params={}):
    r = requests.get(
        API + endpoint,
        params=params,
        headers={'Authorization': f'Discogs token={TOKEN}'},
        timeout=10,
    )
    calls_remaining = int(r.headers.get('X-Discogs-Ratelimit-Remaining', 0))
    debug(f'{calls_remaining} calls remaining')
    if calls_remaining < 5:
        debug('sleeping...')
        sleep(10)
    if not r.status_code == 200:
        raise DealException(
            f'GET {r.url} failed ({r.status_code})',
            r.status_code)
    return r.json()


@sleep_and_retry
@limits(calls=1, period=1)
def call_graphql_api(operation, variables, extensions):
    r = requests.get(
        GQL_API,
        params={
            'operationName': operation,
            'variables': json.dumps(variables),
            'extensions': json.dumps(extensions),
        },
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_1_0) '
                + 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 '
                + 'Safari/537.36'
            ),
            'Origin': 'https://www.discogs.com',
            'Referer': 'https://www.discogs.com/',
        },
        timeout=10,
    )
    if not r.status_code == 200:
        raise DealException(
            f'GET {r.url} failed ({r.status_code})',
            r.status_code)
    return r.json()


def get(url, params={}):
    r = requests.get(
        url,
        params=params,
        timeout=10,
    )
    if not r.status_code == 200:
        raise DealException(
            f'GET {r.url} failed ({r.status_code})',
            r.status_code)
    r.encoding = 'UTF-8'
    return r.text


def get_seller_rating(listing):
    return float(listing['seller']['stats'].get('rating', '0.0'))


def get_total_price(listing):
    price = listing['price'].get('value')
    shipping = listing['shipping_price'].get('value')
    if price and shipping:
        return price + shipping
    else:
        return None


def get_suggested_price(release_id, condition):
    suggestions = call_public_api(
        f'/marketplace/price_suggestions/{release_id}'
    )
    return suggestions.get(condition, {}).get('value')


def get_demand_ratio(release_id):
    stats = call_public_api(f'/releases/{release_id}/stats')
    return (
        stats['num_want'] / (stats['num_have'] if stats['num_have'] > 0 else 1)
    )


def get_price_statistics(release_id):
    o = call_graphql_api(
        'ReleaseStatsPrices',
        {'discogsId': release_id, 'currency': 'USD'},
        {'persistedQuery': {
            'version': 1,
            'sha256Hash':
            'f1222b0b90f95a8cd645e4e48049be3385587ce6808c9364f812af761d44d66f'
        }}
    )

    def error(message):
        j = json.dumps(o, indent=2, sort_keys=True)
        raise DealException(f'{message}\n\n{j}\n')

    statistics = o.get('data', {}).get('release', {}).get('statistics', {})

    if any(x not in statistics for x in ('min', 'median', 'max')):
        error('missing price statistics')

    prices = (statistics[x] for x in ('min', 'median', 'max'))
    amounts = []
    for price in prices:
        if price is None:
            amounts.append(price)  # not sold yet
        else:
            amount = price.get('converted', {}).get('amount')
            if amount is None:
                error('missing amount')
            else:
                amounts.append(amount)
    return amounts


def get_release_year(listing):
    # Discogs API uses 0 for unknown year
    year = listing['release'].get('year', 0)
    return date.today().year if year == 0 else year


def difference(price, benchmark):
    if benchmark is None:
        return None
    else:
        return round((benchmark - price) / benchmark * 100)


def summarize_difference(difference):
    if difference > 0:
        return f'{difference}% below'
    elif difference < 0:
        return f'{-difference}% above'
    else:
        return 'same as'


def isoformat(dt):
    return dt.isoformat(timespec='seconds')


def now():
    return isoformat(datetime.now(timezone.utc))


def get_deals(conditions, currencies, minimum_discount):

    for condition in args.condition:
        for currency in args.currency:

            wantlist_url = f'{WWW}/sell/mpmywantsrss'
            wantlist_params = {
                'output': 'rss',
                'user': DISCOGS_USER,
                'condition': condition,
                'currency': currency,
                'hours_range': '0-12',
            }
            feed = atoma.parse_atom_bytes(
                get(wantlist_url, wantlist_params).encode('utf8')
            )

            for entry in feed.entries:
                try:
                    listing_id = entry.id_.split('/')[-1]
                    listing = call_public_api(
                        f'/marketplace/listings/{listing_id}'
                    )
                    seller_rating = get_seller_rating(listing)
                    price = get_total_price(listing)
                    release_year = get_release_year(listing)
                    release_id = listing['release']['id']
                    min_price, median_price, max_price = get_price_statistics(
                        release_id
                    )
                    suggested_price = get_suggested_price(release_id, condition)
                    demand_ratio = get_demand_ratio(release_id)
                    has_sold = True

                    if price is None:
                        continue

                    # adjust price for standard domestic shipping
                    price = price - STANDARD_SHIPPING

                    release_age = date.today().year - release_year

                    if condition == CONDITIONS['VG+']:
                        if release_age < ALLOW_VG['minimum_age']:
                            continue
                        if seller_rating < ALLOW_VG['minimum_seller_rating']:
                            continue

                    if median_price is None:
                        has_sold = False

                    if has_sold:
                        if not price < median_price:
                            continue

                        difference_from_median = difference(
                            price, median_price)
                        difference_from_suggested = difference(
                            price, suggested_price)
                        difference_from_min = difference(
                            price, min_price)
                        difference_from_max = difference(
                            price, max_price)

                        minimum = minimum_discount if demand_ratio < 2 else 5

                        if difference_from_median < minimum:
                            continue

                    debug(
                        f'\n{entry.title.value}\n'
                        f'{entry.summary.value}\n'
                        f'price: ${price:.2f}\n'
                        f'demand ratio: {demand_ratio:.1f}\n'
                        f'seller rating: {seller_rating:.1f}\n'
                        f'release year: {release_year}'
                    )

                    if has_sold:
                        debug(
                            f'median price: ${median_price:.2f}\n'
                            f'suggested price: ${suggested_price:.2f}\n'
                            f'lowest price: ${min_price:.2f}\n'
                            f'highest price: ${max_price:.2f}\n'
                            f'difference from median: '
                            f'{difference_from_median}%\n'
                            f'difference from suggested: '
                            f'{difference_from_suggested}%\n'
                            f'difference from lowest: '
                            f'{difference_from_min}%\n'
                            f'difference from highest: '
                            f'{difference_from_max}%\n'
                        )
                        summary = (
                            f'<b>{summarize_difference(difference_from_median)}'
                            f' median price (${median_price:.2f})</b><br>'
                            f'{summarize_difference(difference_from_suggested)}'
                            f' suggested price (${suggested_price:.2f})<br>'
                            f'{summarize_difference(difference_from_min)}'
                            f' min price (${min_price:.2f})<br>'
                            f'{summarize_difference(difference_from_max)}'
                            f' max price (${max_price:.2f})<br>'
                            f'demand ratio: {demand_ratio:.1f}<br><br>'
                            f'{entry.summary.value}'
                        )
                    else:
                        debug('never sold\n')
                        summary = (
                            f'<b>never sold</b><br>'
                            f'demand ratio: {demand_ratio:.1f}<br><br>'
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
