#!/bin/sh

/venv/bin/python -I \
    -m deals.wantlist \
    --quiet \
    --clear

/venv/bin/python -I \
    -m deals.searches

mv /searches.pickle /srv/http/searches.pickle
