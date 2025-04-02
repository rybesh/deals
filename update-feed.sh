#!/bin/sh

/venv/bin/python -I \
    -m deals.main \
    --quiet \
    --feed /index.xml \
    --minutes 25

cp -f /index.xml /srv/http/index.xml
