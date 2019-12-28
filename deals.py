#! ./venv/bin/python3

import argparse
import requests
import atoma
import re
from sys import stderr
from time import sleep
from urllib.parse import urlencode, quote_plus
from forex_python.converter import get_rate
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator


FEED_URL = 'https://deals.aeshin.org/'

CONDITIONS = {
    'NM': 'Near Mint (NM or M-)',
    'M': 'Mint (M)',
}

CURRENCIES = [
    'USD',
    'JPY',
]


class DealException(Exception):
    pass


def get(url):
    sleep(1)
    r = requests.get(url)
    if not r.status_code == 200:
        raise DealException('GET %s: %s' % (url, r.status_code))
    return r.text


def find_release_url(sell_url):
    m = re.search(r'/release/(\d+)\?ev=item-vc"', get(sell_url))
    if m is None:
        raise DealException('release id not found')
    return 'https://www.discogs.com/release/%s' % m.group(1)


def find_median_price(sell_url):
    release_html = get(find_release_url(sell_url))
    m = re.search(r'<h4>Last Sold:</h4>\n\s+Never', release_html)
    if m is not None:
        return None
    m = re.search(r'<h4>Median:</h4>\n\s+\$(\d+\.\d\d)\n', release_html)
    if m is None:
        raise DealException('median price not found')
    return float(m.group(1))


def find_sale_price(summary_text):
    m = re.search(
        r'(?:%s) (\d+\.\d\d) - ' % ('|'.join(CURRENCIES)),
        summary_text)
    if m is None:
        raise DealException('price not found')
    return float(m.group(1))


def isoformat(dt):
    return dt.isoformat(timespec='seconds')


def now():
    return isoformat(datetime.now(timezone.utc))


def find_deals(conditions, currencies):

    for condition in args.condition:
        for currency in args.currency:

            wantlist_params = {
                'output': 'rss',
                'user': 'rybesh',
                'condition': condition,
                'currency': currency,
            }
            wantlist_url = ('https://www.discogs.com/sell/mpmywantsrss?'
                            + urlencode(wantlist_params, quote_via=quote_plus))

            feed = atoma.parse_atom_bytes(get(wantlist_url).encode('utf8'))

            exchange_rate = get_rate('USD', currency)

            for entry in feed.entries:
                try:
                    price = find_sale_price(entry.summary.value)
                    median = find_median_price(entry.id_)

                    if median is None:
                        continue

                    median = median * exchange_rate

                    if not price < median:
                        continue

                    discount = int((median - price) / median * 100)

                    yield {
                        'id': entry.id_,
                        'title': entry.title.value,
                        'updated': isoformat(entry.updated),
                        'summary': ('<b>%s%% below median</b><br>%s'
                                    % (discount, entry.summary.value)),
                    }

                except DealException as e:
                    print('%s: %s' % (entry.id_, e), file=stderr)


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
parser.add_argument("condition", type=condition)
parser.add_argument("currency", type=currency)
parser.add_argument("outfile")

args = parser.parse_args()

try:
    fg = FeedGenerator()
    fg.id(FEED_URL)
    fg.title('Discogs Deals')
    fg.updated(now())
    fg.link(href=FEED_URL, rel='self')
    fg.author({'name': 'Ryan Shaw', 'email': 'rieyin@icloud.com'})

    for deal in find_deals(args.condition, args.currency):
        fe = fg.add_entry()
        fe.id(deal['id'])
        fe.title(deal['title'])
        fe.updated(deal['updated'])
        fe.link(href=deal['id'])
        fe.content(deal['summary'], type='html')

    fg.atom_file(args.outfile)

except DealException as e:
    print(e, file=stderr)
