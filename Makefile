PREFIX ?= /usr/local
DESTDIR ?=
PYTHON ?= python3

.PHONY: test compile install uninstall package man clean

test:
	$(PYTHON) -m unittest discover -v

compile:
	$(PYTHON) -m compileall -q proctor tests

install:
	PREFIX=$(PREFIX) DESTDIR=$(DESTDIR) PYTHON=$(PYTHON) ./install.sh

package:
	./package.sh --all

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/proctor
	rm -rf $(DESTDIR)$(PREFIX)/lib/proctor
	rm -f $(DESTDIR)$(PREFIX)/share/man/man1/proctor.1
	rm -f $(DESTDIR)$(PREFIX)/share/bash-completion/completions/proctor
	rm -f $(DESTDIR)$(PREFIX)/share/zsh/site-functions/_proctor
	rm -f $(DESTDIR)$(PREFIX)/share/fish/vendor_completions.d/proctor.fish

man: docs/proctor.1

clean:
	find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist *.egg-info
