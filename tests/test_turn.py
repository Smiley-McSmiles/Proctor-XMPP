from __future__ import print_function

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from proctor.turn import (
    apply_turnserver_config, check_turn_auth, detect_coturn, ensure_turn_firewall,
    ensure_turn_selinux, firewall_state, parse_turnserver_conf,
    prosody_turn_settings, patch_prosody_config, turn_checks,
    turn_firewalld_service_xml, turn_ports, turn_web_config_text,
    turnserver_config_text,
)


class TurnParserTests(unittest.TestCase):
    def _write(self, text):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "turnserver.conf")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_parses_auth_realm_ports_and_relay(self):
        path = self._write(
            "# comment\n"
            "listening-port=3478\n"
            "tls-listening-port=5349\n"
            "min-port=49152\n"
            "max-port=65535\n"
            "realm=turn.example.com\n"
            "use-auth-secret\n"
            "static-auth-secret=abc123\n"
            "fingerprint\n"
            "user=coturn\ngroup=coturn\n"
            "external-ip=203.0.113.10\n"
        )
        turn = parse_turnserver_conf(path)
        self.assertTrue(turn.exists)
        self.assertEqual(turn.realm, "turn.example.com")
        self.assertEqual(turn.secret, "abc123")
        self.assertTrue(turn.use_auth_secret)
        self.assertFalse(turn.no_auth)
        self.assertEqual(turn.min_port, 49152)
        self.assertEqual(turn.external_ip, "203.0.113.10")
        self.assertEqual(turn.errors, [])

    def test_flags_and_conflicting_auth(self):
        path = self._write(
            "realm=turn.example.com\n"
            "use-auth-secret\n"
            "lt-cred-mech\n"
            "static-auth-secret=x\n"
            "stun-only\n"
            "min-port=60000\n"
            "max-port=50000\n"
        )
        turn = parse_turnserver_conf(path)
        self.assertTrue(turn.stun_only)
        self.assertTrue(any("use-auth-secret and lt-cred-mech" in item for item in turn.errors))
        self.assertTrue(any("min-port" in item for item in turn.errors))

    def test_missing_file(self):
        turn = detect_coturn("/definitely/not/here/turnserver.conf")
        self.assertFalse(turn.exists)
        self.assertIsNone(turn.realm)


class TurnConfigTests(unittest.TestCase):
    def _turn(self, **overrides):
        from proctor.turn import TurnConfig
        turn = TurnConfig()
        for key, value in overrides.items():
            setattr(turn, key, value)
        return turn

    def test_generated_config_contains_hash_rest_api(self):
        turn = self._turn(no_tls=False)
        text = turnserver_config_text(
            turn, "secret123", "turn.example.com",
            cert="/etc/letsencrypt/live/turn.example.com/fullchain.pem",
            pkey="/etc/letsencrypt/live/turn.example.com/privkey.pem")
        self.assertIn("realm=turn.example.com", text)
        self.assertIn("use-auth-secret", text)
        self.assertIn("static-auth-secret=secret123", text)
        self.assertIn("min-port=49152", text)
        self.assertIn("cert=/etc/letsencrypt/live/turn.example.com/fullchain.pem", text)
        self.assertNotIn("no-auth", text)
        self.assertNotIn("lt-cred-mech", text)

    def test_no_tls_without_certificate(self):
        turn = self._turn()
        text = turnserver_config_text(turn, "s", "turn.example.com", no_tls=True)
        self.assertIn("no-tls", text)
        self.assertNotIn("cert=", text)

    def test_nginx_vhost_has_acme_and_optional_tls(self):
        plain = turn_web_config_text("nginx", "turn.example.com")
        self.assertIn("server_name turn.example.com;", plain)
        self.assertIn("/.well-known/acme-challenge/", plain)
        self.assertIn("root /var/www/html;", plain)
        self.assertNotIn("443 ssl", plain)
        tls = turn_web_config_text("nginx", "turn.example.com",
                                   cert_name="turn.example.com", tls=True)
        self.assertIn("listen 443 ssl;", tls)
        self.assertIn(
            "ssl_certificate     /etc/letsencrypt/live/turn.example.com/fullchain.pem;",
            tls)

    def test_apache_vhost(self):
        text = turn_web_config_text("apache", "turn.example.com")
        self.assertIn("<VirtualHost *:80>", text)
        self.assertIn("ServerName turn.example.com", text)
        self.assertIn("Alias /.well-known/acme-challenge/", text)
        self.assertNotIn("SSLEngine", text)

    def test_ports(self):
        turn = self._turn(port=3478, tls_port=5349, no_udp=False, no_tls=False)
        self.assertEqual(turn_ports(turn), [3478, 5349])
        turn.no_tls = True
        self.assertEqual(turn_ports(turn), [3478])

    def test_firewalld_service_includes_relay_range(self):
        turn = self._turn(port=3478, tls_port=5349)
        xml = turn_firewalld_service_xml(turn)
        self.assertIn('port protocol="udp" port="49152-65535"', xml)
        self.assertIn('port protocol="tcp" port="3478"', xml)
        self.assertIn('port protocol="udp" port="5349"', xml)

    def test_prosody_patch_adds_module_and_settings(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "prosody.cfg.lua")
        with open(path, "w") as handle:
            handle.write(
                'modules_enabled = {\n    "roster";\n}\n\n'
                'VirtualHost "example.com"\n'
            )
        settings = prosody_turn_settings("example.com", "shared-secret")
        preview = patch_prosody_config(path, settings, dry_run=True)
        self.assertIn('"turn_external";', preview)
        self.assertIn('turn_external_host = "turn.example.com"', preview)
        self.assertIn('turn_external_secret = "shared-secret"', preview)
        self.assertIn('turn_external_port = 3478', preview)
        self.assertIn(
            'turn_external_host = "turn.example.com"\n'
            'turn_external_port = 3478\n'
            'turn_external_secret = "shared-secret"\n', preview)
        self.assertTrue(preview.endswith("\n"))
        self.assertNotIn("\n\n\n", preview)

    def test_prosody_patch_replaces_existing_values(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "prosody.cfg.lua")
        with open(path, "w") as handle:
            handle.write(
                'modules_enabled = { "roster"; "turn_external"; }\n'
                'turn_external_host = "old.example.com"\n'
                'turn_external_secret = "old"\n'
            )
        text = patch_prosody_config(
            path, prosody_turn_settings("example.com", "new"), dry_run=True)
        self.assertNotIn("old.example.com", text)
        self.assertIn('turn_external_host = "turn.example.com"', text)
        self.assertIn('turn_external_secret = "new"', text)
        self.assertEqual(text.count('"turn_external"'), 1)


class TurnFirewallSelinuxTests(unittest.TestCase):
    def _turn(self):
        from proctor.turn import TurnConfig
        turn = TurnConfig()
        turn.port = 3478
        turn.tls_port = 5349
        return turn

    def test_firewalld_writes_service_and_reloads(self):
        commands = []
        def fake_run(command, **kwargs):
            commands.append(command)
            if command[:2] == ["firewall-cmd", "--state"]:
                return SimpleNamespace(returncode=0, stdout=b"running\n", stderr=b"")
            if command[:2] == ["firewall-cmd", "--get-default-zone"]:
                return SimpleNamespace(returncode=0, stdout=b"public\n", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        root = tempfile.mkdtemp()
        with patch("proctor.turn.run", fake_run), \
                patch("proctor.turn.which",
                      lambda name: "/usr/bin/firewall-cmd" if "firewall" in name else None), \
                patch("proctor.turn.ensure_dir", lambda path, mode=None: None), \
                patch("proctor.turn.atomic_write",
                      lambda path, data, mode=None: commands.append(["write", path])):
            result = ensure_turn_firewall(self._turn(), SimpleNamespace(firewall="auto"))
        self.assertEqual(result["backend"], "firewalld")
        self.assertIn(["--permanent", "--zone=public", "--add-service=coturn"],
                      [item[1:] for item in commands if len(item) > 1] or [])
        self.assertTrue(any(item[:1] == ["write"] for item in commands))

    def test_ufw_opens_ports_and_range(self):
        commands = []
        def fake_run(command, **kwargs):
            commands.append(command)
            if command[:2] == ["ufw", "status"]:
                return SimpleNamespace(returncode=0, stdout=b"Status: active\n", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch("proctor.turn.run", fake_run), \
                patch("proctor.turn.which",
                      lambda name: "/usr/sbin/ufw" if name == "ufw" else None):
            result = ensure_turn_firewall(self._turn(), SimpleNamespace(firewall="auto"))
        self.assertEqual(result["status"], "opened")
        self.assertIn(["ufw", "allow", "3478/udp"], commands)
        self.assertIn(["ufw", "allow", "49152:65535/udp"], commands)

    def test_firewalld_state_reads_coturn_service_ports(self):
        def fake_run(command, **kwargs):
            if command[:2] == ["firewall-cmd", "--state"]:
                return SimpleNamespace(returncode=0, stdout=b"running\n", stderr=b"")
            if command[:2] == ["firewall-cmd", "--list-all"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"public (active)\n  services: coturn\n", stderr=b"")
            if command == ["firewall-cmd", "--info-service=coturn"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"coturn\n  ports: 3478/tcp 3478/udp 5349/tcp\n",
                    stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch("proctor.turn.run", fake_run), \
                patch("proctor.turn.which",
                      lambda name: "/usr/bin/firewall-cmd" if "firewall" in name else None):
            state = firewall_state(self._turn())
        self.assertEqual(state["missing"], [])
        self.assertTrue(state["ok"])

    def test_semanage_recognizes_unreserved_port_labels(self):
        def fake_run(command, **kwargs):
            if command[:3] == ["semanage", "port", "-l"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"unreserved_port_t tcp 3478\n"
                           b"unreserved_port_t udp 3478\n",
                    stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch("proctor.turn.run", fake_run), \
                patch("proctor.turn.which", lambda name: "/usr/sbin/semanage"):
            from proctor.turn import _semanage_exists
            self.assertTrue(_semanage_exists(3478, "tcp"))
            self.assertTrue(_semanage_exists(3478, "udp"))

    def test_supplied_certificate_enables_turn_tls(self):
        turn = self._turn()
        turn.exists = True
        turn.cert = None
        turn.pkey = None
        text = apply_turnserver_config(
            turn, "secret", "turn.example.com",
            cert="/etc/letsencrypt/live/example.com/fullchain.pem",
            pkey="/etc/letsencrypt/live/example.com/privkey.pem",
            dry_run=True)
        self.assertNotIn("no-tls", text)
        self.assertIn("cert=/etc/letsencrypt/live/example.com/fullchain.pem", text)

    def test_selinux_labels_custom_ports(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["getenforce"]:
                return SimpleNamespace(returncode=0, stdout=b"Enforcing\n", stderr=b"")
            if command[:3] == ["semanage", "port", "-l"]:
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with patch("proctor.turn.run", fake_run), \
                patch("proctor.turn.which", lambda name: "/usr/sbin/%s" % name), \
                patch("proctor.turn._selinux_enabled", lambda: True), \
                patch("proctor.turn.os.path.isdir", lambda path: True):
            result = ensure_turn_selinux(self._turn())
        self.assertEqual(result["state"], "enforcing")
        self.assertTrue(any(call[:4] == ["semanage", "port", "-a", "-t"] for call in calls))


class TurnCheckerTests(unittest.TestCase):
    def _turn(self, **overrides):
        from proctor.turn import TurnConfig
        turn = TurnConfig()
        turn.exists = True
        turn.path = "/etc/coturn/turnserver.conf"
        turn.realm = "turn.example.com"
        turn.use_auth_secret = True
        turn.secret = "shared"
        turn.port = 3478
        for key, value in overrides.items():
            setattr(turn, key, value)
        return turn

    def test_checks_flag_missing_realm_and_auth(self):
        turn = self._turn(realm=None, use_auth_secret=False, secret=None)
        install = SimpleNamespace(modules_enabled=["turn_external"],
                                  parsed={"values": {}})
        with patch("proctor.turn.detect_coturn", lambda: turn), \
                patch("proctor.turn.coturn_installed", lambda: True), \
                patch("proctor.turn.firewall_state",
                      lambda t: {"backend": None, "missing": [], "ok": None}), \
                patch("proctor.turn.selinux_state", lambda: "disabled"), \
                patch("proctor.turn.check_stun",
                      lambda host, port: {"ok": False, "error": "down"}), \
                patch("proctor.turn.check_turn_auth",
                      lambda host, port, realm=None: {"ok": False, "error": "no realm"}):
            checks = dict((name, (status, detail))
                          for name, status, detail in turn_checks(None, install))
        self.assertEqual(checks["coturn-realm"][0], "fail")
        self.assertEqual(checks["coturn-auth"][0], "fail")
        self.assertEqual(checks["prosody-turn"][0], "fail")

    def test_checks_detect_secret_mismatch(self):
        turn = self._turn(secret="coturn-secret")
        install = SimpleNamespace(modules_enabled=["turn_external"],
                                  parsed={"values": {"turn_external_secret": "other"}})
        with patch("proctor.turn.detect_coturn", lambda: turn), \
                patch("proctor.turn.coturn_installed", lambda: True), \
                patch("proctor.turn.firewall_state",
                      lambda t: {"backend": "firewalld", "missing": [3478], "ok": False}), \
                patch("proctor.turn.selinux_state", lambda: "disabled"), \
                patch("proctor.turn.check_stun",
                      lambda host, port: {"ok": True, "port": port}), \
                patch("proctor.turn.check_turn_auth",
                      lambda host, port, realm=None: {"ok": True, "realm": "turn.example.com"}):
            checks = dict((name, (status, detail))
                          for name, status, detail in turn_checks(None, install))
        self.assertEqual(checks["prosody-turn"][0], "fail")
        self.assertIn("does not match", checks["prosody-turn"][1])
        self.assertEqual(checks["coturn-firewall"][0], "fail")

    def test_turn_auth_parsing_reports_realm_and_nonce(self):
        # Build a minimal 401 with REALM and NONCE attributes.
        import struct
        realm = b"turn.example.com"
        nonce = b"abc"
        attrs = struct.pack(">HH", 0x0014, len(realm)) + realm
        attrs += b"\x00" * ((4 - len(realm) % 4) % 4)
        attrs += struct.pack(">HH", 0x0015, len(nonce)) + nonce
        attrs += b"\x00" * ((4 - len(nonce) % 4) % 4)
        packet = struct.pack(">HHI12s", 0x0113, len(attrs), 0x2112A442, b"x" * 12) + attrs
        with patch("proctor.turn._socket_stun_allocate", lambda host, port, timeout: packet):
            result = check_turn_auth("127.0.0.1", 3478, realm="turn.example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["realm"], "turn.example.com")
        self.assertTrue(result["nonce"])


if __name__ == "__main__":
    unittest.main()
