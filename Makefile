SHELL = /bin/bash
PYTHON := ./venv/bin/python
PIP := ./venv/bin/python -m pip
.DEFAULT_GOAL := index.xml

.PHONY: clean secrets deploy

$(PYTHON):
	python3 -m venv venv
	$(PIP) install --upgrade pip
	$(PIP) install wheel
	$(PIP) install -r requirements.txt
	$(PIP) install --editable .

wantlist.pickle: | $(PYTHON)
	rsync deals.internal:$@ $@ || \
	caffeinate -s time $(PYTHON) -I -m deals.wantlist --clear

searches.pickle: wantlist.pickle | $(PYTHON)
	rsync deals.internal:$@ $@ || \
	time $(PYTHON) -I -m deals.searches

index.xml: wantlist.pickle | $(PYTHON)
	rsync deals.internal:$@ $@ || \
	time $(PYTHON) -I -m deals.main --feed $@ --minutes 1

clean:
	rm -rf venv wantlist.pickle searches.pickle index.xml

secrets:
	cat .env | fly secrets import
	@echo
	fly secrets list

deploy: wantlist.pickle searches.pickle index.xml
	fly deploy
