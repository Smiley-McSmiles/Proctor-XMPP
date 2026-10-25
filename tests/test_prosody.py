from __future__ import print_function

import os
import tempfile
import unittest
from types import SimpleNamespace

from proctor.hostinfo import derive_data_dir, derive_log_dir, default_paths
from proctor.prosody import (
    ProsodyInstall, normalize_sql, parse_prosody_config, strip_lua_comments,
)


class LuaParsingTests(unittest.TestCase):
    def test_strip_comments_preserves_strings_and_lines(self):
        text = (
            'a = "http://example.com/--not-a-comment"\n'
            '-- comment\n'
            'b = "value" -- trailing\n'
            '--[[ block\ncomment ]] c = "three"\n'
        )
        clean = strip_lua_comments(text)
        self.assertIn("http://example.com/--not-a-comment", clean)
        self.assertNotIn("block", clean)
        self.assertNotIn("-- trailing", clean)
        self.assertIn('b = "value"', clean)

    def test_parse_config_with_include_sql_log_certs(self):
        root = tempfile.mkdtemp()
        etc = os.path.join(root, "etc", "prosody")
        os.makedirs(os.path.join(etc, "conf.d"))
        config = os.path.join(etc, "prosody.cfg.lua")
        with open(config, "w") as handle:
            handle.write(
                'data_path = "%s/data"\n'
                'storage = "sql"\n'
                'sql = { driver = "PostgreSQL", database = "prosody", '
                'host = "db.example", port = 5433, username = "prosody", password = "secret" }\n'
                'certificates = "certs"\n'
                'modules_enabled = { "roster"; "muc"; }\n'
                'VirtualHost "example.com"\n'
                'Component "conference.example.com" "muc"\n'
                'log = { { to = "file", filename = "%s/prosody.log" } }\n'
                'Include "conf.d/*.cfg.lua"\n' % (root, root)
            )
        with open(os.path.join(etc, "conf.d", "site.cfg.lua"), "w") as handle:
            handle.write('VirtualHost "second.example"\n'
                         'ssl = { key = "%s/key.pem"; certificate = "%s/cert.pem" }\n'
                         % (etc, etc))
        parsed = parse_prosody_config(config)
        self.assertEqual(parsed["values"]["storage"], "sql")
        self.assertEqual(parsed["sql"]["driver"], "PostgreSQL")
        self.assertEqual(parsed["sql"]["port"], 5433)
        self.assertTrue(parsed["sql_has_password"])
        self.assertIn("example.com", parsed["virtualhosts"])
        self.assertIn("second.example", parsed["virtualhosts"])
        self.assertIn("muc", parsed["modules"])
        self.assertEqual(parsed["log_files"], [os.path.join(root, "prosody.log")])
        self.assertIn(os.path.join(etc, "cert.pem"), parsed["certificate_files"])
        self.assertIn(os.path.join(etc, "certs"), parsed["certificate_dirs"])

    def test_sqlite_default_path(self):
        parsed = {"sql": {"driver": "SQLite3", "database": "prosody.sqlite"}}
        sql = normalize_sql(parsed, "/var/lib/prosody")
        self.assertEqual(sql["engine"], "sqlite")
        self.assertEqual(sql["path"], "/var/lib/prosody/prosody.sqlite")

    def test_path_defaults_per_bsd(self):
        host = SimpleNamespace(system_id="freebsd", defaults=default_paths(
            SimpleNamespace(system_id="freebsd", family=lambda: "freebsd")))
        self.assertEqual(host.defaults["config_dir"], "/usr/local/etc/prosody")
        self.assertEqual(derive_data_dir(host, "/usr/local/etc/prosody"), "/var/db/prosody")
        openbsd = SimpleNamespace(system_id="openbsd", is_bsd=lambda: True, defaults={
            "data_dir": "/var/prosody", "log_dir": "/var/log/prosody"})
        self.assertEqual(derive_data_dir(openbsd, "/etc/prosody"), "/var/prosody")
        self.assertEqual(derive_log_dir(openbsd, "/etc/prosody"), "/var/log/prosody")


class ModuleDetectionTests(unittest.TestCase):
    def test_directory_modules_are_detected(self):
        from proctor.prosody import ProsodyInstall
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "muc"))
        open(os.path.join(root, "muc", "mod_muc.lua"), "w").close()
        open(os.path.join(root, "mod_saslauth.lua"), "w").close()
        install = object.__new__(ProsodyInstall)
        install.module_dirs = lambda: [root]
        self.assertEqual(install.available_modules(), ["muc", "saslauth"])


class InstallTests(unittest.TestCase):
    def test_install_flags_and_warnings(self):
        root = tempfile.mkdtemp()
        etc = os.path.join(root, "etc", "prosody")
        os.makedirs(etc)
        config = os.path.join(etc, "prosody.cfg.lua")
        with open(config, "w") as handle:
            handle.write('data_path = "%s/data"\nstorage = "internal_hashed"\n'
                         'prosody_user = "xmpp"\nprosody_group = "xmpp"\n'
                         'VirtualHost "example.com"\n' % root)
        parsed = parse_prosody_config(config)
        host = SimpleNamespace(
            system_id="linux", is_bsd=lambda: False, defaults={
                "config_dir": etc, "config_file": config,
                "data_dir": os.path.join(root, "data"),
                "log_dir": os.path.join(root, "log"),
                "cert_dir": os.path.join(etc, "certs"),
                "user": "prosody", "group": "prosody", "service": "prosody",
            })
        install = ProsodyInstall(host, config, parsed)
        self.assertEqual(install.user, "xmpp")
        self.assertEqual(install.storage, "internal_hashed")
        self.assertEqual(install.virtualhosts, ["example.com"])
        self.assertTrue(any("data directory" in warning for warning in install.warnings))


if __name__ == "__main__":
    unittest.main()
