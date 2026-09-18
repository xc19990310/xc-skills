"""Only synthetic encrypted fixtures; never read account config or real WeChat."""
import ctypes
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import encrypted_snapshot as snapshots
import decrypt_all_dbs as decrypt
import extract_keys as extract
import private_io
import vault_cli

KEY = "01" * 32  # Synthetic public test key, never a real account key.
LIBRARY = "/opt/homebrew/opt/sqlcipher/lib/libsqlcipher.dylib"


class CipherFixture:
    def __init__(self, path):
        self.lib = ctypes.CDLL(LIBRARY)
        self.lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self.lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
        self.lib.sqlite3_close.argtypes = [ctypes.c_void_p]
        self.db = ctypes.c_void_p()
        path.parent.mkdir(parents=True, exist_ok=True)
        assert self.lib.sqlite3_open(os.fsencode(path), ctypes.byref(self.db)) == 0
        self.execute(f'''PRAGMA key="x'{KEY}'"; PRAGMA cipher_compatibility=4;
            CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT);
            INSERT INTO messages VALUES(1, 'base');
            PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;''')

    def execute(self, sql):
        error = ctypes.c_char_p()
        result = self.lib.sqlite3_exec(self.db, sql.encode(), None, None, ctypes.byref(error))
        if result:
            raise AssertionError("synthetic fixture SQL failed")

    def close(self):
        self.lib.sqlite3_close(self.db)


class MacSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechat-synthetic-")
        self.root = Path(self.temp.name)
        self.src = self.root / "source/message/message_0.db"
        self.dst = self.root / "vault/decrypted/current/message/message_0.db"
        self.fixture = CipherFixture(self.src)

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def contents(self):
        with snapshots.read_only(self.dst) as con:
            return con.execute("SELECT content FROM messages ORDER BY id").fetchall()

    def test_committed_wal_is_included_without_modifying_live_files(self):
        base_hash = hashlib.sha256(self.src.read_bytes()).hexdigest()
        self.fixture.execute("INSERT INTO messages VALUES(2, 'WAL only');")
        self.assertEqual(base_hash, hashlib.sha256(self.src.read_bytes()).hexdigest())
        before = snapshots.source_fingerprint(self.src, KEY)
        snapshots.decrypt_db(self.src, self.dst, KEY)
        self.assertEqual(self.contents(), [("base",), ("WAL only",)])
        self.assertEqual(before, snapshots.source_fingerprint(self.src, KEY))
        self.assertEqual(stat.S_IMODE(self.dst.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dst.parent.stat().st_mode), 0o700)

    def test_hidden_sender_rowids_and_database_metadata_preserved(self):
        self.fixture.execute("CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id(rowid,user_name) VALUES(2,'two'),(55,'fiftyfive'); PRAGMA user_version=57;")
        snapshots.decrypt_db(self.src, self.dst, KEY)
        with snapshots.read_only(self.dst) as con:
            self.assertEqual(con.execute("SELECT rowid,user_name FROM Name2Id").fetchall(), [(2, "two"), (55, "fiftyfive")])
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 57)

    def test_uncommitted_writes_not_exported(self):
        self.fixture.execute("INSERT INTO messages VALUES(2, 'committed'); BEGIN; INSERT INTO messages VALUES(3, 'uncommitted');")
        snapshots.decrypt_db(self.src, self.dst, KEY)
        self.assertEqual(self.contents(), [("base",), ("committed",)])
        self.fixture.execute("ROLLBACK;")

    def test_wrong_key_keeps_previous_snapshot(self):
        snapshots.decrypt_db(self.src, self.dst, KEY)
        old = self.dst.read_bytes()
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.decrypt_db(self.src, self.dst, "02" * 32)
        self.assertEqual(old, self.dst.read_bytes())
        self.assertEqual(list(self.dst.parent.glob(".snapshot-*")), [])

    def test_damaged_wal_rejected_instead_of_silent_old_snapshot(self):
        snapshots.decrypt_db(self.src, self.dst, KEY)
        old = self.dst.read_bytes()
        self.fixture.execute("INSERT INTO messages VALUES(2, 'new');")
        wal = Path(str(self.src) + "-wal")
        original = wal.read_bytes()
        changed = bytearray(original)
        changed[100] ^= 0xFF
        wal.write_bytes(changed)
        try:
            with self.assertRaisesRegex(snapshots.SnapshotError, "checksum"):
                snapshots.decrypt_db(self.src, self.dst, KEY)
            self.assertEqual(old, self.dst.read_bytes())
        finally:
            wal.write_bytes(original)

    def test_wal_salt_damage_does_not_publish_old_main_database(self):
        self.fixture.execute("INSERT INTO messages VALUES(2, 'committed in WAL');")
        wal = Path(str(self.src) + "-wal")
        original = wal.read_bytes()
        damaged = bytearray(original)
        damaged[40] ^= 1
        wal.write_bytes(damaged)
        try:
            with self.assertRaisesRegex(snapshots.SnapshotError, "salt mismatch"):
                snapshots.decrypt_db(self.src, self.dst, KEY)
            self.assertFalse(self.dst.exists())
        finally:
            wal.write_bytes(original)

    def test_empty_recycled_wal_requires_valid_matching_index(self):
        self.fixture.execute("INSERT INTO messages VALUES(2, 'checkpointed'); PRAGMA wal_checkpoint(FULL);")
        wal = Path(str(self.src) + "-wal")
        shm = Path(str(self.src) + "-shm")
        original_wal, original_shm = wal.read_bytes(), shm.read_bytes()
        data, index = bytearray(original_wal), bytearray(original_shm)
        order = "<" if sys.byteorder == "little" else ">"
        def checksum(raw, endian):
            words = struct.unpack(endian + str(len(raw)//4) + "I", raw)
            a = b = 0
            for i in range(0, len(words), 2):
                a = (a + words[i] + b) & 0xFFFFFFFF
                b = (b + words[i+1] + a) & 0xFFFFFFFF
            return a, b
        # Model a recycled cycle: no current frames, checkpointed DB, old tail.
        data[16] ^= 1
        endian = "<" if struct.unpack(">I", data[:4])[0] == 0x377F0682 else ">"
        data[24:32] = struct.pack(">2I", *checksum(data[:24], endian))
        index[16:20] = bytes(4)
        index[32:40] = data[16:24]
        index[40:48] = struct.pack(order + "2I", *checksum(index[:40], order))
        index[48:96] = index[:48]
        index[96:100] = bytes(4)
        index[128:132] = bytes(4)
        wal.write_bytes(data)
        shm.write_bytes(index)
        try:
            snapshots.decrypt_db(self.src, self.dst, KEY)
            self.assertEqual(self.contents(), [("base",), ("checkpointed",)])
            previous = self.dst.read_bytes()
            index[8] ^= 1
            shm.write_bytes(index)
            with self.assertRaises(snapshots.SnapshotError):
                snapshots.decrypt_db(self.src, self.dst, KEY)
            self.assertEqual(previous, self.dst.read_bytes())
            shm.unlink()
            with self.assertRaises(snapshots.SnapshotError):
                snapshots.decrypt_db(self.src, self.dst, KEY)
        finally:
            wal.write_bytes(original_wal)
            shm.write_bytes(original_shm)

    def test_incremental_detects_only_wal_changes_and_output_damage(self):
        fingerprint = snapshots.decrypt_db(self.src, self.dst, KEY)
        rel = "message/message_0.db"
        state = {rel: {"source": fingerprint, "output_sha256": hashlib.sha256(self.dst.read_bytes()).hexdigest()}}
        self.assertTrue(decrypt.unchanged(self.src, self.dst, KEY, state, rel))
        self.fixture.execute("INSERT INTO messages VALUES(2, 'new');")
        self.assertFalse(decrypt.unchanged(self.src, self.dst, KEY, state, rel))
        fingerprint = snapshots.decrypt_db(self.src, self.dst, KEY)
        state[rel]["source"] = fingerprint
        state[rel]["output_sha256"] = hashlib.sha256(self.dst.read_bytes()).hexdigest()
        self.dst.write_bytes(b"broken")
        self.assertFalse(decrypt.unchanged(self.src, self.dst, KEY, state, rel))

    def test_source_change_mid_copy_does_not_publish(self):
        real_copy = snapshots.shutil.copyfileobj
        def racing_copy(inp, out):
            real_copy(inp, out)
            self.fixture.execute("INSERT INTO messages(content) VALUES('race');")
        with mock.patch.object(snapshots.shutil, "copyfileobj", side_effect=racing_copy):
            with self.assertRaises(snapshots.SnapshotError):
                snapshots.decrypt_db(self.src, self.dst, KEY)
        self.assertFalse(self.dst.exists())

    def test_raw_key_not_passed_in_process_arguments(self):
        real_run = snapshots.subprocess.run
        calls = []
        def checked_run(argv, **kwargs):
            calls.append(argv)
            self.assertNotIn(KEY, repr(argv))
            self.assertNotIn(KEY, repr(kwargs.get("env")))
            return real_run(argv, **kwargs)
        with mock.patch.object(snapshots.subprocess, "run", side_effect=checked_run):
            snapshots.decrypt_db(self.src, self.dst, KEY)
        self.assertTrue(calls)

    def test_query_open_cannot_create_or_write(self):
        with self.assertRaises(sqlite3.OperationalError):
            vault_cli.connect(self.root / "missing.db")
        self.assertFalse((self.root / "missing.db").exists())
        snapshots.decrypt_db(self.src, self.dst, KEY)
        with vault_cli.connect(self.dst) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM messages")

    def test_refresh_exit_status_manifest_and_old_snapshot(self):
        vault = self.root / "vault"
        config = self.root / "config.json"
        keys = self.root / "keys.json"
        config.write_text(json.dumps({"db_base_path": str(self.src.parent.parent)}))
        keys.write_text(json.dumps({"message_0": KEY}))
        with mock.patch.multiple(decrypt, CONFIG_FILE=config, KEYS_FILE=keys,
                                 DEFAULT_VAULT_DIR=vault, DECRYPT_STATE_FILE=vault / "state/decrypt_state.json"), \
             mock.patch.object(sys, "argv", ["decrypt", "--mode", "incremental", "-o", str(self.dst.parent.parent)]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(decrypt.main(), 0)
            manifest_path = self.dst.parent.parent / "refresh_status.json"
            first = json.loads(manifest_path.read_text())
            self.assertTrue(first["complete"])
            self.assertEqual(decrypt.main(), 0)
            self.assertEqual(json.loads(manifest_path.read_text())["records"][0]["status"], "unchanged")
            keys.write_text(json.dumps({"message_0": "02" * 32}))
            self.assertEqual(decrypt.main(), 2)
            failure = json.loads(manifest_path.read_text())
            self.assertFalse(failure["complete"])
            self.assertTrue(failure["records"][0]["previous_retained"])
            self.assertEqual(self.contents(), [("base",)])
            self.assertEqual(len(list((vault / "manifests").glob("*.json"))), 3)
            orphan = self.dst.parent / "message_99.db"
            orphan.write_bytes(self.dst.read_bytes())
            keys.write_text(json.dumps({"message_0": KEY}))
            self.assertEqual(decrypt.main(), 2)
            retained = json.loads(manifest_path.read_text())
            self.assertFalse(retained["complete"])
            self.assertEqual(retained["records"][-1]["status"], "retained_orphan")

    def test_no_keys_is_not_a_success(self):
        vault = self.root / "empty-vault"
        with mock.patch.object(decrypt, "resolve_db_base", return_value=self.src.parent.parent), \
             mock.patch.object(decrypt, "load_config", return_value={}), \
             mock.patch.multiple(decrypt, CONFIG_FILE=self.root / "unused-config.json", KEYS_FILE=self.root / "missing-keys.json",
                                 DEFAULT_VAULT_DIR=vault, DECRYPT_STATE_FILE=vault / "state/decrypt_state.json"), \
             mock.patch.object(sys, "argv", ["decrypt", "-o", str(vault / "current")]), redirect_stdout(io.StringIO()):
            self.assertEqual(decrypt.main(), 2)
            data = json.loads((vault / "current/refresh_status.json").read_text())
            self.assertFalse(data["complete"])
            self.assertEqual(data["records"][0]["status"], "missing_key")


class PrivateFileTests(unittest.TestCase):
    def test_contact_binary_text_metadata_does_not_break_lookup(self):
        from fixture_factory import build
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            build(root)
            con = sqlite3.connect(root / "contact/contact.db")
            try:
                con.execute("ALTER TABLE contact ADD COLUMN big_head_url TEXT")
                con.execute("UPDATE contact SET big_head_url=CAST(X'FFFE' AS TEXT)")
                con.commit()
            finally:
                con.close()
            contacts, _ = vault_cli.load_contacts(root)
            self.assertTrue(contacts)
            self.assertTrue(any("测试" in c["display_name"] for c in contacts.values()))

    def test_export_symlink_protected_roots_and_private_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            database = source / "messages.db"
            database.write_bytes(b"original database")
            link = root / "report.md"
            link.symlink_to(database)
            with self.assertRaises(ValueError):
                private_io.atomic_text(link, "would overwrite")
            self.assertEqual(database.read_bytes(), b"original database")
            with self.assertRaises(ValueError):
                private_io.atomic_text(source / "report.md", "blocked", forbidden=(source,))
            with self.assertRaises(ValueError):
                private_io.atomic_text(database, "blocked")
            destination = root / "allowed.md"
            destination.write_text("old")
            os.chmod(destination, 0o644)
            private_io.atomic_text(destination, "new")
            self.assertEqual(destination.read_text(), "new")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_mac_export_entrypoint_rejects_link_and_digest_defaults_private(self):
        from fixture_factory import build, GROUP
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = root / "synthetic"
            build(source)
            protected = root / "live.db"
            protected.write_bytes(b"unchanged")
            link = root / "report.md"
            link.symlink_to(protected)
            parser = vault_cli.build_parser()
            args = parser.parse_args(["--decrypted-dir", str(source), "export", GROUP, "--output", str(link)])
            with mock.patch.object(vault_cli, "load_config", return_value={}), redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    vault_cli.command_export(args)
                self.assertEqual(protected.read_bytes(), b"unchanged")
                args = parser.parse_args(["--decrypted-dir", str(source), "digest-source", GROUP])
                with mock.patch.object(vault_cli, "DEFAULT_EXPORTS_DIR", root / "private-exports"):
                    vault_cli.command_digest_source(args)
                self.assertTrue(list((root / "private-exports/digests").rglob("*.json")))

    def test_private_files_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "private/capture.jsonl"
            private_io.private_log(path)
            private_io.atomic_json(root / "keys.json", {"synthetic": KEY})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / "keys.json").stat().st_mode), 0o600)
            link = root / "private/link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                private_io.private_log(link, reset=True)
            with self.assertRaises(ValueError):
                private_io.atomic_json(link, {})

    def test_multiple_accounts_are_not_selected_implicitly(self):
        with mock.patch.object(extract, "load_config", return_value={}), \
             mock.patch.object(extract.glob, "glob", return_value=["/fake/a/db_storage", "/fake/b/db_storage"]):
            with self.assertRaisesRegex(SystemExit, "Multiple accounts"):
                extract.find_db_base()

    def test_original_app_cannot_be_resigned(self):
        with self.assertRaisesRegex(SystemExit, "original app"):
            extract.prepare_wechat(extract.WECHAT_APP, False)

    def test_list_dbs_has_no_dependency_install_or_account_leak(self):
        with mock.patch.object(sys, "argv", ["extract", "--list-dbs"]), \
             mock.patch.object(extract, "check_env") as check, \
             mock.patch.object(extract, "find_db_base", return_value=("secret_account", Path("/secret_account/db_storage"))), \
             mock.patch.object(extract, "load_config", return_value={}), \
             mock.patch.object(extract, "collect_db_info", return_value={}), \
             mock.patch.object(extract, "run_cmd", side_effect=AssertionError("No process allowed")), \
             redirect_stdout(io.StringIO()) as out:
            extract.main()
            check.assert_called_once_with(capture=False, match=False)
            self.assertNotIn("secret_account", out.getvalue())

    def test_path_traversal_key_rejected(self):
        self.assertIsNone(decrypt.key_name_to_rel("../../escape.db"))
        self.assertIsNone(decrypt.key_name_to_rel("/tmp/escape.db"))


if __name__ == "__main__":
    unittest.main()
