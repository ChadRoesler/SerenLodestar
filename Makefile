# Mirrors the CI matrix legs locally.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test         - Python base install (.[dev])          - matches CI extras=dev
#   make test-corp    - Python with CORP extras (.[dev,corp]) - matches CI extras=dev,corp
#   make test-all     - both in sequence
#   make clean        - remove any leftover venvs
#
# Each target creates a fresh isolated venv, runs tests, then removes it.
# Venvs are also gitignored as a belt-and-suspenders safety net.
#
# (This file was Corpus Callosum's for a while - PKG_DIR pointed at a package
# that is not in this repo, so `make test` could not have worked.)

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR    := SerenLodestar
VENV_BASE  := .venv-base
VENV_CORP  := .venv-corp

.PHONY: test test-corp test-all clean

test:
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_BASE); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-base\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev]"; \
	.\.venv-base\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	exit $$status

test-corp:
	Remove-Item -Recurse -Force $(VENV_CORP) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_CORP); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-corp\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev,corp]"; \
	.\.venv-corp\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_CORP) -ErrorAction SilentlyContinue; \
	exit $$status

test-all: test test-corp

clean:
	Remove-Item -Recurse -Force $(VENV_BASE), $(VENV_CORP) -ErrorAction SilentlyContinue
