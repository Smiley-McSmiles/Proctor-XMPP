from __future__ import print_function

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from proctor.config import load_config
from proctor.errors import ConfigError
from proctor.service import ServiceController, validate_prosody_config
from proctor.schedule import _validate_time
from proctor.crypto import validate_encryption

from tests.helpers import make_prosody, run_json, run_cli


class ServiceControllerTests(unittest.TestCase):
    def _host(self, init):
        return SimpleNamespace(init=init)

    def test_systemd_commands(self):
        controller = ServiceController(self._host("systemd"), "prosody")
        self.assertEqual(controller.command("stop"), ["systemctl", "stop", "prosody"])
        self.assertEqual(controller.command("status"),
                         ["systemctl", "is-active", "--quiet", "prosody"])

    def test_openrc_runit_freebsd_openbsd_netbsd(self):
        cases = {
            "openrc": (["rc-service", "prosody", "start"], ["rc-service", "prosody", "stop"]),
            "runit": (["sv", "start", "prosody"], ["sv", "stop", "prosody"]),
            "rc": (["service", "prosody", "start"], ["service", "prosody", "stop"]),
            "rcctl": (["rcctl", "start", "prosody"], ["rcctl", "stop", "prosody"]),
            "rcd": (["/etc/rc.d/prosody", "start"], ["/etc/rc.d/prosody", "stop"]),
        }
        for init, (start, stop) in cases.items():
            controller = ServiceController(self._host(init), "prosody")
            self.assertEqual(controller.command("start"), start)
            self.assertEqual(controller.command("stop"), stop)

    def test_custom_commands(self):
        controller = ServiceController(self._host("unknown"), "xmpp", config={
            "commands": {"start": [sys.executable, "-c", "pass", "{name}"],
                         "stop": "stop-xmpp {name}"}
        })
        self.assertTrue(controller.available)
        self.assertEqual(controller.command("start"),
                         [sys.executable, "-c", "pass", "xmpp"])
        self.assertEqual(controller.command("stop"), ["stop-xmpp", "xmpp"])
        self.assertTrue(controller.start())

    def test_oauth_unsupported(self):
        controller = ServiceController(self._host("unknown"), "prosody")
        self.assertFalse(controller.available)


class ScheduleAndCryptoTests(unittest.TestCase):
    def test_schedule_show_command_parses(self):
        code, payload, stderr = run_json(["schedule", "--show"])
        self.assertEqual(code, 0, stderr)
        self.assertIn("systemd", payload)
        self.assertIn("cron", payload)

    def test_validate_time(self):
        self.assertEqual(_validate_time("3:05"), "03:05")
        self.assertEqual(_validate_time("23:59"), "23:59")
        for value in ("24:00", "3", "aa:bb", "12:60"):
            with self.assertRaises(ConfigError):
                _validate_time(value)

    def test_encryption_requires_recipients(self):
        with self.assertRaises(ConfigError):
            validate_encryption({"method": "age", "recipients": []})
        with self.assertRaises(ConfigError):
            validate_encryption({"method": "nonsense", "recipients": ["x"]})


class ProvisioningTests(unittest.TestCase):
    def _layout(self):
        from proctor.provision import ServerLayout
        layout = ServerLayout()
        layout.config_file = "/etc/prosody/prosody.cfg.lua"
        layout.config_dir = "/etc/prosody"
        layout.data_dir = "/var/lib/prosody"
        layout.log_dir = "/var/log/prosody"
        layout.cert_dirs = ["/etc/prosody/certs"]
        layout.user = "prosody"
        layout.group = "prosody"
        return layout

    def test_normalize_domains(self):
        from proctor.provision import normalize_domains
        self.assertEqual(normalize_domains(["Example.COM, chat.example.com"]),
                         ["example.com", "chat.example.com"])
        self.assertEqual(normalize_domains(["https://example.com/path"]), ["example.com"])
        self.assertEqual(normalize_domains([""]), [])
        for value in ("not a domain", "*.example.com"):
            with self.assertRaises(Exception):
                normalize_domains([value])

    def test_prosody_config_text(self):
        from proctor.provision import prosody_config_text
        layout = self._layout()
        text = prosody_config_text(layout, ["example.com"], "admin@example.com",
                                   False, "example.com", storage="sqlite",
                                   proxy=True)
        self.assertIn('VirtualHost "example.com"', text)
        self.assertIn("conference.example.com", text)
        self.assertIn('storage = "sql"', text)
        self.assertIn("http_interfaces = { \"127.0.0.1\" }", text)
        self.assertIn("c2s_require_encryption = false", text)
        self.assertIn("admin@example.com", text)

    def test_prosody_internal_storage_uses_internal_driver(self):
        from proctor.provision import prosody_config_text
        layout = self._layout()
        text = prosody_config_text(
            layout, ["example.com"], "admin@example.com", False,
            "example.com", storage="internal_hashed", proxy=True,
        )
        self.assertIn('authentication = "internal_hashed"', text)
        self.assertIn('storage = "internal"', text)
        self.assertNotIn('storage = "internal_hashed"', text)
        self.assertEqual(layout.storage, "internal")

    def test_find_existing_certificate_reuses_letsencrypt(self):
        from proctor import provision
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "other"))
        os.makedirs(os.path.join(root, "sonder.guru"))
        sans = {
            "other": {"other.example"},
            "sonder.guru": {"sonder.guru", "*.sonder.guru"},
        }
        with mock.patch.object(provision, "CERTBOT_LIVE", root), \
                mock.patch.object(provision, "has_certificate", return_value=True), \
                mock.patch.object(provision, "certificate_domains",
                                  side_effect=lambda name: sans[name]):
            self.assertEqual(provision.find_existing_certificate(
                ["sonder.guru", "upload.sonder.guru"]), "sonder.guru")
            self.assertEqual(provision.find_existing_certificate(
                ["sonder.guru", "a.b.sonder.guru"]), "sonder.guru")

    def test_missing_certificate_domains_handles_wildcards(self):
        from proctor import provision
        with mock.patch.object(provision, "certificate_domains",
                               return_value={"example.com", "*.example.com"}):
            self.assertEqual(provision.missing_certificate_domains(
                "example.com", ["example.com", "upload.example.com"]), [])
            self.assertEqual(provision.missing_certificate_domains(
                "example.com", ["example.com", "a.b.example.com"]),
                ["a.b.example.com"])

    def test_web_invite_config_and_page(self):
        from proctor import provision
        layout = self._layout()
        text = provision.prosody_config_text(
            layout, ["example.com"], "admin@example.com", True, "example.com",
            proxy=True, web_invites=True,
        )
        self.assertIn("invites_page_external = true", text)
        self.assertIn(
            'invites_page = "https://example.com/ward-invite?{invite.token}"',
            text)
        self.assertIn('site_name = "example.com"', text)

        root = tempfile.mkdtemp()
        target = os.path.join(root, "invite", "index.html")
        with mock.patch.object(provision, "INVITE_PAGE_DIR",
                               os.path.dirname(target)), \
                mock.patch.object(provision, "INVITE_PAGE_PATH", target):
            path = provision.install_invite_page()
        self.assertEqual(path, target)
        with open(target, encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn("Ward for Android", page)
        self.assertIn('action="/register"', page)
        self.assertIn('name="user"', page)
        self.assertIn('name="password"', page)

    def test_web_domains_include_service_hostnames(self):
        from proctor.provision import service_hostnames, web_domains
        self.assertEqual(service_hostnames(["example.com"]),
                         ["conference.example.com", "upload.example.com"])
        self.assertEqual(web_domains(["example.com"]),
                         ["example.com", "conference.example.com",
                          "upload.example.com"])
        self.assertEqual(web_domains(["example.com", "upload.example.com"]),
                         ["example.com", "upload.example.com",
                          "conference.example.com"])

    def test_config_module_names_include_components(self):
        from proctor.provision import _config_module_names
        names = _config_module_names(
            'modules_enabled = { "roster"; "mam"; }\n'
            'Component "conference.example.com" "muc"\n'
            '    modules_enabled = { "mam"; }\n'
        )
        self.assertEqual(names, {"roster", "mam", "muc"})

    def test_missing_community_module_is_linked(self):
        from proctor import provision
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, ".hg"))
        os.makedirs(os.path.join(root, "mod_foo"))
        open(os.path.join(root, "mod_foo", "mod_foo.lua"), "w").close()
        enabled = os.path.join(root, "enabled")
        reporter = SimpleNamespace(step=lambda *_a, **_k: None,
                                   info=lambda *_a, **_k: None,
                                   warn=lambda *_a, **_k: None)
        options = SimpleNamespace(dry_run=False, install_missing=True)
        config = SimpleNamespace(host=SimpleNamespace())
        with mock.patch.object(provision, "COMMUNITY_MODULES_ROOT", root), \
                mock.patch.object(provision, "COMMUNITY_MODULES_ENABLED", enabled), \
                mock.patch.object(provision, "_available_prosody_modules",
                                  return_value=set()), \
                mock.patch.object(provision, "ensure_mercurial",
                                  return_value={"installed": True}), \
                mock.patch.object(provision, "run"):
            result = provision.ensure_community_modules(
                config, options, reporter, SimpleNamespace(),
                'modules_enabled = { "foo"; }\n')
        self.assertEqual(result["linked"], ["foo"])
        self.assertTrue(os.path.islink(os.path.join(enabled, "mod_foo")))

    def test_prosody_config_uses_modern_module_names(self):
        from proctor import provision
        layout = self._layout()
        with mock.patch.object(provision, "_available_prosody_modules",
                               return_value=set()):
            text = provision.prosody_config_text(
                layout, ["example.com"], "admin@example.com", False,
                "example.com", proxy=True,
            )
        self.assertIn('"saslauth"', text)
        self.assertNotIn('"sasl"', text)
        self.assertIn('plugin_paths = { "/usr/local/lib/prosody-modules-enabled" }',
                      text)
        for module in ("adhoc", "admin_adhoc", "invites", "invites_adhoc",
                       "invites_register", "invites_page",
                       "invites_register_web", "register_apps",
                       "password_policy"):
            self.assertIn('"%s"' % module, text)
        self.assertIn('http_external_url = "http://example.com"', text)
        for module in ("mam", "pep", "bookmarks", "csi_simple", "cloud_notify"):
            self.assertIn('"%s"' % module, text)
        self.assertIn('Component "conference.example.com" "muc"', text)
        self.assertIn('    modules_enabled = { "mam" }', text)
        self.assertIn('Component "upload.example.com" "http_file_share"', text)
        self.assertIn('http_paths = { file_share = "/upload" }', text)
        self.assertIn("http_file_share_size_limit = 10485760", text)
        self.assertIn('http_external_url = "http://upload.example.com"', text)
        self.assertIn("c2s_direct_tls_ports = { }", text)
        self.assertNotIn("legacy_ssl_ports", text)
        self.assertNotIn("cross_domain_bosh", text)
        self.assertNotIn("cross_domain_websocket", text)

    def test_prosody_config_falls_back_to_legacy_modules(self):
        from proctor import provision
        layout = self._layout()
        with mock.patch.object(provision, "_available_prosody_modules",
                               return_value={"sasl", "http_upload"}):
            text = provision.prosody_config_text(
                layout, ["example.com"], "admin@example.com", False,
                "example.com", proxy=True,
            )
        self.assertIn('"sasl"', text)
        self.assertIn('Component "upload.example.com" "http_upload"', text)
        self.assertIn("http_upload_file_size_limit = 10485760", text)
        self.assertNotIn("http_file_share", text)

    def test_prosody_bosh_tls_config_matches_proxy_mode(self):
        from proctor import provision
        layout = self._layout()
        with mock.patch.object(provision, "_available_prosody_modules",
                               return_value=set()):
            proxied = provision.prosody_config_text(
                layout, ["example.com"], "admin@example.com", True, "example.com",
                proxy=True,
            )
            direct = provision.prosody_config_text(
                layout, ["example.com"], "admin@example.com", True, "example.com",
                proxy=False,
            )
        self.assertIn("https_ports = { }", proxied)
        self.assertNotIn("https_ssl = {", proxied)
        self.assertIn('http_external_url = "https://upload.example.com"', proxied)
        self.assertIn("https_ports = { 5281 }", direct)
        self.assertIn("https_ssl = {", direct)
        self.assertIn("/etc/letsencrypt/live/example.com/privkey.pem", direct)

    def test_nginx_and_apache_config_text(self):
        from proctor.provision import apache_config_text, nginx_config_text
        nginx = nginx_config_text(["example.com"], True, "example.com", True,
                                  ["/http-bind", "/xmpp-websocket", "/upload"])
        self.assertIn("listen 443 ssl", nginx)
        self.assertIn("server_name example.com", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:5280", nginx)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem", nginx)
        self.assertIn("Upgrade $http_upgrade", nginx)
        self.assertIn("location = /ward-invite", nginx)
        self.assertIn("/usr/local/share/proctor/invite", nginx)
        self.assertIn("location ^~ /register", nginx)
        direct = nginx_config_text(["example.com"], False, "example.com", False,
                                   ["/http-bind"])
        self.assertIn("location ^~ /register", direct)
        self.assertIn("proxy_pass http://127.0.0.1:5280", direct)
        apache = apache_config_text(["example.com"], False, "example.com", True,
                                    ["/http-bind", "/upload"])
        self.assertIn("<VirtualHost *:80>", apache)
        self.assertIn("ProxyPass /http-bind", apache)
        self.assertNotIn("SSLEngine", apache)

    def test_package_commands(self):
        from proctor.provision import PackageInstaller
        class Reporter(object):
            def info(self, message):
                pass
            def step(self, message):
                pass
            def warn(self, message):
                pass
        for manager, expected in (
                ("apt", "apt-get"),
                ("dnf", "dnf"),
                ("apk", "apk"),
                ("xbps", "xbps-install"),
                ("pkg", "pkg"),
                ("pkgin", "pkgin")):
            host = SimpleNamespace(package_manager=manager, pretty_name=manager)
            installer = PackageInstaller(host, Reporter(), dry_run=True)
            self.assertEqual(installer.command(["prosody"])[0], expected)

    def test_package_refresh_and_acl_dependency(self):
        from proctor.provision import PackageInstaller, certbot_packages
        class Reporter(object):
            def info(self, message):
                pass
            def step(self, message):
                pass
            def warn(self, message):
                pass
        expected = {
            "apt": "apt-get", "dnf": "dnf", "yum": "yum",
            "zypper": "zypper", "apk": "apk", "xbps": "xbps-install",
        }
        for manager, first in expected.items():
            host = SimpleNamespace(package_manager=manager, pretty_name=manager)
            installer = PackageInstaller(host, Reporter(), dry_run=True)
            self.assertEqual(installer.refresh_command()[0], first)
        host = SimpleNamespace(package_manager="apt", system_id="linux",
                               hostname="test", pretty_name="Ubuntu",
                               family=lambda: "debian")
        self.assertIn("acl", certbot_packages(host))

    def test_firewall_ports_and_noninteractive_skip(self):
        from unittest.mock import patch
        from proctor.provision import ensure_firewall, firewall_ports
        self.assertEqual(firewall_ports(True, True), [80, 443, 5222, 5269])
        self.assertEqual(firewall_ports(True, False), [80, 5222, 5269])
        self.assertEqual(firewall_ports(False, True), [5222, 5269, 5280, 5281])
        self.assertEqual(firewall_ports(False, True, web=True),
                         [80, 443, 5222, 5269, 5280, 5281])
        host = SimpleNamespace()
        options = SimpleNamespace(firewall="auto")
        prompter = SimpleNamespace(interactive=False, assume_yes=True)
        class Reporter(object):
            def step(self, message):
                pass
            def warn(self, message):
                pass
        active = SimpleNamespace(returncode=0, stdout=b"Status: active\n", stderr=b"")
        with patch("proctor.provision.which", lambda name: "/usr/sbin/ufw"
                   if name == "ufw" else None), \
                patch("proctor.provision.run", lambda *args, **kwargs: active):
            result = ensure_firewall(host, [5222, 5269], options,
                                     Reporter(), prompter)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("--firewall yes", result["hint"])

    def test_certificate_access_repairs_certbot_parent_and_key(self):
        import grp
        import pwd
        from unittest.mock import patch
        from proctor.provision import grant_certificate_access
        root = tempfile.mkdtemp()
        live = os.path.join(root, "live", "example.com")
        archive = os.path.join(root, "archive", "example.com")
        os.makedirs(live)
        os.makedirs(archive)
        key = os.path.join(archive, "privkey1.pem")
        fullchain = os.path.join(archive, "fullchain1.pem")
        with open(key, "w") as handle:
            handle.write("key\n")
        with open(fullchain, "w") as handle:
            handle.write("cert\n")
        os.chmod(root, 0o700)
        os.chmod(key, 0o600)
        os.chmod(fullchain, 0o600)
        os.symlink(os.path.join("..", "..", "archive", "example.com", "privkey1.pem"),
                   os.path.join(live, "privkey.pem"))
        layout = SimpleNamespace(
            user=pwd.getpwuid(os.getuid()).pw_name,
            group=grp.getgrgid(os.getgid()).gr_name,
        )
        chown_calls = []
        with patch("proctor.provision.CERTBOT_ROOT", root), \
                patch("proctor.provision.CERTBOT_LIVE", os.path.dirname(live)), \
                patch("proctor.provision.CERTBOT_ARCHIVE", os.path.dirname(archive)), \
                patch("proctor.provision.which", lambda name: None), \
                patch("proctor.provision.os.chown",
                      lambda path, uid, gid: chown_calls.append((uid, gid))):
            self.assertTrue(grant_certificate_access(layout))
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(fullchain).st_mode), 0o644)
        self.assertIn((-1, os.getgid()), chown_calls)

    def test_renewal_hook_repairs_parent_and_uses_acl(self):
        from unittest.mock import patch
        from proctor.provision import install_renewal_hook
        captured = {}
        layout = SimpleNamespace(user="prosody", group="prosody",
                                 service_name="prosody")
        with patch("proctor.provision.ensure_dir"), \
                patch("proctor.provision.atomic_write",
                      lambda path, data, mode=None: captured.update(
                          path=path, data=data)):
            install_renewal_hook(layout)
        text = captured["data"].decode()
        self.assertIn("setfacl", text)
        self.assertIn("u:prosody:x /etc/letsencrypt", text)
        self.assertIn("/etc/letsencrypt/live", text)
        self.assertIn("/etc/letsencrypt/archive", text)
        self.assertIn("chmod 711 /etc/letsencrypt", text)

    def test_finalize_restore_generates_nginx_config(self):
        from unittest.mock import patch
        from proctor.provision import WebServerInfo, finalize_restore
        web = WebServerInfo()
        web.kind = "nginx"
        prepared = {
            "web": web,
            "domains": ["example.com"],
            "cert_name": "example.com",
            "proxy": True,
            "proxy_paths": ["/http-bind", "/xmpp-websocket"],
            "backend_host": "127.0.0.1",
            "backend_port": 5280,
            "issue_certbot": False,
            "skipped": False,
        }
        config = SimpleNamespace(host=SimpleNamespace(family=lambda: "debian"))
        options = SimpleNamespace(dry_run=False, email=None, assume_yes=False,
                                  install_missing=None)
        manifest = {"source": {"webserver": {"kind": "nginx", "proxied": True}}}
        captured = {}
        def fake_apply(_config, _info, text, _reporter, dry_run=False):
            captured["text"] = text
            return {"path": "/etc/nginx/conf.d/proctor-xmpp.conf"}
        with patch("proctor.provision.apply_web_config", fake_apply), \
                patch("proctor.provision.has_certificate", lambda name: False):
            result = finalize_restore(config, manifest, None, prepared, options,
                                      SimpleNamespace(warn=lambda message: None),
                                      SimpleNamespace(interactive=False))
        self.assertEqual(result["kind"], "nginx")
        self.assertFalse(result["tls"])
        self.assertIn("server_name example.com", captured["text"])
        self.assertIn("proxy_pass http://127.0.0.1:5280", captured["text"])
        self.assertNotIn("ssl_certificate", captured["text"])

    def test_restore_plan_uses_recorded_webserver(self):
        from proctor.provision import Prompter, ProvisionOptions, prepare_restore
        host = SimpleNamespace(
            system_id="linux", defaults={"config_file": "/nonexistent/prosody.cfg.lua"},
            package_manager="dnf", pretty_name="test", family=lambda: "fedora",
        )
        config = SimpleNamespace(host=host)
        manifest = {
            "source": {
                "prosody": {"virtualhosts": ["example.com"]},
                "webserver": {"kind": "nginx", "proxied": True,
                              "proxy_paths": ["/http-bind"]},
                "letsencrypt": {"present": False, "cert_names": [], "domains": []},
            }
        }
        options = ProvisionOptions(dry_run=True, install_missing=True,
                                   webserver="auto", certbot="no",
                                   assume_yes=True)
        result = prepare_restore(config, manifest, options,
                                 SimpleNamespace(info=lambda m: None,
                                                 warn=lambda m: None,
                                                 step=lambda m: None),
                                 Prompter(assume_yes=True))
        self.assertTrue(result["plan"]["install_prosody"])
        self.assertEqual(result["plan"]["webserver"], "nginx")

    def test_renewal_configs_are_converted_to_webroot(self):
        import tempfile
        from unittest.mock import patch
        from proctor.provision import configure_renewal_webroot
        root = tempfile.mkdtemp()
        renewal = os.path.join(root, "renewal")
        webroot = os.path.join(root, "webroot")
        os.makedirs(renewal)
        os.makedirs(webroot)
        nginx_conf = os.path.join(renewal, "example.com.conf")
        with open(nginx_conf, "w") as handle:
            handle.write(
                "[renewalparams]\n"
                "authenticator = nginx\n"
                "installer = nginx\n"
                "account = abc\n"
            )
        dns_conf = os.path.join(renewal, "wild.conf")
        with open(dns_conf, "w") as handle:
            handle.write("[renewalparams]\nauthenticator = dns-cloudflare\n")
        with patch("proctor.provision.CERTBOT_RENEWAL", renewal), \
                patch("proctor.provision.WEBROOT", webroot):
            changed = configure_renewal_webroot()
        self.assertEqual(changed, 1)
        with open(nginx_conf) as handle:
            text = handle.read()
        self.assertIn("authenticator = webroot", text)
        self.assertIn("webroot_path = %s," % webroot, text)
        self.assertNotIn("installer = nginx", text)
        with open(dns_conf) as handle:
            self.assertIn("dns-cloudflare", handle.read())

    def test_setup_dry_run(self):
        code, payload, stderr = run_json([
            "setup", "--dry-run", "--domain", "example.com",
            "--webserver", "nginx", "--certbot", "no",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["domains"], ["example.com"])
        self.assertEqual(payload["webserver"], "nginx")
        self.assertIn('VirtualHost "example.com"', payload["prosody_config_preview"])
        self.assertIn("server_name example.com conference.example.com "
                      "upload.example.com", payload["web_config_preview"])


class PathMapperTests(unittest.TestCase):
    def test_only_certs_includes_letsencrypt(self):
        from proctor.restore import RestoreOptions, _selected
        options = RestoreOptions(only={"certs"})
        letsencrypt = {"component": "letsencrypt",
                       "path": "/etc/letsencrypt/live/example.com/fullchain.pem"}
        local = {"component": "certs", "path": "/etc/prosody/certs/example.pem"}
        config = {"component": "config", "path": "/etc/prosody/prosody.cfg.lua"}
        self.assertTrue(_selected(letsencrypt, options, [], []))
        self.assertTrue(_selected(local, options, [], []))
        self.assertFalse(_selected(config, options, [], []))

    def test_rewrite_does_not_cascade(self):
        from proctor.restore import PathMapper
        mapper = PathMapper([("/var/lib/prosody", "/var/lib/prosody-new"),
                             ("/etc/prosody", "/etc/prosody")])
        data = b'data_path = "/var/lib/prosody"\n'
        self.assertEqual(mapper.rewrite_bytes(data),
                         b'data_path = "/var/lib/prosody-new"\n')

    def test_nested_mapping_wins(self):
        from proctor.restore import PathMapper
        mapper = PathMapper([("/etc/prosody", "/opt/prosody"),
                             ("/etc/prosody/certs", "/opt/certs")])
        self.assertEqual(mapper.map("/etc/prosody/certs/x.pem"), "/opt/certs/x.pem")
        self.assertEqual(mapper.map("/etc/prosody/conf.d/x.lua"),
                         "/opt/prosody/conf.d/x.lua")


class ConfigAndCliTests(unittest.TestCase):
    def test_explicit_missing_config_errors(self):
        with self.assertRaises(ConfigError):
            load_config("/definitely/not/here/proctor.json", allow_missing=False)

    def test_init_print_and_write(self):
        root = tempfile.mkdtemp()
        code, payload, stderr = run_json([
            "init", "--print", "--repo", os.path.join(root, "repo"),
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["version"], 1)
        path = os.path.join(root, "config.json")
        code, payload, stderr = run_json([
            "init", "--path", path, "--repo", os.path.join(root, "repo"),
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["config"], path)
        self.assertTrue(os.path.isfile(path))
        config = load_config(path)
        self.assertEqual(config.repository, os.path.join(root, "repo"))

    def test_discover_and_doctor(self):
        root = tempfile.mkdtemp()
        fixture = make_prosody(root)
        code, payload, stderr = run_json([
            "discover", "--repo", fixture["repo"],
            "--config", os.path.join(root, "nonexistent.json"),
        ])
        self.assertEqual(code, 2)  # explicit missing config is an error
        code, payload, stderr = run_json([
            "discover", "--repo", fixture["repo"],
        ])
        # discover without a config does not auto-detect a temp Prosody tree,
        # but it must still return a valid host report.
        self.assertEqual(code, 0, stderr)
        self.assertIn("host", payload)

    def test_doctor_on_fixture(self):
        root = tempfile.mkdtemp()
        fixture = make_prosody(root)
        config_path = os.path.join(root, "proctor.json")
        code, payload, stderr = run_json([
            "init", "--path", config_path, "--repo", fixture["repo"],
            "--prosody-config", fixture["config"],
        ])
        self.assertEqual(code, 0, stderr)
        code, payload, stderr = run_json([
            "doctor", "--config", config_path, "--no-config-check",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["ok"])
        names = [item["name"] for item in payload["checks"]]
        self.assertIn("platform", names)

    def test_compression_roundtrip_and_verify(self):
        root = tempfile.mkdtemp()
        fixture = make_prosody(root)
        for compression in ("xz", "zstd", "none"):
            if compression == "zstd":
                try:
                    from compression import zstd  # noqa: F401
                except ImportError:
                    if not shutil.which("zstd"):
                        continue
            code, payload, stderr = run_json([
                "backup", "--repo", fixture["repo"], "--live", "--no-system",
                "--prosody-config", fixture["config"], "--compression", compression,
                "--verify", "--note", compression,
            ])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["compression"], compression)
            code, verify, stderr = run_json([
                "verify", payload["id"], "--repo", fixture["repo"],
            ])
            self.assertEqual(code, 0, stderr)
            self.assertTrue(verify["members_ok"])

    @unittest.skipUnless(shutil.which("gpg"), "gpg is not installed")
    def test_gpg_encrypted_backup_and_verify(self):
        root = tempfile.mkdtemp()
        fixture = make_prosody(root)
        gnupg = os.path.join(root, "gnupg")
        os.makedirs(gnupg, mode=0o700)
        env = dict(os.environ)
        env["GNUPGHOME"] = gnupg
        generated = subprocess.run(
            ["gpg", "--batch", "--passphrase", "", "--quick-generate-key",
             "Proctor Test <proctor@example.invalid>", "default", "default", "never"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        if generated.returncode != 0:
            self.skipTest("could not generate a temporary GPG key")
        listing = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-keys"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        ).stdout.decode("utf-8", "replace")
        fingerprint = None
        for line in listing.splitlines():
            if line.startswith("fpr:"):
                fingerprint = line.split(":")[9]
                break
        self.assertIsNotNone(fingerprint)
        config_path = os.path.join(root, "proctor.json")
        with open(config_path, "w") as handle:
            json.dump({
                "version": 1,
                "repository": fixture["repo"],
                "prosody": {"config": fixture["config"]},
                "encryption": {"method": "gpg", "recipients": [fingerprint]},
                "backup": {"include_system": False, "include_logs": False},
            }, handle)
        code, payload, stderr = run_json([
            "backup", "--config", config_path, "--live", "--encrypt", "--verify",
        ], env={"GNUPGHOME": gnupg})
        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["encrypted"])
        self.assertTrue(payload["archive"].endswith(".gpg"))
        code, verify, stderr = run_json([
            "verify", payload["id"], "--config", config_path,
        ], env={"GNUPGHOME": gnupg})
        self.assertEqual(code, 0, stderr)
        self.assertTrue(verify["members_ok"])
        self.assertTrue(verify["manifest_ok"])
        code, users, stderr = run_json([
            "users", payload["id"], "--config", config_path,
        ], env={"GNUPGHOME": gnupg})
        self.assertEqual(code, 0, stderr)
        self.assertIn("alice@example.com", users["users"])

    def test_include_path_adds_extra_component(self):
        root = tempfile.mkdtemp()
        fixture = make_prosody(root)
        extra = os.path.join(root, "proxy", "nginx.conf")
        os.makedirs(os.path.dirname(extra))
        with open(extra, "w") as handle:
            handle.write("server { listen 5281 ssl; }\n")
        code, payload, stderr = run_json([
            "backup", "--repo", fixture["repo"], "--live", "--no-system",
            "--prosody-config", fixture["config"], "--include-path", extra,
        ])
        self.assertEqual(code, 0, stderr)
        manifest_path = os.path.join(fixture["repo"], "backups",
                                     payload["id"] + ".manifest.json")
        with open(manifest_path) as handle:
            manifest = json.load(handle)["manifest"]
        extras = [entry for entry in manifest["entries"] if entry["component"] == "extra"]
        self.assertEqual(len(extras), 1)
        self.assertEqual(extras[0]["path"], extra)
        self.assertIn("extra", manifest["components"])

    def test_backup_only_config_skips_database(self):
        root = tempfile.mkdtemp()
        fixture = make_prosody(root)
        code, payload, stderr = run_json([
            "backup", "--repo", fixture["repo"], "--live", "--no-system",
            "--prosody-config", fixture["config"], "--only", "config",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertIsNone(payload["database"])
        manifest_path = os.path.join(fixture["repo"], "backups",
                                     payload["id"] + ".manifest.json")
        import json
        with open(manifest_path) as handle:
            manifest = json.load(handle)["manifest"]
        components = set(item["component"] for item in manifest["entries"])
        self.assertEqual(components, {"config"})


if __name__ == "__main__":
    unittest.main()
