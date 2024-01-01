#!/bin/sh

/venv/bin/python -I \
    -m deals.main \
    --quiet \
    --feed /srv/http/index.xml \
    --minutes 25
