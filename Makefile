SHELL = /bin/bash
PYTHON := ./venv/bin/python
PIP := ./venv/bin/python -m pip
APP := deals
REGION := iad
.DEFAULT_GOAL := run

$(PYTHON):
	python3 -m venv venv
	$(PIP) install --upgrade pip
	$(PIP) install wheel
	$(PIP) install -r requirements.txt

run: | $(PYTHON)
	time ./deals.py -c '>VG' -$$ all -m 0 -f atom.xml

launch:
	fly launch \
	--auto-confirm \
	--copy-config \
	--ignorefile .dockerignore \
	--dockerfile Dockerfile \
	--region $(REGION) \
	--name $(APP)
	@echo "Next: make secrets"

secrets:
	cat .env | fly secrets import
	@echo
	fly secrets list
	@echo "Next: make deploy"

deploy:
	source .env && \
	fly deploy \
	--local-only \
	--build-secret DISCOGS_USER="$$DISCOGS_USER" \
	--build-secret TOKEN="$$TOKEN" \
	--build-secret FEED_URL="$$FEED_URL" \
	--build-secret FEED_AUTHOR_NAME="$$FEED_AUTHOR_NAME" \
	--build-secret FEED_AUTHOR_EMAIL="$$FEED_AUTHOR_EMAIL"

clean:
	rm -rf venv atom.xml

.PHONY: run clean
