SHELL = /bin/bash
PYTHON := ./venv/bin/python
PIP := ./venv/bin/python -m pip
APP := deals
REGION := iad
.DEFAULT_GOAL := run

.PHONY: update_feed update_wantlist clean secrets deploy

$(PYTHON):
	python3 -m venv venv
	$(PIP) install --upgrade pip
	$(PIP) install wheel
	$(PIP) install -r requirements.txt
	$(PIP) install --editable .

update_feed: | $(PYTHON)
	time $(PYTHON) -I \
	-m deals.main \
	--feed atom.xml \
	--minutes 1

update_wantlist: | $(PYTHON)
	caffeinate -s \
	time $(PYTHON) -I \
	-m deals.wantlist \
	--clear

wantlist.pickle:
	rsync deals.internal:$@ $@

searches.pickle: wantlist.pickle | $(PYTHON)
	time $(PYTHON) -I \
	-m deals.searches

clean:
	rm -rf venv wantlist.pickle

secrets:
	cat .env | fly secrets import
	@echo
	fly secrets list

deploy: wantlist.pickle
	source .env && \
	fly deploy \
	--build-secret DISCOGS_USER="$$DISCOGS_USER" \
	--build-secret TOKEN="$$TOKEN" \
	--build-secret FEED_URL="$$FEED_URL" \
	--build-secret FEED_AUTHOR_NAME="$$FEED_AUTHOR_NAME" \
	--build-secret FEED_AUTHOR_EMAIL="$$FEED_AUTHOR_EMAIL"
