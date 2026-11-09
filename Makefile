.PHONY: help install verify test run check clean

PY ?= python3
VENV = .venv
BIN = $(VENV)/bin
BRIDGE_PORT ?= 4097
BRIDGE_HOST ?= 0.0.0.0

help:
	@echo "Targets:"
	@echo "  install   create .venv and install deps"
	@echo "  verify    check all files present and non-empty"
	@echo "  test      run smoke test"
	@echo "  check     verify + test"
	@echo "  run       start the server"
	@echo "  clean     remove .venv and caches"

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -r requirements.txt

verify:
	$(BIN)/python verify_files.py

test:
	$(BIN)/python test_imports.py

check: verify test

run:
	BRIDGE_HOST=$(BRIDGE_HOST) BRIDGE_PORT=$(BRIDGE_PORT) $(BIN)/uvicorn server:app --host $(BRIDGE_HOST) --port $(BRIDGE_PORT)

clean:
	rm -rf $(VENV) __pycache__ *.egg-info
