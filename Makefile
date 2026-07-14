.PHONY: help install dev-install run gunicorn test lint format clean distclean translations-extract translations-update translations-init translations-compile docker docker-push

PYTHON      ?= python3
VENV        ?= venv
VENV_BIN    := $(VENV)/bin
LANG_CODE   ?= en

help:
	@echo "Cibles disponibles :"
	@echo "  make install             - crée le venv et installe l'appli (+ gunicorn)"
	@echo "  make dev-install         - idem + dépendances de dev (pytest, ruff)"
	@echo "  make run                 - lance le serveur de développement Flask"
	@echo "  make gunicorn            - lance l'appli en production via gunicorn"
	@echo "  make test                - exécute la suite de tests"
	@echo "  make lint                - vérifie le code avec ruff"
	@echo "  make format              - reformate le code avec ruff"
	@echo "  make translations-extract  - (re)génère messages.pot depuis le code/templates"
	@echo "  make translations-init LANG_CODE=xx - crée un nouveau catalogue de langue"
	@echo "  make translations-update   - met à jour les .po existants après extraction"
	@echo "  make translations-compile  - compile les .po en .mo (requis avant de lancer l'appli)"
	@echo "  make clean               - supprime les fichiers Python compilés/caches"
	@echo "  make distclean           - clean + supprime le venv, la base et les logs"

$(VENV_BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(VENV_BIN)/pip install --upgrade pip

install: $(VENV_BIN)/python
	$(VENV_BIN)/pip install -e ".[prod]"

dev-install: $(VENV_BIN)/python
	$(VENV_BIN)/pip install -e ".[prod,dev]"

run: install
	$(VENV_BIN)/kartotek-master

gunicorn: install
	VENV_DIR=$(VENV) bash -x ./scripts/run_gunicorn.sh

test: dev-install
	$(VENV_BIN)/pytest

lint: dev-install
	$(VENV_BIN)/ruff check src

format: dev-install
	$(VENV_BIN)/ruff format src

translations-extract: install
	$(VENV_BIN)/pybabel extract -F babel.cfg -o src/kartotek_master/translations/messages.pot .

translations-update: install
	$(VENV_BIN)/pybabel update -i src/kartotek_master/translations/messages.pot -d src/kartotek_master/translations

translations-init: install
	$(VENV_BIN)/pybabel init -i src/kartotek_master/translations/messages.pot -d src/kartotek_master/translations -l $(LANG_CODE)

translations-compile: install
	$(VENV_BIN)/pybabel compile -d src/kartotek_master/translations

clean:
	find . -type d -name '__pycache__' -not -path './$(VENV)/*' -exec rm -rf {} +
	rm -rf .pytest_cache build dist src/*.egg-info

distclean: clean
	rm -rf $(VENV) data/*.db data/*.db-wal data/*.db-shm logs/*.log

docker:
	docker build -t kartotek_master .

docker-push:
	docker tag kartotek_master localhost:5000/kartotek_master
	docker push localhost:5000/kartotek_master
