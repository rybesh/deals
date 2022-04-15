PYTHON := ./venv/bin/python
PIP := ./venv/bin/python -m pip
.DEFAULT_GOAL := run

$(PYTHON):
	python3 -m venv venv
	$(PIP) install --upgrade pip
	$(PIP) install wheel
	$(PIP) install -r requirements.txt

run: | $(PYTHON)
	time ./deals.py -c all -$$ all -m 0 -f atom.xml

clean:
	rm -rf venv

.PHONY: run clean
