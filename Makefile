PY ?= .venv/bin/python

.PHONY: check test lint dead streams vendor-test build bench

check: test lint dead streams vendor-test build

test:
	$(PY) -m pytest -q -W error
	$(PY) -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('misaka', quiet=1) else 1)"
	$(PY) -c "import pkgutil,importlib,misaka; [importlib.import_module(m.name) for m in pkgutil.walk_packages(misaka.__path__, 'misaka.') if not m.name.endswith('__main__')]; print('import sweep ok')"

lint:
	$(PY) -m ruff check misaka tests bench --exclude misaka/documents/pageindex --exclude misaka/extensions/hermes_lcm/vendor --exclude tests/hermes_lcm_vendor --exclude misaka/ai/models_generated.py --exclude misaka/ai/image_models_generated.py

# A module-level name nobody reads is rot; catching it here is cheaper than an audit.
dead:
	$(PY) scripts/deadcheck.py

streams:
	$(PY) scripts/streamcheck.py

# Upstream hermes-lcm's own suite, run against the vendored copy: the fidelity
# harness for the port. Outside `test` because upstream does not write to -W error.
vendor-test:
	$(PY) -m pytest -q -p no:cacheprovider tests/hermes_lcm_vendor/

build:
	uv build -q && uv lock --check

# A real benchmark run costs money and hours: deliberately outside `check`.
bench:
	$(PY) -m bench run --questions smoke --driver misaka --concurrency 1
