#!/usr/bin/env python3
"""Set the Proctor version everywhere it is written.

    python3 packaging/set_version.py 1.2.0 [--note "what changed"]
    ./package.sh --version 1.2.0 --all

Updates proctor/__init__.py, pyproject.toml, the README badge/version line,
docs/BACKUP-FORMAT.md, the man page, the bug-report placeholder and CHANGELOG.md.
"""
import os
import re
import sys
import time

VERSION_RE = r"\d+\.\d+\.\d+(\.\d+)?([_-]?(rc|alpha|beta|pre|dev)\d*)?"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def write(rel, text):
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as handle:
        handle.write(text)


def sub(rel, pattern, repl, flags=0):
    text = read(rel)
    new, count = re.subn(pattern, repl, text, count=1, flags=flags)
    if count != 1:
        raise SystemExit("set_version: could not find the version in %s" % rel)
    write(rel, new)


def main(argv):
    args = argv[1:]
    note = None
    if "--note" in args:
        index = args.index("--note")
        note = args[index + 1] if index + 1 < len(args) else None
        del args[index:index + 2]
    if len(args) != 1 or not re.fullmatch(VERSION_RE, args[0].lstrip("v")):
        print("usage: set_version.py MAJOR.MINOR.PATCH[.BUILD][_rc1] [--note TEXT]",
              file=sys.stderr)
        return 2
    version = args[0].lstrip("v")
    sub("proctor/__init__.py", r'^__version__ = "[^"]+"',
        '__version__ = "%s"' % version, re.M)
    sub("pyproject.toml", r'^version = "[^"]+"', 'version = "%s"' % version, re.M)
    badge = version.replace("-", "--").replace("_", "__")
    sub("README.md", r"version-[0-9][^)]*-informational\.svg",
        "version-%s-informational.svg" % badge)
    sub("README.md", r"\*\*Version:\*\* [^\s<]+", "**Version:** %s" % version)
    sub("docs/BACKUP-FORMAT.md", r'"proctor_version": "[^"]+"',
        '"proctor_version": "%s"' % version)
    sub("docs/proctor.1", r'^\.TH PROCTOR 1 "[^"]*" "proctor [^"]+"',
        '.TH PROCTOR 1 "%s" "proctor %s"' % (time.strftime("%B %Y"), version), re.M)
    sub(".github/ISSUE_TEMPLATE/bug_report.yml",
        r"proctor [0-9][^\s(]* \(Python", "proctor %s (Python" % version)
    changelog = read("CHANGELOG.md")
    if not re.search(r"^## %s\b" % re.escape(version), changelog, re.M):
        entry = ("## %s — %s\n\n%s\n\n" %
                 (version, time.strftime("%Y-%m-%d"), note or "Release %s" % version))
        if changelog.startswith("# Changelog"):
            head, rest = changelog.split("\n", 1)
            changelog = head + "\n\n" + entry + rest.lstrip("\n")
        else:
            changelog = entry + changelog
        write("CHANGELOG.md", changelog)
        print("added a %s changelog entry - edit its notes" % version)
    print("version set to %s" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
