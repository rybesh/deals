#!/bin/sh

/venv/bin/python -I \
    -m deals.wantlist \
    --quiet \
    --clear

/venv/bin/python -I \
    -m deals.searches

cp -f /wantlist.pickle /srv/http/wantlist.pickle
cp -f /searches.pickle /srv/http/searches.pickle
