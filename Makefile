PREFIX ?= /usr/local
PYTHON ?= python3

.PHONY: test compile install uninstall man clean

test:
	$(PYTHON) -m unittest discover -v

compile:
	$(PYTHON) -m compileall -q proctor tests

install:
	PREFIX=$(PREFIX) PYTHON=$(PYTHON) ./install.sh

uninstall:
	rm -f $(PREFIX)/bin/proctor
	rm -rf $(PREFIX)/lib/proctor
	rm -f $(PREFIX)/share/man/man1/proctor.1
	rm -f $(PREFIX)/share/bash-completion/completions/proctor
	rm -f $(PREFIX)/share/zsh/site-functions/_proctor
	rm -f $(PREFIX)/share/fish/vendor_completions.d/proctor.fish

man: docs/proctor.1

clean:
	find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist *.egg-info
