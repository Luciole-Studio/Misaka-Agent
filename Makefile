PY ?= .venv/bin/python

.PHONY: check test lint build

check: test lint build

test:
	$(PY) -m pytest -q -W error
	$(PY) -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('misaka', quiet=1) else 1)"
	$(PY) -c "import pkgutil,importlib,misaka; [importlib.import_module(m.name) for m in pkgutil.walk_packages(misaka.__path__, 'misaka.') if not m.name.endswith('__main__')]; print('import sweep ok')"

lint:
	$(PY) -m ruff check misaka tests --exclude misaka/documents/pageindex --exclude misaka/ai/models_generated.py --exclude misaka/ai/image_models_generated.py

build:
	uv build -q && uv lock --check
