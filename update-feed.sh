#!/bin/sh

python3 /deals.py --quiet --condition '>VG' --minimum-discount 20 --feed /srv/http/index.xml
