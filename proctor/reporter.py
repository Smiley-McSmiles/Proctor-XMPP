"""Console reporting: human logs on stderr, machine data on stdout."""

from __future__ import print_function

import sys


class Reporter(object):
    def __init__(self, json_mode=False, quiet=False, verbose=0, color=None, stream=None,
                 error_stream=None):
        self.json_mode = json_mode
        self.quiet = quiet
        self.verbose = verbose or 0
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        if color is None:
            color = hasattr(self.error_stream, "isatty") and self.error_stream.isatty()
        self.color = bool(color)

    def _style(self, text, code):
        if not self.color:
            return text
        return "\033[%sm%s\033[0m" % (code, text)

    def _print(self, message):
        print(message, file=self.error_stream)

    def info(self, message):
        if self.quiet:
            return
        self._print(message)

    def step(self, message):
        if self.quiet:
            return
        self._print(self._style("==> ", "1;36") + str(message))

    def success(self, message):
        if self.quiet:
            return
        self._print(self._style("ok: ", "1;32") + str(message))

    def warn(self, message):
        self._print(self._style("warning: ", "1;33") + str(message))

    def error(self, message):
        self._print(self._style("error: ", "1;31") + str(message))

    def debug(self, message):
        if self.verbose:
            self._print(self._style("debug: ", "2") + str(message))

    def blank(self):
        if not self.quiet:
            self._print("")

    def emit(self, result, human=None):
        """Print a command result. JSON to stdout, or human formatter output."""
        if self.json_mode:
            import json
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str),
                  file=self.stream)
            return
        if human is not None:
            output = human(result)
            if output:
                print(output, file=self.stream)
        elif isinstance(result, str):
            print(result, file=self.stream)


def table(headers, rows, aligns=None):
    """Render a simple ASCII table without external dependencies."""
    headers = [str(item) for item in headers]
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(item) for item in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    aligns = aligns or ["left"] * len(widths)

    def line(cells):
        parts = []
        for index, cell in enumerate(cells):
            width = widths[index]
            if index < len(aligns) and aligns[index] == "right":
                parts.append(cell.rjust(width))
            else:
                parts.append(cell.ljust(width))
        return "  ".join(parts).rstrip()

    output = [line(headers)]
    output.append("  ".join("-" * width for width in widths))
    for row in rows:
        output.append(line(row))
    return "\n".join(output)
