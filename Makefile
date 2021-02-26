PYTHON = ./venv/bin/python
PIP = ./venv/bin/pip

$(PYTHON):
	python3 -m venv venv
	$(PIP) install --upgrade pip
	$(PIP) install wheel
	$(PIP) install -r requirements.txt

run: $(PYTHON)
	time ./deals.py all all 0 atom.xml

clean:
	rm -rf venv

default: run

.PHONY: run clean
