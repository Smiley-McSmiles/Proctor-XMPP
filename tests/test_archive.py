from __future__ import print_function

import json
import os
import tarfile
import tempfile
import unittest

from proctor.archive import (
    ArchiveReader, BackupWriter, detect_compression, read_manifest, validate_member,
    verify_members,
)
from proctor.errors import IntegrityError

from tests.helpers import make_prosody, run_cli, run_json


class ArchiveTests(unittest.TestCase):
    def test_roundtrip_and_verification(self):
        root = tempfile.mkdtemp()
        source = os.path.join(root, "etc", "prosody")
        os.makedirs(os.path.join(source, "certs"))
        with open(os.path.join(source, "prosody.cfg.lua"), "w") as handle:
            handle.write("VirtualHost \"example.com\"\n")
        with open(os.path.join(source, "certs", "cert.pem"), "w") as handle:
            handle.write("cert\n")
        os.symlink("prosody.cfg.lua", os.path.join(source, "alias.lua"))
        os.makedirs(os.path.join(source, "empty"))
        archive = os.path.join(root, "backup.tar.gz")
        writer = BackupWriter(archive, "gz")
        writer.add_tree(source, "config")
        manifest = {
            "format": "proctor-backup", "format_version": 1, "id": "test",
            "entries": writer.entries, "extras": writer.extras,
        }
        writer.add_bytes("manifest.json", (json.dumps(manifest) + "\n").encode(), record=False)
        writer.close()
        self.assertEqual(detect_compression(archive), "gz")
        with ArchiveReader(archive, root) as tar:
            embedded = json.loads(read_manifest(archive, root))
            self.assertEqual(embedded["format"], "proctor-backup")
            errors = verify_members(tar, embedded)
        self.assertEqual(errors, [])
        types = {entry["type"] for entry in writer.entries}
        self.assertEqual(types, {"file", "dir", "symlink"})
        symlink = [entry for entry in writer.entries if entry["type"] == "symlink"][0]
        self.assertEqual(symlink["linkname"], "prosody.cfg.lua")

    def test_verify_detects_tampered_member(self):
        root = tempfile.mkdtemp()
        archive = os.path.join(root, "backup.tar")
        writer = BackupWriter(archive, "none")
        writer.add_bytes("files/etc/example", b"original", component="config")
        manifest = {"format": "proctor-backup", "format_version": 1,
                    "entries": writer.entries, "extras": writer.extras}
        writer.add_bytes("manifest.json", json.dumps(manifest).encode(), record=False)
        writer.close()
        # Rebuild an archive with the same manifest but different contents.
        tampered = os.path.join(root, "tampered.tar")
        with tarfile.open(archive, "r") as source, tarfile.open(tampered, "w") as target:
            for member in source:
                if member.name == "files/etc/example":
                    data = b"evil"
                    member.size = len(data)
                    import io
                    target.addfile(member, io.BytesIO(data))
                else:
                    target.addfile(member, source.extractfile(member))
        with ArchiveReader(tampered, root) as tar:
            errors = verify_members(tar, manifest)
        self.assertTrue(any("checksum mismatch" in item for item in errors))

    def test_validate_member_rejects_traversal(self):
        member = tarfile.TarInfo("../etc/passwd")
        with self.assertRaises(IntegrityError):
            validate_member(member)


class RepositoryRetentionTests(unittest.TestCase):
    def _entries(self, dates):
        return [{"id": "id-%d" % index, "created_at": date, "tags": []}
                for index, date in enumerate(dates)]

    def test_retention_keeps_newest_and_buckets(self):
        from proctor.repository import select_retention
        entries = self._entries([
            "2026-10-18T12:00:00Z",
            "2026-10-17T12:00:00Z",
            "2026-10-10T12:00:00Z",
            "2026-09-01T12:00:00Z",
            "2026-01-01T12:00:00Z",
        ])
        keep, remove = select_retention(entries, {
            "keep_last": 1, "keep_daily": 1, "keep_weekly": 0, "keep_monthly": 2,
        })
        keep_ids = {item["id"] for item in keep}
        self.assertIn("id-0", keep_ids)
        self.assertIn("id-3", keep_ids)
        self.assertNotIn("id-1", keep_ids)
        self.assertNotIn("id-4", keep_ids)

    def test_protected_tag_is_never_pruned(self):
        from proctor.repository import select_retention
        entries = self._entries(["2026-10-18T12:00:00Z", "2020-01-01T12:00:00Z"])
        entries[1]["tags"] = ["protected"]
        keep, remove = select_retention(entries, {
            "keep_last": 1, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0,
        })
        self.assertEqual([], remove)


class CliFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.paths = make_prosody(self.root)
        self.backup_args = [
            "backup", "--repo", self.paths["repo"], "--live",
            "--prosody-config", self.paths["config"], "--no-system",
            "--letsencrypt", "never",
        ]

    def _backup(self):
        code, payload, stderr = run_json(self.backup_args + ["--verify", "--note", "test"])
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(payload)
        return payload

    def test_backup_list_info_users_verify(self):
        payload = self._backup()
        backup_id = payload["id"]
        self.assertFalse(payload["encrypted"])
        self.assertEqual(payload["compression"], "gz")
        self.assertGreater(payload["size"], 0)
        self.assertEqual(payload["database"], "sqlite")

        code, listing, stderr = run_json(["list", "--repo", self.paths["repo"]])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(listing["count"], 1)

        code, info, stderr = run_json(["info", backup_id, "--repo", self.paths["repo"]])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(info["manifest"]["id"], backup_id)
        self.assertIn("webserver", info["manifest"]["source"])
        self.assertIn("letsencrypt", info["manifest"]["source"])
        self.assertNotIn("password", json.dumps(info["manifest"]["source"]["prosody"],
                                                sort_keys=True).lower())

        code, users, stderr = run_json(["users", backup_id, "--repo", self.paths["repo"]])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(users["count"], 3)  # 2 SQL + 1 filesystem
        self.assertIn("carol@example.com", users["users"])
        self.assertIn("alice@example.com", users["users"])

        code, verify, stderr = run_json(["verify", backup_id, "--repo", self.paths["repo"]])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(verify["checksum_ok"])
        self.assertTrue(verify["members_ok"])

    def test_database_file_is_not_copied_as_raw_data(self):
        payload = self._backup()
        manifest_path = os.path.join(self.paths["repo"], "backups",
                                     payload["id"] + ".manifest.json")
        with open(manifest_path) as handle:
            manifest = json.load(handle)["manifest"]
        paths = [entry["path"] for entry in manifest["entries"]]
        self.assertNotIn(os.path.join(self.paths["data"], "prosody.sqlite"), paths)

    def test_restore_with_path_mapping_and_database(self):
        payload = self._backup()
        target = os.path.join(self.root, "target")
        target_etc = os.path.join(target, "etc", "prosody")
        target_data = os.path.join(target, "var", "lib", "prosody")
        target_log = os.path.join(target, "var", "log", "prosody")
        os.makedirs(target_etc)
        os.makedirs(target_log)
        target_config = os.path.join(target_etc, "prosody.cfg.lua")
        with open(target_config, "w") as handle:
            handle.write('data_path = "%s"\nstorage = "internal_hashed"\n'
                         'VirtualHost "other.example"\n' % target_data)
        code, dry, stderr = run_json([
            "restore", payload["id"], "--repo", self.paths["repo"], "--yes", "--dry-run",
            "--no-safety-backup", "--no-start", "--prosody-config", target_config,
            "--map", "%s=%s" % (self.paths["etc"], target_etc),
            "--map", "%s=%s" % (self.paths["data"], target_data),
            "--map", "%s=%s" % (self.paths["logs"], target_log),
        ])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(os.path.exists(os.path.join(target_data, "prosody.sqlite")))

        code, restored, stderr = run_json([
            "restore", payload["id"], "--repo", self.paths["repo"], "--yes",
            "--no-safety-backup", "--no-start", "--prosody-config", target_config,
            "--map", "%s=%s" % (self.paths["etc"], target_etc),
            "--map", "%s=%s" % (self.paths["data"], target_data),
            "--map", "%s=%s" % (self.paths["logs"], target_log),
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(restored["extracted"]["files"], 6)
        with open(os.path.join(target_etc, "prosody.cfg.lua")) as handle:
            config_text = handle.read()
        self.assertIn(target_data, config_text)
        self.assertIn(target_log, config_text)
        self.assertNotIn(self.paths["data"], config_text)
        self.assertTrue(os.path.isfile(os.path.join(target_data, "prosody.sqlite")))
        self.assertTrue(os.path.islink(os.path.join(
            target_data, "example.com", "vcards", "alice.vcard")))
        self.assertEqual(os.readlink(os.path.join(
            target_data, "example.com", "vcards", "alice.vcard")), "bob.vcard")
        with open(os.path.join(target_data, "example.com", "accounts", "carol.dat")) as handle:
            self.assertEqual(handle.read(), "carol-account\n")
        import sqlite3
        connection = sqlite3.connect(os.path.join(target_data, "prosody.sqlite"))
        try:
            users = connection.execute(
                'SELECT DISTINCT "user" FROM prosody WHERE store = \'accounts\' ORDER BY 1'
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in users], ["alice", "bob"])

    def test_restore_only_data_keeps_target_config_and_uses_target_db_path(self):
        payload = self._backup()
        target = os.path.join(self.root, "data-only")
        target_etc = os.path.join(target, "etc", "prosody")
        target_data = os.path.join(target, "var", "lib", "prosody")
        os.makedirs(target_etc)
        target_config = os.path.join(target_etc, "prosody.cfg.lua")
        with open(target_config, "w") as handle:
            handle.write('-- KEEP ME\ndata_path = "%s"\nstorage = "sql"\n'
                         'sql = { driver = "SQLite3", database = "prosody.sqlite" }\n'
                         % target_data)
        code, restored, stderr = run_json([
            "restore", payload["id"], "--repo", self.paths["repo"], "--yes",
            "--no-safety-backup", "--no-start", "--only", "data",
            "--prosody-config", target_config,
            "--map", "%s=%s" % (self.paths["data"], target_data),
        ])
        self.assertEqual(code, 0, stderr)
        with open(target_config) as handle:
            self.assertIn("KEEP ME", handle.read())
        self.assertTrue(os.path.isfile(os.path.join(target_data, "prosody.sqlite")))
        self.assertTrue(os.path.isfile(os.path.join(
            target_data, "example.com", "accounts", "carol.dat")))
        self.assertTrue(os.path.isfile(os.path.join(
            target_data, "example.com", "vcards", "bob.vcard")))

    def test_keep_config_preserves_marker_but_restores_certs(self):
        payload = self._backup()
        target = os.path.join(self.root, "keep-target")
        target_etc = os.path.join(target, "etc", "prosody")
        target_data = os.path.join(target, "var", "lib", "prosody")
        os.makedirs(os.path.join(target_etc, "certs"))
        target_config = os.path.join(target_etc, "prosody.cfg.lua")
        with open(target_config, "w") as handle:
            handle.write('-- TARGET MARKER\ndata_path = "%s"\n' % target_data)
        code, restored, stderr = run_json([
            "restore", payload["id"], "--repo", self.paths["repo"], "--yes",
            "--no-safety-backup", "--no-start", "--keep-config",
            "--prosody-config", target_config,
            "--map", "%s=%s" % (self.paths["etc"], target_etc),
            "--map", "%s=%s" % (self.paths["data"], target_data),
        ])
        self.assertEqual(code, 0, stderr)
        with open(target_config) as handle:
            self.assertIn("TARGET MARKER", handle.read())
        self.assertTrue(os.path.isfile(os.path.join(target_etc, "certs", "example.com.crt")))

    def test_export_import_roundtrip(self):
        payload = self._backup()
        bundle = os.path.join(self.root, "site.proctorbundle")
        code, exported, stderr = run_json([
            "export", payload["id"], "--repo", self.paths["repo"], "-o", bundle,
        ])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(os.path.isfile(bundle))
        second_repo = os.path.join(self.root, "repo2")
        code, imported, stderr = run_json([
            "import", bundle, "--repo", second_repo, "--verify",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(imported["imported"])
        self.assertEqual(imported["entry"]["id"], payload["id"])
        code, listing, stderr = run_json(["list", "--repo", second_repo])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(listing["count"], 1)

    def test_corrupt_archive_is_rejected_before_restore(self):
        payload = self._backup()
        archive = os.path.join(self.paths["repo"], "backups", payload["archive"])
        with open(archive, "ab") as handle:
            handle.write(b"corruption")
        code, verify, stderr = run_json([
            "verify", payload["id"], "--repo", self.paths["repo"],
        ])
        self.assertEqual(code, 4)
        self.assertTrue(verify["errors"])
        target = os.path.join(self.root, "corrupt-target")
        target_etc = os.path.join(target, "etc", "prosody")
        os.makedirs(target_etc)
        code, _, stderr = run_json([
            "restore", payload["id"], "--repo", self.paths["repo"], "--yes",
            "--no-safety-backup", "--no-start",
            "--prosody-config", os.path.join(target_etc, "prosody.cfg.lua"),
            "--map", "%s=%s" % (self.paths["etc"], target_etc),
        ])
        self.assertEqual(code, 4)
        self.assertFalse(os.path.exists(os.path.join(target_etc, "prosody.cfg.lua")))

    def test_repository_index_is_rebuilt_from_sidecars(self):
        payload = self._backup()
        for name in ("index.json", ".proctor-repo.json"):
            path = os.path.join(self.paths["repo"], name)
            if os.path.exists(path):
                os.unlink(path)
        code, listing, stderr = run_json(["list", "--repo", self.paths["repo"]])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["backups"][0]["id"], payload["id"])

    def test_restore_install_missing_dry_run_reports_provisioning(self):
        payload = self._backup()
        target = os.path.join(self.root, "provision-target")
        target_etc = os.path.join(target, "etc", "prosody")
        target_data = os.path.join(target, "var", "lib", "prosody")
        code, restored, stderr = run_json([
            "restore", payload["id"], "--repo", self.paths["repo"], "--yes",
            "--dry-run", "--install-missing", "--webserver", "none",
            "--certbot", "no", "--no-safety-backup", "--no-start",
            "--prosody-config", os.path.join(target_etc, "prosody.cfg.lua"),
            "--map", "%s=%s" % (self.paths["etc"], target_etc),
            "--map", "%s=%s" % (self.paths["data"], target_data),
        ])
        self.assertEqual(code, 0, stderr)
        plan = restored["provisioning"]["plan"]
        self.assertTrue(plan["install_prosody"])
        self.assertEqual(plan["webserver"], "none")

    def test_import_from_repository_repairs_missing_sidecar(self):
        payload = self._backup()
        sidecar = os.path.join(self.paths["repo"], "backups",
                               payload["id"] + ".manifest.json")
        os.unlink(sidecar)
        archive = os.path.join(self.paths["repo"], "backups", payload["archive"])
        code, imported, stderr = run_json([
            "import", archive, "--repo", self.paths["repo"],
        ])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(os.path.isfile(sidecar))
        code, verify, stderr = run_json([
            "verify", payload["id"], "--repo", self.paths["repo"],
        ])
        self.assertEqual(code, 0, stderr)

    def test_prune_dry_run_does_not_delete(self):
        payload = self._backup()
        code, result, stderr = run_json(["prune", "--repo", self.paths["repo"]])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(result["dry_run"])
        archive = os.path.join(self.paths["repo"], "backups", payload["archive"])
        self.assertTrue(os.path.isfile(archive))

    def test_delete_requires_dry_run_or_yes(self):
        payload = self._backup()
        code, result, stderr = run_json([
            "delete", payload["id"], "--repo", self.paths["repo"], "--dry-run",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertTrue(result["dry_run"])
        archive = os.path.join(self.paths["repo"], "backups", payload["archive"])
        self.assertTrue(os.path.isfile(archive))


if __name__ == "__main__":
    unittest.main()
