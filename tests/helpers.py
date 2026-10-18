"""Shared fixtures for Proctor's test suite."""

from __future__ import print_function

import contextlib
import io
import json
import os
import sqlite3
import sys

from proctor.cli import main


def run_cli(arguments, env=None):
    old_env = {}
    values = {
        "PROCTOR_SERVICE_MANAGER": "unknown",
        "NO_COLOR": "1",
    }
    if env:
        values.update(env)
    for key, value in values.items():
        old_env[key] = os.environ.get(key)
        os.environ[key] = value
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, stdout.getvalue(), stderr.getvalue()


def run_json(arguments, env=None):
    code, stdout, stderr = run_cli(list(arguments) + ["--json"], env=env)
    try:
        payload = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        raise AssertionError("invalid JSON output: %r\nstderr: %s" % (stdout, stderr))
    return code, payload, stderr


def make_prosody(root, storage="sql", with_logs=True):
    """Create a synthetic Prosody installation under root."""
    etc = os.path.join(root, "etc", "prosody")
    data = os.path.join(root, "var", "lib", "prosody")
    logs = os.path.join(root, "var", "log", "prosody")
    repo = os.path.join(root, "repo")
    for path in (etc, os.path.join(etc, "certs"), os.path.join(etc, "conf.d"),
                 os.path.join(data, "example.com", "accounts"),
                 os.path.join(data, "example.com", "vcards"), logs, repo):
        os.makedirs(path, exist_ok=True)

    config = os.path.join(etc, "prosody.cfg.lua")
    sql_line = ""
    if storage == "sql":
        sql_line = 'sql = { driver = "SQLite3", database = "prosody.sqlite" }\n'
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(
            "-- synthetic config\n"
            'admins = { "admin@example.com" }\n'
            'data_path = "%s"\n'
            'storage = "%s"\n'
            '%s'
            'authentication = "internal_hashed"\n'
            'certificates = "certs"\n'
            'modules_enabled = { "roster"; "sasl"; "tls"; "dialback"; "muc"; }\n'
            'VirtualHost "example.com"\n'
            'Component "conference.example.com" "muc"\n'
            '%s'
            'Include "conf.d/*.cfg.lua"\n'
            % (data, storage, sql_line,
               ('log = { { levels = { min = "info" }, to = "file", '
                'filename = "%s/prosody.log" } }\n' % logs) if with_logs else "")
        )
    with open(os.path.join(etc, "conf.d", "example.cfg.lua"), "w", encoding="utf-8") as handle:
        handle.write(
            'VirtualHost "example.com"\n'
            'ssl = {\n'
            '  key = "%s/certs/example.com.key";\n'
            '  certificate = "%s/certs/example.com.crt";\n'
            '}\n' % (etc, etc)
        )
    with open(os.path.join(etc, "certs", "example.com.crt"), "w", encoding="utf-8") as handle:
        handle.write("-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n-----END CERTIFICATE-----\n")
    with open(os.path.join(etc, "certs", "example.com.key"), "w", encoding="utf-8") as handle:
        handle.write("-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n")
    with open(os.path.join(data, "example.com", "accounts", "carol.dat"), "w",
              encoding="utf-8") as handle:
        handle.write("carol-account\n")
    with open(os.path.join(data, "example.com", "vcards", "bob.vcard"), "w",
              encoding="utf-8") as handle:
        handle.write("BEGIN:VCARD\nFN:Bob\nEND:VCARD\n")
    os.symlink("bob.vcard", os.path.join(data, "example.com", "vcards", "alice.vcard"))
    if with_logs:
        with open(os.path.join(logs, "prosody.log"), "w", encoding="utf-8") as handle:
            handle.write("log line\n")

    if storage == "sql":
        database = os.path.join(data, "prosody.sqlite")
        connection = sqlite3.connect(database)
        connection.execute(
            'CREATE TABLE prosody (host TEXT, "user" TEXT, store TEXT, key TEXT, value TEXT, '
            'PRIMARY KEY (host, "user", store, key))'
        )
        for user in ("alice", "bob"):
            connection.execute(
                'INSERT INTO prosody VALUES (?, ?, ?, ?, ?)',
                ("example.com", user, "accounts", "password", "hash-" + user),
            )
        connection.execute(
            'INSERT INTO prosody VALUES (?, ?, ?, ?, ?)',
            ("example.com", "alice", "roster", "contact", "{}"),
        )
        connection.commit()
        connection.close()
    return {"root": root, "etc": etc, "data": data, "logs": logs, "repo": repo,
            "config": config}
