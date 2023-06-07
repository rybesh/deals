#!/bin/sh

python3 /deals.py \
    --quiet \
    --condition '>VG' \
    --minimum-discount 25 \
    --skip-never-sold \
    --feed /srv/http/index.xml
