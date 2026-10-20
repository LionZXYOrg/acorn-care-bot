"""Offline migration tests. Every source and outside target is generated locally."""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = (Path(__file__).resolve().parents[1]
          / "greenwich/migrate_storage.py")
SPEC = importlib.util.spec_from_file_location("greenwich_storage_migration_tests", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class GuardTests(unittest.TestCase):
    def test_no_flag_and_nonroot_refuse_before_opening_any_source(self):
        for confirmed, uid in ((False, 0), (False, 10002), (True, 10002)):
            with self.subTest(confirmed=confirmed, uid=uid), \
                    patch.object(migration.os, "geteuid", return_value=uid), \
                    patch.object(migration.os, "open", side_effect=AssertionError("source opened")) as opener:
                with self.assertRaises(migration.MigrationRefused):
                    migration.migrate("/generated-private-source", confirm_workers_stopped=confirmed)
                opener.assert_not_called()

    def test_unsafe_roots_and_lexical_escapes_refuse_before_source_access(self):
        paths = ("/", "/tmp", "/mnt", "/home", "/root", "/var/lib", "/var/lib/kubelet",
                 "/etc/private", "/proc/self", "/dev/example", "/usr/example",
                 "relative", "/tmp/example/../escape", "/tmp/./example", "//tmp/example",
                 "/tmp//example", "/tmp/example/", "/tmp/example\0private")
        with patch.object(migration.os, "geteuid", return_value=0), \
                patch.object(migration.os, "open", side_effect=AssertionError("source opened")) as opener:
            for path in paths:
                with self.subTest(path=path), self.assertRaises(migration.MigrationRefused):
                    migration.migrate(path, confirm_workers_stopped=True)
            opener.assert_not_called()

    def test_cli_never_echoes_paths_arguments_or_internal_errors(self):
        for argv in (["/generated-private-source"],
                     ["/generated-private-source", "--private-argument"],
                     ["/generated-private-source", "--confirm-workers-stopped"]):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(migration, "migrate", side_effect=OSError("generated-private-error")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(migration.main(argv), 1)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), migration._FAILURE + "\n")

    def test_public_api_redacts_source_specific_system_errors(self):
        error = OSError(13, "generated-private-error", "/generated-private-source")
        with patch.object(migration.os, "geteuid", return_value=0), \
                patch.object(migration.sys, "platform", "linux"), \
                patch.object(migration, "_migrate", side_effect=error):
            with self.assertRaises(migration.MigrationRefused) as caught:
                migration.migrate("/generated-private-source", confirm_workers_stopped=True)
        self.assertEqual(str(caught.exception), migration._FAILURE)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_abbreviated_stopped_flag_is_not_confirmation(self):
        err = io.StringIO()
        with patch.object(migration, "migrate") as migrate, contextlib.redirect_stderr(err):
            self.assertEqual(migration.main(["/generated-private-source", "--confirm-workers"]), 1)
        migrate.assert_not_called()
        self.assertEqual(err.getvalue(), migration._FAILURE + "\n")

    def test_help_names_offline_detached_root_only_contract(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            migration.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        for word in ("Offline", "detach", "protected", "root", "--confirm-workers-stopped"):
            self.assertIn(word, out.getvalue())


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0,
                     "Real ownership and UID transition tests require Linux root")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="greenwich-offline-migration-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        # The worker can traverse this root-owned parent, but cannot replace entries.
        self.base.chmod(0o755)
        self.source = self.base / "generated-private-state"
        self.source.mkdir()
        os.chown(self.source, 0, 10001)
        self.source.chmod(0o2770)
        self.entries = [self.source]
        state = {"version": 2, "publications": {"2026-10-19": {
            "sent": {"message_id": 123456}, "uncertain": {"status": "needs-review"}}},
            "alerts": {"synthetic": {"message_id": 654321}}}
        self.state_bytes = (json.dumps(state, indent=2) + "\n").encode()
        self.create("state.json", self.state_bytes)
        self.create("state.json.lock", b"saved lock bytes\x00\n")
        self.create("council-cache.json", b'{"raw":{"generated":"sample"},"version":2}\n')
        nested = self.source / "saved"
        nested.mkdir()
        os.chown(nested, 10001, 10001)
        nested.chmod(0o700)
        self.entries.append(nested)
        self.create("saved/runner-attempts.json", b'{"schema":1,"attempts":{"2026-10-19":"claimed"}}\n')
        self.create("saved/runner-attempts.json.lock", b"")
        # Never parse or rewrite even unknown regular files or malformed JSON.
        self.create("saved/interrupted.json", b"\xffnot-json\x00\n")

    def create(self, name, data):
        path = self.source / name
        path.write_bytes(data)
        os.chown(path, 10001, 10001)
        path.chmod(0o600)
        self.entries.append(path)
        return path

    def snapshot(self):
        result = {}
        for path in self.entries:
            metadata = path.lstat()
            data = None
            if stat.S_ISREG(metadata.st_mode):
                fd = os.open(path, os.O_RDONLY | os.O_NOATIME | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    data = stream.read()
            result[path] = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode),
                            metadata.st_ino, metadata.st_dev, metadata.st_nlink, metadata.st_size,
                            metadata.st_atime_ns, metadata.st_mtime_ns, metadata.st_ctime_ns, data)
        return result

    def cli(self, *args, source=None, **kwargs):
        return subprocess.run([sys.executable, str(SCRIPT), str(source or self.source), *args],
                              text=True, capture_output=True, cwd="/", timeout=15, **kwargs)

    def assert_refused_without_mutation(self, source=None):
        before = self.snapshot()
        result = self.cli("--confirm-workers-stopped", source=source)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, migration._FAILURE + "\n")
        self.assertEqual(self.snapshot(), before)

    def worker_access(self):
        # Execute code supplied by the test, not a script beneath root's home.
        # Clear supplementary groups so GID 10001 cannot mask a failed migration.
        code = """
import fcntl, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
assert os.geteuid() == 10002 and os.getegid() == 10002 and os.getgroups() == []
try:
    assert os.access(root, os.R_OK | os.W_OK | os.X_OK)
    for relative in ('state.json', 'state.json.lock', 'council-cache.json',
                     'saved/runner-attempts.json', 'saved/runner-attempts.json.lock',
                     'saved/interrupted.json'):
        with (root / relative).open('r+b') as stream:
            stream.read()
            if relative.endswith('.lock'):
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except (OSError, AssertionError):
    sys.exit(1)
print('worker access passed')
"""
        return subprocess.run([sys.executable, "-c", code, str(self.source)], cwd="/",
                              user=10002, group=10002, extra_groups=(), text=True,
                              capture_output=True, timeout=15)

    def test_populated_0600_state_with_correct_old_root_group_and_real_worker_access(self):
        self.assertEqual((self.source.stat().st_uid, self.source.stat().st_gid), (0, 10001))
        self.assertEqual(self.worker_access().returncode, 1)
        for path in self.entries:
            os.utime(path, ns=(1234567890000000000, 1234567891000000000))
        before = self.snapshot()
        result = self.cli("--confirm-workers-stopped")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Offline ownership migration complete.\n")
        self.assertEqual(result.stderr, "")
        after = self.snapshot()
        for path in self.entries:
            with self.subTest(entry=path.name):
                self.assertEqual(after[path][:2], (10002, 10002))
                # All metadata except ownership and the unavoidable ctime is intact.
                self.assertEqual(after[path][2:9], before[path][2:9])
                self.assertEqual(after[path][10], before[path][10])
        state = self.source / "state.json"
        self.assertEqual(state.read_bytes(), self.state_bytes)
        self.assertEqual(json.loads(state.read_bytes())["publications"]["2026-10-19"]["uncertain"],
                         {"status": "needs-review"})
        worker = self.worker_access()
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(worker.stdout, "worker access passed\n")

    def test_repeat_run_is_noop_and_preserves_ctime(self):
        migration.migrate(self.source, confirm_workers_stopped=True)
        before = self.snapshot()
        with patch.object(migration.os, "fchown", side_effect=AssertionError("redundant chown")):
            self.assertEqual(migration.migrate(self.source, confirm_workers_stopped=True), 0)
        self.assertEqual(self.snapshot(), before)

    def test_partial_old_and_new_ownership_is_safe_to_finish(self):
        os.chown(self.source / "state.json", 10002, 10002)
        changed = migration.migrate(self.source, confirm_workers_stopped=True)
        self.assertEqual(changed, len(self.entries) - 1)
        self.assertTrue(all(path.stat().st_uid == path.stat().st_gid == 10002 for path in self.entries))

    def test_old_owned_root_and_root_owned_nested_directory_preserve_distinct_modes(self):
        os.chown(self.source, 10001, 10001)
        self.source.chmod(0o750)
        nested = self.source / "saved"
        os.chown(nested, 0, 10001)
        nested.chmod(0o2750)
        (self.source / "council-cache.json").chmod(0o400)
        before = self.snapshot()
        migration.migrate(self.source, confirm_workers_stopped=True)
        after = self.snapshot()
        for path in self.entries:
            self.assertEqual(after[path][:2], (10002, 10002))
            self.assertEqual(after[path][2:9], before[path][2:9])
            self.assertEqual(after[path][10], before[path][10])

    def test_file_capability_is_refused_before_mutation(self):
        before = self.snapshot()
        with patch.object(migration.os, "listxattr", return_value=["security.capability"]), \
                patch.object(migration.os, "fchown") as chown:
            with self.assertRaises(migration.MigrationRefused):
                migration.migrate(self.source, confirm_workers_stopped=True)
            chown.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_io_interruption_is_redacted_and_partial_ownership_can_be_finished(self):
        before = self.snapshot()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(migration.os, "fsync", side_effect=OSError("generated-private-IO-error")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(migration.main([str(self.source), "--confirm-workers-stopped"]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), migration._FAILURE + "\n")
        partial = self.snapshot()
        self.assertTrue(any(item[:2] == (10002, 10002) for item in partial.values()))
        self.assertTrue(any(item[:2] != (10002, 10002) for item in partial.values()))
        migration.migrate(self.source, confirm_workers_stopped=True)
        after = self.snapshot()
        for path in self.entries:
            self.assertEqual(after[path][:2], (10002, 10002))
            self.assertEqual(after[path][2:9], before[path][2:9])
            self.assertEqual(after[path][10], before[path][10])

    def test_nested_symlink_refuses_before_any_chown_including_outside_target(self):
        outside = self.base / "generated-outside.json"
        outside.write_bytes(b"outside generated bytes")
        os.chown(outside, 10001, 10001)
        self.entries.append(outside)
        (self.source / "saved/zz-escape").symlink_to(outside)
        self.assert_refused_without_mutation()

    def test_source_and_ancestor_symlinks_refuse(self):
        alias = self.base / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.assert_refused_without_mutation(source=alias)
        parent_alias = self.base / "parent-alias"
        parent_alias.symlink_to(self.base, target_is_directory=True)
        self.assert_refused_without_mutation(source=parent_alias / self.source.name)

    def test_hard_link_to_outside_is_not_migrated(self):
        outside = self.base / "outside-hardlink"
        os.link(self.source / "state.json", outside)
        self.entries.append(outside)
        self.assert_refused_without_mutation()

    def test_fifo_and_socket_are_refused_without_opening_them(self):
        fifo = self.source / "zz-fifo"
        os.mkfifo(fifo, 0o600)
        os.chown(fifo, 10001, 10001)
        self.assert_refused_without_mutation()
        fifo.unlink()
        with socket.socket(socket.AF_UNIX) as listener:
            special = self.source / "zz-socket"
            listener.bind(str(special))
            os.chown(special, 10001, 10001)
            self.assert_refused_without_mutation()

    def test_unexpected_owners_setid_files_and_writable_roots_refuse(self):
        path = self.source / "saved/interrupted.json"
        for uid, gid in ((0, 10001), (10001, 0), (10002, 10001), (12345, 12345)):
            with self.subTest(uid=uid, gid=gid):
                os.chown(path, uid, gid)
                self.assert_refused_without_mutation()
        os.chown(path, 10001, 10001)
        for mode in (0o4600, 0o2600, 0o666):
            with self.subTest(mode=mode):
                path.chmod(mode)
                self.assert_refused_without_mutation()
        path.chmod(0o600)
        os.chown(self.source, 0, 0)
        self.assert_refused_without_mutation()
        os.chown(self.source, 0, 10001)
        self.source.chmod(0o2777)
        self.assert_refused_without_mutation()

    def test_unprotected_ancestor_refuses(self):
        self.base.chmod(0o777)
        self.assert_refused_without_mutation()

    def test_held_saved_lock_refuses_all_mutations_and_releases_other_leases(self):
        lock = self.source / "state.json.lock"
        with lock.open("rb") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_refused_without_mutation()
        # A refusal must release any earlier leases from the nested lock file.
        self.assertGreater(migration.migrate(self.source, confirm_workers_stopped=True), 0)

    def test_all_saved_locks_stay_leased_before_and_through_every_chown(self):
        locks = [path for path in self.entries if path.name.endswith(".lock")]
        before = self.snapshot()
        real_chown = os.fchown
        calls = []

        def check_leases(fd, uid, gid):
            if not calls:
                self.assertEqual(self.snapshot(), before)
            for path in locks:
                with path.open("rb") as candidate:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            calls.append(fd)
            real_chown(fd, uid, gid)

        with patch.object(migration.os, "fchown", side_effect=check_leases):
            self.assertEqual(migration.migrate(self.source, confirm_workers_stopped=True), len(self.entries))
        self.assertEqual(len(calls), len(self.entries))
        for path in locks:
            with path.open("rb") as candidate:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_nested_mount_identity_mismatch_refuses_even_on_same_device(self):
        real_mount_id = migration._mount_id
        nested = (self.source / "saved").stat().st_ino

        def different_mount(fd):
            mount = real_mount_id(fd)
            return mount + 1 if os.fstat(fd).st_ino == nested else mount

        before = self.snapshot()
        with patch.object(migration, "_mount_id", side_effect=different_mount), \
                patch.object(migration.os, "fchown", side_effect=AssertionError("mutation")) as chown:
            with self.assertRaises(migration.MigrationRefused):
                migration.migrate(self.source, confirm_workers_stopped=True)
            chown.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_tree_is_revalidated_after_lock_acquisition_before_mutation(self):
        real_flock = fcntl.flock
        injected = self.source / "late-generated-file"

        def change_tree(fd, operation):
            real_flock(fd, operation)
            if not injected.exists():
                injected.write_bytes(b"generated concurrent change")
                os.chown(injected, 10001, 10001)

        with patch.object(migration.fcntl, "flock", side_effect=change_tree), \
                patch.object(migration.os, "fchown", side_effect=AssertionError("mutation")) as chown:
            with self.assertRaises(migration.MigrationRefused):
                migration.migrate(self.source, confirm_workers_stopped=True)
            chown.assert_not_called()
        for path in self.entries:
            self.assertEqual(path.stat().st_uid, 0 if path == self.source else 10001)

    def test_cli_no_flag_does_not_touch_populated_source(self):
        before = self.snapshot()
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, migration._FAILURE + "\n")
        self.assertEqual(self.snapshot(), before)

    def test_real_nonroot_cli_rejects_even_with_stopped_flag(self):
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-c", SCRIPT.read_text(), str(self.source),
                                 "--confirm-workers-stopped"], user=10002, group=10002,
                                extra_groups=(), cwd="/", text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, migration._FAILURE + "\n")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
