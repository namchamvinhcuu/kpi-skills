"""`init-config` of scripts/odoo_rpc.py: the credential writer the HUMAN runs in their own terminal.

Plan kpi-skill-improvements step 4 (B3, D3, AC5, R2, R6). It is the one place of the plugin that writes a secret to disk.

Every environment input of `init_config` is an argument (`env`, `stdin`, `stdout`, `ask`, `ask_secret`, `os_name`,
`sddl`, `user_sid`, `client_factory`), so each test gives the same answer inside Claude Code and in a plain terminal:
`IsolatedTestCase` clears `os.environ`, and the tests that need the real variable set it on purpose. An unmocked
`getpass` / `input` must never run: `InitConfigCase` patches both to raise (the default `ask=input` is bound when the
function is defined, so every test passes `ask=` itself). Two exceptions, both on injected streams: the `main` wiring
test reads the prompts through the real `input()` from a fake `sys.stdin`; the pty tests run a real child process.

Windows: the SDDL branch is driven by an injected SDDL text and user SID on any OS. A Windows path is never created on
POSIX (`ntpath` targets are only printed by a refused run). The real Windows 11 run is NOT claimed (parent Plan step 12).
"""
import builtins
import configparser
import contextlib
import errno
import getpass
import io
import ntpath
import os
import shutil
import socket
import stat
import sys
import time
import traceback
import unittest
import warnings
from unittest import mock

from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FAKE_UID, FakeOdoo
from helpers import (REPO, ExplodingOpener, IsolatedTestCase, RecordingOpener, ini_text, install_catalogs, missing_catalogs,
                     odoo_rpc, write_bytes)

import kpi_catalogs  # noqa: E402  (found through the sys.path entry that `helpers` adds)

POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX mode bits only; Windows uses the SDDL guard")
REAL_INPUT = input
URL = "https://my-instance.example.com"
USER = "S-1-5-21-1-2-3-1001"
GOOD_SDDL = "D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;%s)" % USER
OPEN_SDDL = "D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;WD)"
TEMP_PREFIX = ".odoo-kpi-config-"
TEMP_NAME = r"\.odoo-kpi-config-[a-z0-9_]{8}\.tmp\Z"                # what mkstemp makes: nothing of the typed values in it
OLD_TEXT = ini_text("https://old.example.com", key="OLD-KEY-NOT-REAL", db="old_db", login="old.login")


class Terminal(io.StringIO):
    def isatty(self):
        return True


class Pipe(io.StringIO):
    def isatty(self):
        return False


class NoIsatty:
    """A stream without an `isatty` attribute at all."""

    def write(self, text):
        return len(text)

    def flush(self):
        pass


class Session:
    """Scripted answers of the human; records every prompt. A prompt with no answer left fails the test."""

    def __init__(self, answers, typed_key=FAKE_KEY):
        self.answers, self.typed_key = list(answers), typed_key
        self.prompts, self.secret_prompts, self.when_asked = [], [], []
        self.watch = None

    def ask(self, prompt):
        self.prompts.append(prompt)
        self.when_asked.append(os.path.exists(self.watch) if self.watch else None)
        if not self.answers:
            raise AssertionError("unexpected prompt: %r" % prompt)
        return self.answers.pop(0)

    def ask_secret(self, prompt):
        self.secret_prompts.append(prompt)
        return self.typed_key


def flow(url=URL, db=FAKE_DB, login=FAKE_LOGIN, host=""):
    """The answers of a normal run on a fresh target: URL, database, login, then the host of the connection test."""
    return [url, db, login, host]


class Result:
    def __init__(self):
        self.code = self.exc = None
        self.out = self.real = self.trace = ""


class Spy:
    """Records, in order, every `open()` / `os.open()` path, every `os.replace` (source mode and the target's state at
    that moment) and every `check_private_file` call; the `fsync` calls with the mode of the file being synced."""

    def __init__(self, target):
        self.target = os.path.abspath(target)
        self.events = []

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        real_open, real_os_open, real_replace = builtins.open, os.open, os.replace
        real_check, real_fsync = odoo_rpc.check_private_file, os.fsync
        events, abspath = self.events, os.path.abspath

        def spy_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes)):
                events.append(("open", abspath(os.fsdecode(file))))
            return real_open(file, *args, **kwargs)

        def spy_os_open(path, flags, *args, **kwargs):
            events.append(("os.open", abspath(os.fsdecode(path)), flags))
            return real_os_open(path, flags, *args, **kwargs)

        def spy_replace(src, dst, *args, **kwargs):
            info = {"src_mode": stat.S_IMODE(os.stat(src).st_mode), "dst_exists": os.path.lexists(dst),
                    "folder": sorted(os.listdir(os.path.dirname(abspath(dst))))}
            if os.path.isfile(dst) and not os.path.islink(dst):
                info["dst_mode"] = stat.S_IMODE(os.stat(dst).st_mode)
                with real_open(dst, "rb") as handle:
                    info["dst_bytes"] = handle.read()
            events.append(("replace", abspath(src), abspath(dst), info))
            return real_replace(src, dst, *args, **kwargs)

        def spy_check(path, *args, **kwargs):
            try:
                size = os.path.getsize(path)                           # bytes on disk at the moment of the check
            except OSError:
                size = None
            events.append(("check", abspath(path), size))
            return real_check(path, *args, **kwargs)

        def spy_fsync(fd):
            info = os.fstat(fd)
            events.append(("fsync", stat.S_IMODE(info.st_mode), info.st_size))
            return real_fsync(fd)

        for name, replacement in (("builtins.open", spy_open), ("os.open", spy_os_open), ("os.replace", spy_replace),
                                  ("os.fsync", spy_fsync), ("odoo_rpc.check_private_file", spy_check)):
            if name.startswith("odoo_rpc."):
                self.stack.enter_context(mock.patch.object(odoo_rpc, name.split(".")[1], replacement))
            else:
                self.stack.enter_context(mock.patch(name, replacement))
        return self

    def __exit__(self, *exc_info):
        self.stack.close()
        return False

    def of(self, kind):
        return [event for event in self.events if event[0] == kind]

    def touching_target(self):
        return [event for event in self.events if event[0] in ("open", "os.open") and event[1] == self.target]


def stub_factory(uid=FAKE_UID, seen=None):
    """A `client_factory` whose client logs in without any network; `seen` collects (config, confirm_instance)."""

    class StubClient:
        def __init__(self, config, confirm_instance=None):
            if seen is not None:
                seen.append((config, confirm_instance))

        def login(self):
            return uid

    return StubClient


def real_client_without_network(config, confirm_instance=None):
    """The real `Client` (its guards run) on an opener that fails the test if the network is ever reached."""
    return odoo_rpc.Client(config, confirm_instance=confirm_instance, opener=ExplodingOpener())


def never_called(config, confirm_instance=None):
    raise AssertionError("the connection test ran although it had to be skipped")


class InitConfigCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        for name in ("builtins.input", "getpass.getpass"):
            patcher = mock.patch(name, side_effect=AssertionError("unmocked %s ran" % name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder = os.path.join(self.tmp, "cfg")
        self.target = os.path.join(self.folder, "config.ini")

    # ---- driving
    def call(self, session, **kw):
        args = dict(path=self.target, env={}, stdin=Terminal(), stdout=Terminal(), ask=session.ask,
                    ask_secret=session.ask_secret)
        args.update(kw)
        session.watch = args["path"]
        result, captured_out, captured_err = Result(), io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
            try:
                result.code = odoo_rpc.init_config(**args)
            except Exception as exc:  # the tests assert the type
                result.exc = exc
                result.trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        result.real = captured_out.getvalue() + captured_err.getvalue()
        result.out = args["stdout"].getvalue() if hasattr(args["stdout"], "getvalue") else ""
        return result

    def make_old(self, mode=0o600, data=None, folder_mode=None):
        os.makedirs(self.folder, exist_ok=True)
        if folder_mode is not None and os.name != "nt":
            os.chmod(self.folder, folder_mode)
        return write_bytes(self.folder, OLD_TEXT.encode("utf-8") if data is None else data, mode)

    def tree(self):
        """Everything under the temp root: type, mode and content (link target for a link); never opens a non-file."""
        found = {}
        for current, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                full = os.path.join(current, name)
                info = os.lstat(full)
                entry = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)]
                if stat.S_ISLNK(info.st_mode):
                    entry.append(os.readlink(full))
                elif stat.S_ISREG(info.st_mode):
                    try:
                        with open(full, "rb") as handle:
                            entry.append(handle.read())
                    except OSError:
                        entry.append(b"<unreadable>")
                found[os.path.relpath(full, self.tmp)] = tuple(entry)
        return found

    def read(self, path=None):
        with open(path or self.target, "rb") as handle:
            return handle.read()

    # ---- assertions
    def assertNoLeak(self, result, session, key=FAKE_KEY):
        shown = [result.out, result.real, result.trace, " ".join(session.prompts + session.secret_prompts)]
        if result.exc is not None:
            shown += [str(result.exc), repr(result.exc)]
        for text in shown:
            self.assertNotIn(key, text)
        for name in self.tree():
            self.assertNotIn(key, name, "the key is part of a file name")

    def assertRefusedUntouched(self, result, session, exc_type, before):
        self.assertIsInstance(result.exc, exc_type, (result.exc, result.trace))
        self.assertEqual(self.tree(), before, "something was created or changed")
        self.assertNoLeak(result, session)


# ================================================================== the human-only guards

class GuardTests(InitConfigCase):
    def test_claudecode_in_the_environment_refuses_for_any_value_and_nothing_is_written(self):
        for value in ("1", "", "0", "false", "anything"):
            with self.subTest(value=value):
                session, before = Session([]), self.tree()
                result = self.call(session, env={"CLAUDECODE": value})
                self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)
                self.assertIn("Claude Code", str(result.exc))
                self.assertEqual((session.prompts, session.secret_prompts, result.out, result.real), ([], [], "", ""))

    def test_the_guard_reads_the_injected_env_not_the_real_os_environ(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            session = Session(flow())
            result = self.call(session, env={})                        # the real variable is set, the injected is not
        self.assertIsNone(result.exc, result.trace)
        self.assertEqual(result.code, 0)
        self.assertTrue(os.path.isfile(self.target))
        self.assertNotIn("CLAUDECODE", os.environ)                      # the patch is gone: the next run is plain
        session, before = Session([]), self.tree()
        result = self.call(session, env={"CLAUDECODE": "1"})            # the real variable is absent, the injected is set
        self.assertIsInstance(result.exc, odoo_rpc.GuardError)

    def test_without_an_injected_env_the_real_os_environ_is_used(self):
        for present, expected in ((True, odoo_rpc.GuardError), (False, None)):
            with self.subTest(claudecode_in_os_environ=present):
                values = {"CLAUDECODE": "1"} if present else {}
                with mock.patch.dict(os.environ, values):
                    session = Session(flow())
                    result = self.call(session, env=None, path=os.path.join(self.tmp, "real-env-%s.ini" % present))
                if expected:
                    self.assertIsInstance(result.exc, expected)
                else:
                    self.assertIsNone(result.exc, result.trace)

    def test_a_missing_or_not_a_terminal_stdin_or_stdout_refuses_before_any_prompt(self):
        cases = {"stdin is not a tty": (Pipe(), Terminal()), "stdout is not a tty": (Terminal(), Pipe()),
                 "neither is a tty": (Pipe(), Pipe()), "stdin has no isatty": (NoIsatty(), Terminal()),
                 "stdout has no isatty": (Terminal(), NoIsatty()), "neither has isatty": (NoIsatty(), NoIsatty())}
        for label, (stdin, stdout) in cases.items():
            with self.subTest(case=label):
                session, before = Session([]), self.tree()
                result = self.call(session, stdin=stdin, stdout=stdout)
                self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)
                self.assertIn("terminal", str(result.exc))
                self.assertEqual((session.prompts, session.secret_prompts, result.out), ([], [], ""))

    def test_the_guard_runs_before_the_target_path_is_printed_or_looked_at(self):
        # a symlink target would be refused later with another message: the tty guard answers first
        os.makedirs(self.folder)
        os.symlink(os.path.join(self.tmp, "nowhere"), self.target)
        session = Session([])
        result = self.call(session, stdin=Pipe())
        self.assertIn("needs a terminal", str(result.exc))
        self.assertEqual(result.out, "")

    def test_none_stdin_and_stdout_fall_back_to_the_real_streams(self):
        for stdin, stdout, refused in ((Pipe(), Terminal(), True), (Terminal(), Pipe(), True),
                                       (Terminal(), Terminal(), False)):
            with self.subTest(stdin=type(stdin).__name__, stdout=type(stdout).__name__):
                path = os.path.join(self.tmp, "fallback-%s-%s.ini" % (type(stdin).__name__, type(stdout).__name__))
                session = Session(flow())
                with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
                    try:
                        code, exc = odoo_rpc.init_config(path=path, env={}, stdin=None, stdout=None, ask=session.ask,
                                                         ask_secret=session.ask_secret), None
                    except odoo_rpc.GuardError as caught:
                        code, exc = None, caught
                if refused:
                    self.assertIsInstance(exc, odoo_rpc.GuardError)
                    self.assertFalse(os.path.exists(path))
                else:
                    self.assertEqual((code, exc), (0, None))
                    self.assertIn("Config written", stdout.getvalue())     # printed to the (patched) sys.stdout

    def test_the_isolation_guards_are_active_an_unmocked_input_or_getpass_would_fail_the_test(self):
        with self.assertRaises(AssertionError):
            input("never")
        with self.assertRaises(AssertionError):
            getpass.getpass("never")

    def test_ctrl_c_or_end_of_input_at_any_prompt_propagates_and_writes_nothing_before_the_file_step(self):
        for exc in (KeyboardInterrupt, EOFError):
            for stop_at in (0, 1, 2, "key"):
                with self.subTest(exc=exc.__name__, prompt=stop_at):
                    shutil.rmtree(self.folder, ignore_errors=True)
                    before, session = self.tree(), Session(flow())
                    seen = []

                    def ask(prompt, _exc=exc, _stop=stop_at):
                        seen.append(prompt)
                        if _stop != "key" and len(seen) - 1 == _stop:
                            raise _exc()
                        return session.ask(prompt)

                    def ask_secret(prompt, _exc=exc, _stop=stop_at):
                        if _stop == "key":
                            raise _exc()
                        return session.ask_secret(prompt)

                    with self.assertRaises(exc):
                        odoo_rpc.init_config(path=self.target, env={}, stdin=Terminal(), stdout=Terminal(), ask=ask,
                                             ask_secret=ask_secret)
                    self.assertEqual(self.tree(), before)

    def test_no_prompt_text_leaves_through_a_real_stream_on_the_injected_path(self):
        session = Session(flow())
        result = self.call(session)
        self.assertEqual(result.real, "")                               # everything went to the injected stdout


# ================================================================== an existing target

class ExistingFileTests(InitConfigCase):
    def test_only_the_exact_word_yes_replaces_and_a_refusal_leaves_the_old_file_byte_identical(self):
        for answer in ("no", "", "YES", "Yes", "y", "yes please", "yess", "ye", " ", "no\n", "yes.", "Y", "true", "1",
                       "yes yes", "ok"):
            with self.subTest(answer=answer):
                self.make_old(mode=0o644)
                before, session = self.tree(), Session([answer])
                result = self.call(session)
                self.assertEqual((result.code, result.exc), (1, None))
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.read(), OLD_TEXT.encode("utf-8"))
                self.assertEqual(len(session.prompts), 1)
                self.assertIn("Type yes", session.prompts[0])
                self.assertEqual(session.secret_prompts, [])
                self.assertIn("Cancelled: nothing was written.", result.out)
                self.assertNotIn("Config written", result.out)
                self.assertNoLeak(result, session)

    def test_yes_with_surrounding_whitespace_is_accepted(self):
        for answer in ("yes", " yes", "yes ", "\tyes\n", "  yes  ", "yes\r\n"):
            with self.subTest(answer=repr(answer)):
                self.make_old()
                session = Session([answer] + flow(db="new_db"))
                result = self.call(session)
                self.assertEqual((result.code, result.exc), (0, None), result.trace)
                self.assertIn(b"db = new_db", self.read())
                self.assertNotIn(b"OLD-KEY", self.read())

    def test_the_resolved_target_path_is_printed_first_then_the_confirmation_is_asked(self):
        self.make_old()
        session = Session(["no"])
        result = self.call(session)
        self.assertEqual(result.out.splitlines()[0], "The config file will be: %s" % self.target)

    def test_no_confirmation_is_asked_for_a_fresh_target(self):
        session = Session(flow())
        self.call(session)
        self.assertEqual(len(session.prompts), 4)
        self.assertFalse(any("Type yes" in prompt for prompt in session.prompts))

    def test_the_old_file_is_never_opened_or_read_and_no_default_comes_from_it(self):
        self.make_old(mode=0o644)
        session = Session(["yes"] + flow(url="https://new.example.com", db="new_db", login="new.login"))
        with Spy(self.target) as spy:
            result = self.call(session)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(spy.touching_target(), [])
        for prompt in session.prompts + session.secret_prompts:
            for old in ("old.example.com", "old_db", "old.login", "OLD-KEY"):
                self.assertNotIn(old, prompt)
        self.assertNotIn(b"old", self.read().lower().replace(b"login", b""))

    def test_an_empty_answer_never_falls_back_to_the_value_of_the_old_file(self):
        for position, label in ((1, "url"), (2, "db"), (3, "login")):
            with self.subTest(empty=label):
                self.make_old()
                answers = ["yes", URL, FAKE_DB, FAKE_LOGIN]
                answers[position] = ""
                before, session = self.tree(), Session(answers)
                result = self.call(session)
                self.assertRefusedUntouched(result, session, odoo_rpc.ConfigError, before)
        self.make_old()
        before, session = self.tree(), Session(["yes"] + flow(), typed_key="")
        result = self.call(session)
        self.assertRefusedUntouched(result, session, odoo_rpc.ConfigError, before)
        self.assertEqual(self.read(), OLD_TEXT.encode("utf-8"))

    def test_an_unreadable_or_garbage_old_file_does_not_matter(self):
        for label, data, mode in (("garbage bytes", b"\xff\xfe\x00\x01 not ini at all \x00", 0o600),
                                  ("empty", b"", 0o600), ("mode 000", OLD_TEXT.encode("utf-8"), 0o000),
                                  ("world writable garbage", b"[[[[", 0o666)):
            with self.subTest(old=label):
                if os.path.exists(self.target):
                    os.unlink(self.target)
                self.make_old(mode=mode, data=data)
                session = Session(["yes"] + flow())
                result = self.call(session)
                self.assertEqual((result.code, result.exc), (0, None), result.trace)
                self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).api_key, FAKE_KEY)

    @POSIX_ONLY
    def test_replacing_a_0644_file_never_opens_the_final_path_and_the_key_is_never_in_a_non_0600_file(self):
        self.make_old(mode=0o644)
        session = Session(["yes"] + flow())
        with Spy(self.target) as spy:
            result = self.call(session)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(spy.touching_target(), [], "the final path was opened (for writing or not)")
        replaces = spy.of("replace")
        self.assertEqual(len(replaces), 1)
        _, src, dst, info = replaces[0]
        self.assertEqual(dst, os.path.abspath(self.target))
        self.assertEqual(os.path.dirname(src), os.path.abspath(self.folder), "the temp file is not in the config folder")
        self.assertRegex(os.path.basename(src), TEMP_NAME)              # prefix + 8 random characters + .tmp: no key part
        self.assertEqual(info["src_mode"], 0o600, "the key sat in a file with another mode at replace time")
        self.assertEqual(info["dst_mode"], 0o644)                       # the old file was untouched until the replace
        self.assertEqual(info["dst_bytes"], OLD_TEXT.encode("utf-8"))
        self.assertEqual(sorted(info["folder"]), sorted(["config.ini", os.path.basename(src)]))
        # one fsync, on a 0600 file that already holds ALL the bytes (the buffer was flushed before the sync)
        self.assertEqual([(mode, size) for _, mode, size in spy.of("fsync")], [(0o600, len(self.read()))])
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.folder)), ["config.ini"])
        self.assertNoLeak(result, session)

    @POSIX_ONLY
    def test_the_temp_file_is_0600_whatever_the_umask(self):
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(umask)):
                old = os.umask(umask)
                self.addCleanup(os.umask, old)
                if os.path.exists(self.target):
                    os.unlink(self.target)
                with Spy(self.target) as spy:
                    result = self.call(Session(flow()))
                os.umask(old)
                self.assertIsNone(result.exc, result.trace)
                self.assertEqual(spy.of("replace")[0][3]["src_mode"], 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)


# ================================================================== what the target is

class TargetKindTests(InitConfigCase):
    def refuse(self, expect_prompts=0):
        before, session = self.tree(), Session(["yes"] + flow())
        result = self.call(session)
        self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)
        self.assertIn("link or not a regular file", str(result.exc))
        self.assertEqual(session.prompts, [], "a prompt was asked before the target was refused")
        self.assertEqual(session.secret_prompts, [])
        self.assertIn("The config file will be", result.out)           # the path is shown, then the refusal
        return result

    @POSIX_ONLY
    def test_a_symlink_to_a_file_is_refused_and_the_linked_file_is_untouched(self):
        os.makedirs(self.folder)
        real = write_bytes(self.tmp, OLD_TEXT.encode("utf-8"), 0o600, name="real.ini")
        os.symlink(real, self.target)
        self.refuse()
        self.assertTrue(os.path.islink(self.target))
        self.assertEqual(self.read(real), OLD_TEXT.encode("utf-8"))

    @POSIX_ONLY
    def test_a_symlink_to_a_directory_is_refused(self):
        os.makedirs(self.folder)
        os.makedirs(os.path.join(self.tmp, "somewhere"))
        os.symlink(os.path.join(self.tmp, "somewhere"), self.target)
        self.refuse()
        self.assertEqual(os.listdir(os.path.join(self.tmp, "somewhere")), [])

    @POSIX_ONLY
    def test_a_dangling_symlink_is_refused_and_its_destination_is_not_created(self):
        os.makedirs(self.folder)
        destination = os.path.join(self.tmp, "does-not-exist.ini")
        os.symlink(destination, self.target)
        self.refuse()
        self.assertFalse(os.path.exists(destination))

    def test_a_directory_target_is_refused(self):
        os.makedirs(self.target)
        self.refuse()
        self.assertTrue(os.path.isdir(self.target))
        self.assertEqual(os.listdir(self.target), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "this OS cannot make a FIFO (os.mkfifo is missing)")
    def test_a_fifo_target_is_refused_and_never_opened(self):
        os.makedirs(self.folder)
        os.mkfifo(self.target)
        with Spy(self.target) as spy:
            self.refuse()
        self.assertEqual(spy.touching_target(), [])
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.target).st_mode))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "this OS has no unix domain sockets")
    def test_a_unix_socket_target_is_refused(self):
        os.makedirs(self.folder)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        try:
            sock.bind(self.target)
        except OSError as exc:
            self.skipTest("cannot bind a unix socket here: %s" % exc)
        self.refuse()
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.target).st_mode))

    @unittest.skipUnless(os.path.exists(os.devnull) and os.name != "nt", "no character device to point at")
    def test_a_device_file_target_is_refused(self):
        session, before = Session(["yes"] + flow()), self.tree()
        result = self.call(session, path=os.devnull)
        self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)
        self.assertEqual(session.prompts, [])

    @POSIX_ONLY
    def test_a_symlinked_target_is_refused_before_the_confirmation_even_for_a_yes(self):
        os.makedirs(self.folder)
        real = write_bytes(self.tmp, OLD_TEXT.encode("utf-8"), 0o600, name="real.ini")
        os.symlink(real, self.target)
        session = Session(["yes", "yes"])
        result = self.call(session)
        self.assertIsInstance(result.exc, odoo_rpc.GuardError)
        self.assertEqual(session.prompts, [])


# ================================================================== the typed fields

class FieldTests(InitConfigCase):
    def assertRefusedField(self, answers, typed_key=FAKE_KEY, prompts=None, secret=None, message=None):
        before, session = self.tree(), Session(answers, typed_key=typed_key)
        result = self.call(session)
        self.assertRefusedUntouched(result, session, odoo_rpc.ConfigError, before)
        if prompts is not None:
            self.assertEqual(len(session.prompts), prompts, session.prompts)
        if secret is not None:
            self.assertEqual(len(session.secret_prompts), secret)
        if message:
            self.assertIn(message, str(result.exc))
        self.assertFalse(os.path.exists(self.folder), "the config folder was created before the fields were valid")
        return result

    def test_an_invalid_url_is_refused_before_any_further_prompt_and_before_any_write(self):
        for bad in ("my-instance.example.com", "//my-instance.example.com", "ftp://my-instance.example.com",
                    "https://", "https://user:pw@my-instance.example.com", "http://my instance.example.com",
                    "https://my-instance.example.com/path?x=1", "https://my-instance.example.com/#frag",
                    "https://my\\instance.example.com", "https://h\u00e9.example.com", "https://-bad-.example.com",
                    "javascript:alert(1)", "file:///etc/passwd"):
            with self.subTest(url=bad):
                self.assertRefusedField([bad, FAKE_DB, FAKE_LOGIN, ""], prompts=1, secret=0)

    def test_a_url_with_a_control_character_is_refused_as_a_field(self):
        for bad in ("https://my-instance.example.com\x00", "https://my\x07instance.example.com",
                    "https://my-instance.example.com/a\nb", "https://my-instance.example.com\x7f"):
            with self.subTest(url=repr(bad)):
                self.assertRefusedField([bad, FAKE_DB, FAKE_LOGIN, ""], prompts=1, secret=0)

    def test_the_url_trailing_slashes_are_removed_and_the_value_is_stripped(self):
        self.call(Session(["  https://my-instance.example.com///  ", FAKE_DB, FAKE_LOGIN, ""]))
        self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).url, "https://my-instance.example.com")

    def test_a_non_local_http_url_is_not_refused_at_write_time_the_client_guard_refuses_it_later(self):
        # What actually happens: the URL passes `_validated_url` (scheme + host only), the file IS written, and the
        # refusal comes from `Client` (check_instance: "only https is allowed") when the human types a host to test.
        url = "http://remote.example.com"
        result = self.call(Session(flow(url=url)))                      # Enter: no connection test
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).url, url)
        os.unlink(self.target)
        result = self.call(Session(flow(url=url, host="remote.example.com")), client_factory=real_client_without_network)
        self.assertIsInstance(result.exc, odoo_rpc.GuardError)
        self.assertIn("only https is allowed", str(result.exc))
        self.assertTrue(os.path.isfile(self.target))                    # the file stays: it was valid and private

    def test_an_empty_blank_control_character_or_over_long_field_is_refused_with_no_write(self):
        controls = ("\x00", "\n", "\r", "\t", "\x07", "\x1b", "\x7f", "\x85", "\x9f")
        for label, position in (("db", 2), ("login", 3)):
            for value in ("", "   ", "\t \n"):
                with self.subTest(field=label, value=repr(value)):
                    answers = [URL, FAKE_DB, FAKE_LOGIN, ""]
                    answers[position - 1] = value
                    self.assertRefusedField(answers)
            for control in controls:
                with self.subTest(field=label, control=repr(control)):
                    answers = [URL, FAKE_DB, FAKE_LOGIN, ""]
                    answers[position - 1] = "ab" + control + "cd"
                    self.assertRefusedField(answers)
            with self.subTest(field=label, too_long=True):
                answers = [URL, FAKE_DB, FAKE_LOGIN, ""]
                answers[position - 1] = "x" * (odoo_rpc.INIT_FIELD_LIMIT + 1)
                self.assertRefusedField(answers, message="at most %d characters" % odoo_rpc.INIT_FIELD_LIMIT)

    def test_an_empty_blank_control_character_or_over_long_key_is_refused_and_is_not_echoed_in_the_error(self):
        for label, typed in (("empty", ""), ("blank", "   "), ("none", None), ("newline inside", FAKE_KEY + "\n" + "x"),
                             ("nul inside", FAKE_KEY + "\x00"), ("bell", FAKE_KEY + "\x07"), ("c1 inside", FAKE_KEY + "\x85" + "x"),
                             ("too long", FAKE_KEY * 40), ("one over", "k" * (odoo_rpc.INIT_KEY_LIMIT + 1))):
            with self.subTest(key=label):
                result = self.assertRefusedField(flow(), typed_key=typed, prompts=3, secret=1)
                self.assertNotIn(FAKE_KEY, str(result.exc))
                self.assertNotIn(FAKE_KEY[:12], str(result.exc))

    def test_the_length_limits_are_inclusive(self):
        def host_of(length):
            labels = ["a" * 63, "a" * 63, "a" * 63]
            return ".".join(labels + ["b" * (length - 3 * 64)])
        # the documented limits, written as numbers on purpose: a test that read the constants would follow a mutant
        self.assertEqual((odoo_rpc.INIT_FIELD_LIMIT, odoo_rpc.INIT_KEY_LIMIT), (256, 1024))
        url = "https://" + host_of(256 - 8)
        self.assertEqual(len(url), 256)
        db, login, key = "d" * 256, "l" * 256, "k" * 1024
        result = self.call(Session(flow(url=url, db=db, login=login), typed_key=key))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        config = odoo_rpc.load_config(path=self.target, env={})
        self.assertEqual((config.url, config.db, config.login, config.api_key), (url, db, login, key))
        shutil.rmtree(self.folder)
        self.assertRefusedField(flow(url=url + "a"), message="at most 256 characters")
        for position in (2, 3):                                         # db, login: one over
            answers = flow()
            answers[position - 1] = "x" * 257
            self.assertRefusedField(answers, message="at most 256 characters")
        self.assertRefusedField(flow(), typed_key="k" * 1025, message="at most 1024 characters")

    def test_no_value_is_taken_from_the_environment_variables_either(self):
        env = dict(zip(odoo_rpc.ENV_NAMES.values(), ("https://env.example.com", "env_db", "env.login", "ENV-VALUE")))
        for position in (1, 2, 3):
            with self.subTest(empty=position):
                answers = flow()
                answers[position - 1] = ""
                with mock.patch.dict(os.environ, env):
                    self.assertRefusedField(answers)
        with mock.patch.dict(os.environ, env):
            self.assertRefusedField(flow(), typed_key="")

    def test_values_are_stripped_and_special_characters_survive_a_round_trip(self):
        values = {"url": "https://my-instance.example.com/odoo%20x", "db": "db%name=1;x#y", "login": "Nguy\u1ec5n.V\u0103n [x] ;#",
                  "key": "ab%cd%%ef=1;#[x]\u00e9 :z\u2028end"}
        answers = ["  %s  " % values["url"], "\t%s\n" % values["db"], " %s " % values["login"], ""]
        result = self.call(Session(answers, typed_key="  %s  " % values["key"]))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        config = odoo_rpc.load_config(path=self.target, env={})
        self.assertEqual((config.url, config.db, config.login, config.api_key),
                         (values["url"], values["db"], values["login"], values["key"]))
        self.assertEqual(self.read().decode("utf-8").count("\n[odoo]"), 0)        # one section only, at the start

    def test_the_file_is_utf8_without_a_bom(self):
        self.call(Session(flow(login="Nguy\u1ec5n.V\u0103n")))
        raw = self.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Nguy\u1ec5n.V\u0103n".encode("utf-8"), raw)


# ================================================================== what is written

class WrittenFileTests(InitConfigCase):
    def test_the_file_is_exactly_the_odoo_section_with_the_four_keys_and_load_config_accepts_it(self):
        session = Session(flow(db="my_db", login="my.login"), typed_key="typed-test-value-1")
        result = self.call(session)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        lines = self.read().decode("utf-8").strip().splitlines()
        self.assertEqual(lines, ["[odoo]", "url = %s" % URL, "db = my_db", "login = my.login",
                                 "api_key = typed-test-value-1"])
        config = odoo_rpc.load_config(path=self.target, env={})
        self.assertEqual((config.url, config.db, config.login, config.api_key, config.source),
                         (URL, "my_db", "my.login", "typed-test-value-1", "file"))
        self.assertEqual(sorted(os.listdir(self.folder)), ["config.ini"])

    @POSIX_ONLY
    def test_the_file_is_0600_and_a_created_folder_is_0700_whatever_the_umask(self):
        for umask in (0o000, 0o002, 0o022):
            with self.subTest(umask=oct(umask)):
                folder = os.path.join(self.tmp, "u%o" % umask, "odoo-kpi")
                old = os.umask(umask)
                try:
                    result = self.call(Session(flow()), path=os.path.join(folder, "config.ini"))
                finally:
                    os.umask(old)
                self.assertIsNone(result.exc, result.trace)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, "config.ini")).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)

    @POSIX_ONLY
    def test_an_existing_folder_keeps_its_mode_and_its_other_files(self):
        for mode in (0o755, 0o750, 0o700):
            with self.subTest(mode=oct(mode)):
                folder = os.path.join(self.tmp, "keep%o" % mode)
                os.makedirs(folder)
                os.chmod(folder, mode)
                write_bytes(folder, b"neighbour", 0o644, name="other.txt")
                result = self.call(Session(flow()), path=os.path.join(folder, "config.ini"))
                self.assertIsNone(result.exc, result.trace)
                self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), mode)
                self.assertEqual(sorted(os.listdir(folder)), ["config.ini", "other.txt"])
                self.assertEqual(self.read(os.path.join(folder, "other.txt")), b"neighbour")

    def test_a_fresh_target_is_written_through_a_temp_file_of_the_same_folder_never_opened_at_the_final_path(self):
        with Spy(self.target) as spy:
            result = self.call(Session(flow()))
        self.assertIsNone(result.exc, result.trace)
        self.assertEqual(spy.touching_target(), [])
        (_, src, dst, info), = spy.of("replace")
        self.assertEqual((os.path.dirname(src), dst), (os.path.abspath(self.folder), os.path.abspath(self.target)))
        self.assertRegex(os.path.basename(src), TEMP_NAME)
        self.assertFalse(info["dst_exists"])
        self.assertNotIn(FAKE_KEY, src)

    def test_the_privacy_check_runs_on_the_temp_file_before_the_replace(self):
        self.make_old(mode=0o600)
        with Spy(self.target) as spy:
            result = self.call(Session(["yes"] + flow()))
        self.assertIsNone(result.exc, result.trace)
        # Plan step 4 review F1: the check runs on the still EMPTY temp, before any byte is written, exactly once
        kinds = [event[0] for event in spy.events if event[0] in ("fsync", "check", "replace")]
        self.assertEqual(kinds, ["check", "fsync", "replace"])
        self.assertEqual(len(spy.of("check")), 1)
        check, replace = spy.of("check")[0], spy.of("replace")[0]
        self.assertEqual(check[1], replace[1], "the check did not look at the file that is moved")
        self.assertNotEqual(check[1], os.path.abspath(self.target))
        self.assertEqual(check[2], 0, "the temp file already held bytes when it was checked")

    def test_a_check_that_refuses_the_temp_file_stops_before_the_replace_and_removes_the_temp(self):
        self.make_old(mode=0o644)
        before, session = self.tree(), Session(["yes"] + flow())
        seen = []

        def refuse(path, *args, **kwargs):
            seen.append(sorted(name for name in os.listdir(self.folder) if name.startswith(TEMP_PREFIX)))
            seen.append(os.path.getsize(path))
            raise odoo_rpc.GuardError("not private enough")

        with mock.patch.object(odoo_rpc, "check_private_file", refuse), mock.patch("os.replace") as replace, \
                mock.patch("os.fsync") as fsync, mock.patch("os.fdopen", wraps=os.fdopen) as fdopen:
            result = self.call(session)
        self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(seen[0]), 1, "the temp file did not exist while it was checked")
        self.assertEqual(seen[1], 0, "bytes were on disk when the check refused")
        replace.assert_not_called()
        fsync.assert_not_called()
        fdopen.assert_not_called()                                      # nothing was even opened for writing

    def test_a_wide_acl_on_the_empty_temp_is_refused_before_any_byte_is_written(self):
        # Plan step 4 review F1, Windows shape: the folder's inherited ACL (the SDDL stub) is too wide for the temp
        sizes, writes = [], []
        real_fdopen = os.fdopen

        def sddl(path):
            sizes.append(os.path.getsize(path))
            return OPEN_SDDL

        def spy_fdopen(*args, **kwargs):
            writes.append(args)
            return real_fdopen(*args, **kwargs)

        for old_present in (False, True):
            with self.subTest(old_file=old_present):
                shutil.rmtree(self.folder, ignore_errors=True)
                if old_present:
                    self.make_old(mode=0o644)
                else:
                    os.makedirs(self.folder)
                before, session = self.tree(), Session((["yes"] if old_present else []) + flow())
                del sizes[:], writes[:]
                with mock.patch("os.fsync") as fsync, mock.patch("os.fdopen", spy_fdopen), \
                        mock.patch("os.replace") as replace:
                    result = self.call(session, os_name="nt", sddl=sddl, user_sid=USER)
                self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)
                self.assertIn("WD", str(result.exc))
                self.assertEqual(sizes, [0], "the key was on disk when the ACL was checked")
                self.assertEqual(writes, [])
                fsync.assert_not_called()
                replace.assert_not_called()
                self.assertNotIn("Config written", result.out)

    def test_a_refused_check_closes_the_temp_descriptor_so_that_even_windows_can_delete_the_temp(self):
        # Plan step 4 review F1 follow-up: the check now runs BEFORE `os.fdopen` owns the descriptor. If the refusal
        # leaves it open, POSIX leaks a descriptor and WINDOWS cannot unlink an open file (sharing violation): the
        # `except OSError: pass` of the cleanup then hides it and an empty `.odoo-kpi-config-*.tmp` stays behind.
        os.makedirs(self.folder)
        opened = []
        real_mkstemp, real_unlink = odoo_rpc.tempfile.mkstemp, os.unlink

        def spy_mkstemp(*args, **kwargs):
            handle, temp = real_mkstemp(*args, **kwargs)
            opened.append(handle)
            return handle, temp

        def windows_like_unlink(path, *args, **kwargs):
            for handle in opened:
                try:
                    os.fstat(handle)
                except OSError:
                    continue
                raise PermissionError(errno.EACCES, "the process cannot access the file: it is open")
            return real_unlink(path, *args, **kwargs)

        def refuse(path, *args, **kwargs):
            raise odoo_rpc.GuardError("not private enough")

        before, session = self.tree(), Session(flow())
        with mock.patch.object(odoo_rpc.tempfile, "mkstemp", spy_mkstemp), \
                mock.patch.object(odoo_rpc, "check_private_file", refuse), mock.patch("os.unlink", windows_like_unlink):
            result = self.call(session)
        try:
            os.fstat(opened[0])
            leaked = True
            os.close(opened[0])                                        # do not leak it from the test run either
        except OSError:
            leaked = False
        self.assertFalse(leaked, "the temp file descriptor was never closed after the refused check")
        self.assertEqual(self.tree(), before, "an empty temp file is left behind where an open file cannot be deleted")
        self.assertIsInstance(result.exc, odoo_rpc.GuardError)

    def test_every_failure_step_removes_the_temp_file_and_leaves_the_target_untouched(self):
        def at_fsync(fd):
            raise OSError(errno.ENOSPC, "disk full")

        def at_interrupt(fd):
            raise KeyboardInterrupt

        def at_check(path, *args, **kwargs):
            raise odoo_rpc.GuardError("not private enough")

        def at_replace(src, dst, *args, **kwargs):
            raise OSError(errno.EXDEV, "cannot move " + FAKE_KEY)

        steps = (("fsync fails", "os.fsync", at_fsync, OSError), ("interrupt while writing", "os.fsync", at_interrupt,
                                                                KeyboardInterrupt),
                 ("privacy check fails", "odoo_rpc.check_private_file", at_check, odoo_rpc.GuardError),
                 ("replace fails", "os.replace", at_replace, OSError))
        for old_present in (False, True):
            for label, name, replacement, expected in steps:
                with self.subTest(step=label, old_file=old_present):
                    shutil.rmtree(self.folder, ignore_errors=True)
                    if old_present:
                        self.make_old(mode=0o644)
                    else:
                        os.makedirs(self.folder)
                    before, session = self.tree(), Session((["yes"] if old_present else []) + flow())
                    existed = []

                    def probe(*args, _inner=replacement, **kwargs):
                        existed.append([n for n in os.listdir(self.folder) if n.startswith(TEMP_PREFIX)])
                        return _inner(*args, **kwargs)

                    patcher = (mock.patch.object(odoo_rpc, "check_private_file", probe) if name.startswith("odoo_rpc.")
                               else mock.patch(name, probe))
                    result = Result()
                    with patcher:
                        try:
                            self.call_raw(session, result)
                        except BaseException as exc:                    # KeyboardInterrupt included
                            result.exc = exc
                    self.assertIsInstance(result.exc, expected, result.exc)
                    self.assertEqual(len(existed), 1)
                    self.assertEqual(len(existed[0]), 1, "the temp file did not exist at the failing step")
                    self.assertEqual(self.tree(), before, "temp file left behind or the target changed")
                    self.assertNotIn(FAKE_KEY, "".join(session.prompts))
                    if old_present:
                        self.assertEqual(self.read(), OLD_TEXT.encode("utf-8"))
                        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode) if os.name != "nt" else 0o644, 0o644)

    def call_raw(self, session, result):
        """`call` without catching: the caller decides (KeyboardInterrupt must reach it)."""
        session.watch = self.target
        odoo_rpc.init_config(path=self.target, env={}, stdin=Terminal(), stdout=Terminal(), ask=session.ask,
                             ask_secret=session.ask_secret)

    def test_a_failing_unlink_of_the_temp_does_not_hide_the_original_failure(self):
        session = Session(flow())
        os.makedirs(self.folder)
        with mock.patch("os.fsync", side_effect=RuntimeError("original failure")), \
                mock.patch("os.unlink", side_effect=OSError("cannot unlink")):
            result = self.call(session)
        self.assertIsInstance(result.exc, RuntimeError)
        self.assertEqual(str(result.exc), "original failure")

    def test_a_failure_while_writing_never_lets_a_half_written_final_file_exist(self):
        os.makedirs(self.folder)
        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "I/O error")):
            result = self.call(Session(flow()))
        self.assertIsInstance(result.exc, OSError)
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_config_folder_that_is_a_file_fails_with_the_type_only(self):
        write_bytes(self.tmp, b"I am a file", 0o600, name="cfg")
        session = Session(flow())
        result = self.call(session)
        self.assertIsInstance(result.exc, (FileExistsError, NotADirectoryError))
        self.assertNoLeak(result, session)

    def test_a_relative_target_is_written_next_to_the_current_directory(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        for relative in ("rel.ini", os.path.join("sub", "deeper", "rel2.ini")):
            with self.subTest(path=relative):
                result = self.call(Session(flow()), path=relative)
                self.assertIsNone(result.exc, result.trace)
                self.assertEqual(odoo_rpc.load_config(path=os.path.join(self.tmp, relative), env={}).api_key, FAKE_KEY)
                self.assertEqual(sorted(n for n in os.listdir(os.path.dirname(os.path.join(self.tmp, relative)))
                                        if n.startswith(".")), [])

    def test_the_default_location_is_used_when_no_path_is_given(self):
        env = {"XDG_CONFIG_HOME": os.path.join(self.tmp, "xdg")}
        result = self.call(Session(flow()), path=None, env=env)
        self.assertIsNone(result.exc, result.trace)
        expected = os.path.join(self.tmp, "xdg", "odoo-kpi", "config.ini")
        self.assertEqual(result.out.splitlines()[0], "The config file will be: %s" % expected)
        self.assertTrue(os.path.isfile(expected))
        self.assertEqual(odoo_rpc.load_config(path=expected, env={}).api_key, FAKE_KEY)

    def test_the_injected_env_override_names_the_target(self):
        target = os.path.join(self.tmp, "chosen", "mine.ini")
        result = self.call(Session(flow()), path=None, env={"ODOO_KPI_CONFIG": target, "XDG_CONFIG_HOME": self.tmp})
        self.assertIsNone(result.exc, result.trace)
        self.assertEqual(result.out.splitlines()[0], "The config file will be: %s" % target)
        self.assertTrue(os.path.isfile(target))

    def test_with_nothing_injected_the_home_of_the_isolated_environment_is_used_never_the_real_one(self):
        result = self.call(Session(flow()), path=None, env={})
        self.assertIsNone(result.exc, result.trace)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".config", "odoo-kpi", "config.ini")))


# ================================================================== the key never leaves

class KeyLeakTests(InitConfigCase):
    """The key must not appear in stdout, stderr, an exception text / repr / traceback, a prompt or a file name, on any
    path: success, every refusal, a write failure and a failing connection test."""

    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo()
        self.fake.start()
        self.addCleanup(self.fake.stop)

    def test_success_without_and_with_the_connection_test(self):
        for host in ("", "127.0.0.1"):
            with self.subTest(host=host):
                if os.path.exists(self.target):
                    os.unlink(self.target)
                session = Session(flow(url=self.fake.url, host=host))
                result = self.call(session)
                self.assertEqual((result.code, result.exc), (0, None), result.trace)
                self.assertNoLeak(result, session)

    def test_refusals_cancel_and_write_failures(self):
        os.makedirs(self.folder)
        link = os.path.join(self.tmp, "link.ini")
        scenarios = [
            ("claudecode", dict(env={"CLAUDECODE": "1"}), [], odoo_rpc.GuardError),
            ("no tty", dict(stdin=Pipe()), [], odoo_rpc.GuardError),
            ("bad url", {}, ["nope"], odoo_rpc.ConfigError),
            ("empty db", {}, [URL, ""], odoo_rpc.ConfigError),
            ("fsync fails", {}, flow(), OSError),
        ]
        for label, kw, answers, expected in scenarios:
            with self.subTest(case=label):
                session = Session(answers)
                if label == "fsync fails":
                    with mock.patch("os.fsync", side_effect=OSError("failed " + FAKE_KEY)):
                        result = self.call(session, **kw)
                else:
                    result = self.call(session, **kw)
                self.assertIsInstance(result.exc, expected)
                if label != "fsync fails":                              # that OSError text carries the key on purpose
                    self.assertNoLeak(result, session)
                for name in self.tree():
                    self.assertNotIn(FAKE_KEY, name)
        self.make_old()
        session = Session(["no"])
        self.assertNoLeak(self.call(session), session)
        if os.name != "nt":
            os.unlink(self.target)
            os.symlink(os.path.join(self.tmp, "x.ini"), link)
            session = Session(flow())
            result = self.call(session, path=link)
            self.assertNoLeak(result, session)

    def test_a_failing_connection_test_never_shows_the_key_even_when_the_server_echoes_it(self):
        for mode, expected_text in (("error_echo", "***"), ("error_echo_top", "***"), ("http_500", "HTTP 500"),
                                    ("login_false", "login failed"), ("non_json", "JSON"), ("cf_1010", "1010")):
            with self.subTest(mode=mode):
                if os.path.exists(self.target):
                    os.unlink(self.target)
                self.fake.mode = mode
                session = Session(flow(url=self.fake.url, host="127.0.0.1"))
                result = self.call(session)
                self.assertIsInstance(result.exc, odoo_rpc.RpcError, (mode, result.trace))
                self.assertIn(expected_text, str(result.exc))
                self.assertSafeRpcError(result.exc)
                self.assertNoLeak(result, session)
                self.assertTrue(os.path.isfile(self.target), "the file must stay: it is valid, only the test failed")
                self.assertIn("Config written", result.out)

    def test_the_error_text_of_an_invalid_field_never_holds_the_typed_value(self):
        for typed in ("ab\x07" + FAKE_KEY, FAKE_KEY + " " * 2 + "\n" + FAKE_KEY, FAKE_KEY * 100):
            with self.subTest(length=len(typed)):
                session = Session(flow(), typed_key=typed)
                result = self.call(session)
                self.assertIsInstance(result.exc, odoo_rpc.ConfigError)
                self.assertNoLeak(result, session)

    def test_an_unexpected_exception_that_carries_the_key_is_reduced_to_its_type_by_main(self):
        for exc in (OSError(errno.EXDEV, "cannot move " + FAKE_KEY), ValueError(FAKE_KEY), RuntimeError(repr(FAKE_KEY))):
            with self.subTest(exc=type(exc).__name__):
                out, err = Terminal(), io.StringIO()
                with mock.patch.object(odoo_rpc, "init_config", side_effect=exc):
                    code = odoo_rpc.main(["init-config"], out, err)
                self.assertEqual(code, 1)
                self.assertEqual(err.getvalue(), "error: unexpected %s\n" % type(exc).__name__)
                self.assertNotIn(FAKE_KEY, out.getvalue() + err.getvalue())


# ================================================================== the connection test

class ConnectionTests(InitConfigCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo()
        self.fake.start()
        self.addCleanup(self.fake.stop)

    def test_enter_skips_the_test_and_no_client_is_built(self):
        session = Session(flow(url=URL, host=""))
        result = self.call(session, client_factory=never_called)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertIn("Connection test skipped", result.out)
        self.assertNotIn("login: ok", result.out)
        self.assertTrue(os.path.isfile(self.target))

    def test_a_blank_host_also_skips(self):
        result = self.call(Session(flow(host="   \t ")), client_factory=never_called)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)

    def test_a_typed_host_logs_in_against_the_fake_server_and_prints_exactly_the_uid_line(self):
        session = Session(flow(url=self.fake.url, host="127.0.0.1"))
        result = self.call(session)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(result.out.splitlines(), ["The config file will be: %s" % self.target,
                                                   "Config written (private to your user).",
                                                   "login: ok (uid %d)" % FAKE_UID])
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.fake.requests[0]["body"]["params"]["method"], "login")
        self.assertEqual(self.fake.requests[0]["body"]["params"]["args"], [FAKE_DB, FAKE_LOGIN, FAKE_KEY])
        self.assertNoLeak(result, session)

    def test_the_host_is_a_separate_prompt_asked_after_the_file_is_written(self):
        session = Session(flow(url=self.fake.url, host="127.0.0.1"))
        self.call(session)
        self.assertEqual(len(session.prompts), 4)
        self.assertIn("FULL host name", session.prompts[3])
        self.assertEqual(session.when_asked, [False, False, False, True])       # the file exists only at the 4th prompt
        self.assertEqual(len(session.secret_prompts), 1)

    def test_ask_secret_is_called_only_for_the_key_and_ask_never_for_the_key(self):
        session = Session(flow(url=self.fake.url, host="127.0.0.1"))
        self.call(session)
        self.assertEqual(len(session.secret_prompts), 1)
        self.assertIn("API key", session.secret_prompts[0])
        self.assertFalse(any("key" in prompt.lower() for prompt in session.prompts), session.prompts)

    def test_prompts_are_not_written_to_the_injected_stdout(self):
        session = Session(flow(host=""))
        result = self.call(session)
        for prompt in session.prompts + session.secret_prompts:
            self.assertNotIn(prompt.strip(), result.out)

    def test_the_client_gets_the_config_of_the_file_and_the_typed_host_not_the_environment(self):
        seen = []
        other = dict(zip(odoo_rpc.ENV_NAMES.values(), ("https://other.example.com", "other_db", "other.login",
                                                       "OTHER-ENV-VALUE")))
        with mock.patch.dict(os.environ, other):
            result = self.call(Session(flow(host="  My-Instance.Example.Com  ")), client_factory=stub_factory(seen=seen))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        (config, confirm), = seen
        self.assertEqual((config.url, config.db, config.login, config.api_key, config.source),
                         (URL, FAKE_DB, FAKE_LOGIN, FAKE_KEY, "file"))
        self.assertEqual(confirm, "My-Instance.Example.Com")            # stripped, case is the guard's business

    def test_the_client_gets_the_config_of_the_file_even_when_the_four_variables_are_in_the_env_argument(self):
        # Plan kpi-multi-project step 3 review: the post-write `load_config(path=target, env={})` must not take the
        # `env` of the run. With all four ODOO_KPI_* values set (and a project variable) the connection test still logs
        # in with what the human just typed: a config of source "env" would test the wrong server with the wrong key.
        install_catalogs(self, "alpha")
        seen = []
        environment = dict(zip(odoo_rpc.ENV_NAMES.values(), ("https://other.example.com", "other_db", "other.login",
                                                             "OTHER-ENV-VALUE")))
        environment[odoo_rpc.PROJECT_ENV] = "alpha"
        result = self.call(Session(flow(host="my-instance.example.com")), env=environment,
                           client_factory=stub_factory(seen=seen))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        (config, confirm), = seen
        self.assertEqual(config.source, "file")
        self.assertEqual((config.url, config.db, config.login, config.api_key), (URL, FAKE_DB, FAKE_LOGIN, FAKE_KEY))
        self.assertIsNone(config.project)                                # the project variable is not carried either
        self.assertEqual(confirm, "my-instance.example.com")
        self.assertIn("ODOO_KPI_* environment variables", result.out)    # the run saw the variables (the note is printed)
        self.assertNotIn(b"OTHER-ENV-VALUE", self.read())                # and none of them reached the file

    def test_a_non_local_host_needs_the_full_host_typed_otherwise_the_existing_guard_refuses_and_the_file_stays(self):
        for typed in ("wrong.example.com", "my-instance", "my-instance.example.com.evil.test", "example.com",
                      "https://my-instance.example.com", "my-instance.example.com/"):
            with self.subTest(typed=typed):
                if os.path.exists(self.target):
                    os.unlink(self.target)
                session = Session(flow(url=URL, host=typed))
                result = self.call(session, client_factory=real_client_without_network)
                self.assertIsInstance(result.exc, odoo_rpc.GuardError, (typed, result.trace))
                self.assertIn("full host name", str(result.exc))
                self.assertTrue(os.path.isfile(self.target))
                self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).api_key, FAKE_KEY)
                self.assertNotIn("login: ok", result.out)
                self.assertNoLeak(result, session)

    def test_the_right_full_host_typed_again_passes_the_guard_whatever_the_case(self):
        for typed in ("my-instance.example.com", "MY-INSTANCE.EXAMPLE.COM", "  my-instance.example.com  "):
            with self.subTest(typed=typed):
                if os.path.exists(self.target):
                    os.unlink(self.target)
                opener = RecordingOpener(FAKE_UID)
                factory = lambda config, confirm_instance=None: odoo_rpc.Client(          # noqa: E731
                    config, confirm_instance=confirm_instance, opener=opener)
                result = self.call(Session(flow(url=URL, host=typed)), client_factory=factory)
                self.assertEqual((result.code, result.exc), (0, None), result.trace)
                self.assertEqual(len(opener.requests), 1)

    def test_a_wrong_login_exits_through_the_rpc_error_and_the_file_stays(self):
        self.fake.mode = "login_false"
        result = self.call(Session(flow(url=self.fake.url, host="127.0.0.1")))
        self.assertIsInstance(result.exc, odoo_rpc.RpcError)
        self.assertTrue(os.path.isfile(self.target))


# ================================================================== the Windows branch (injected SDDL)

class WindowsBranchTests(InitConfigCase):
    def nt(self, session, sddl_text=GOOD_SDDL, calls=None, **kw):
        def sddl(path):
            if calls is not None:
                calls.append(os.path.abspath(path))
            return sddl_text(path) if callable(sddl_text) else sddl_text

        return self.call(session, os_name="nt", sddl=sddl, user_sid=USER, **kw)

    def test_a_good_sddl_on_the_temp_proceeds_and_the_file_is_written(self):
        calls = []
        with Spy(self.target) as spy:
            result = self.nt(Session(flow()), calls=calls)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        (src,) = [event[1] for event in spy.of("replace")]
        self.assertEqual(calls, [src], "the SDDL must be read from the temp file once (no connection test)")
        self.assertEqual([event[2] for event in spy.of("check")], [0], "read from the still EMPTY temp file")
        self.assertTrue(os.path.isfile(self.target))
        self.assertEqual(os.listdir(self.folder), ["config.ini"])

    def test_with_a_connection_test_the_sddl_is_read_from_the_temp_then_from_the_final_file(self):
        calls = []
        with Spy(self.target) as spy:
            result = self.nt(Session(flow(host="my-instance.example.com")), calls=calls, client_factory=stub_factory())
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        (src,) = [event[1] for event in spy.of("replace")]
        self.assertEqual(calls, [src, os.path.abspath(self.target)])
        self.assertIn("login: ok", result.out)

    def test_a_wide_open_sddl_on_the_temp_refuses_removes_the_temp_and_leaves_the_target_alone(self):
        for old_present in (False, True):
            with self.subTest(old_file=old_present):
                shutil.rmtree(self.folder, ignore_errors=True)
                if old_present:
                    self.make_old(mode=0o644)
                else:
                    os.makedirs(self.folder)
                before, session = self.tree(), Session((["yes"] if old_present else []) + flow())
                calls = []
                with Spy(self.target) as spy:
                    result = self.nt(session, sddl_text=OPEN_SDDL, calls=calls)
                self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)
                self.assertIn("WD", str(result.exc))
                self.assertEqual(len(calls), 1)
                self.assertTrue(os.path.basename(calls[0]).startswith(TEMP_PREFIX))
                self.assertEqual(spy.of("replace"), [], "the file was moved although the check refused it")

    def test_another_users_sid_or_a_broad_group_is_refused_the_own_sid_and_system_are_not(self):
        for label, text, refused in (
                ("another user", "D:PAI(A;;FA;;;SY)(A;;FA;;;S-1-5-21-1-2-3-1002)", True),
                ("Users", "D:PAI(A;;FA;;;SY)(A;;FR;;;BU)", True), ("Everyone SID", "D:(A;;FR;;;S-1-1-0)", True),
                ("own SID only", "D:PAI(A;;FA;;;%s)" % USER, False),
                ("SYSTEM + admins + own, a deny for Users", GOOD_SDDL + "(D;;FR;;;BU)", False)):
            with self.subTest(case=label):
                if os.path.exists(self.target):
                    os.unlink(self.target)
                result = self.nt(Session(flow()), sddl_text=text)
                if refused:
                    self.assertIsInstance(result.exc, odoo_rpc.GuardError)
                    self.assertFalse(os.path.exists(self.target))
                else:
                    self.assertEqual((result.code, result.exc), (0, None), result.trace)

    def test_an_unreadable_sddl_refuses_and_removes_the_temp(self):
        def unreadable(path):
            raise odoo_rpc.GuardError("cannot read the permissions of the config file (icacls failed)")

        os.makedirs(self.folder)
        before, session = self.tree(), Session(flow())
        result = self.nt(session, sddl_text=unreadable)
        self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)

    def test_the_posix_mode_check_is_not_what_decides_on_windows(self):
        # a world-readable mode on the POSIX temp is irrelevant when the injected SDDL is clean: the branch is the SDDL
        if os.name == "nt":
            self.skipTest("POSIX modes do not exist here")
        calls = []
        real_mkstemp = odoo_rpc.tempfile.mkstemp

        def loose_mkstemp(*args, **kwargs):
            handle, temp = real_mkstemp(*args, **kwargs)
            os.chmod(temp, 0o666)
            return handle, temp

        with mock.patch.object(odoo_rpc.tempfile, "mkstemp", loose_mkstemp):
            result = self.nt(Session(flow()), calls=calls)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(len(calls), 1)

    def test_the_windows_default_paths_are_resolved_with_ntpath_and_nothing_is_written(self):
        cwd_before = sorted(os.listdir(os.getcwd()))
        cases = {"appdata": ({"APPDATA": "C:\\Users\\qa\\AppData\\Roaming"},
                             "C:\\Users\\qa\\AppData\\Roaming\\odoo-kpi\\config.ini"),
                 "userprofile fallback": ({"USERPROFILE": "C:\\Users\\qa"},
                                          "C:\\Users\\qa\\AppData\\Roaming\\odoo-kpi\\config.ini"),
                 "override wins": ({"ODOO_KPI_CONFIG": "D:\\keys\\mine.ini", "APPDATA": "C:\\x"}, "D:\\keys\\mine.ini")}
        for label, (env, expected) in cases.items():
            with self.subTest(case=label):
                before, session = self.tree(), Session(["not a url"])      # refused at the URL: nothing is written
                result = self.call(session, path=None, env=env, os_name="nt", sddl=lambda p: GOOD_SDDL, user_sid=USER)
                self.assertRefusedUntouched(result, session, odoo_rpc.ConfigError, before)
                self.assertEqual(result.out.splitlines()[0], "The config file will be: %s" % expected)
        self.assertEqual(ntpath.join("C:\\Users\\qa", "AppData", "Roaming", "odoo-kpi", "config.ini"),
                         cases["userprofile fallback"][1])               # the expectation itself is plain ntpath
        self.assertEqual(sorted(os.listdir(os.getcwd())), cwd_before, "a Windows-style file name leaked into the cwd")


# ================================================================== the hidden prompt

class HiddenPromptTests(InitConfigCase):
    def test_a_normal_answer_is_returned_untouched_and_getpass_gets_the_prompt(self):
        with mock.patch("getpass.getpass", return_value="typed-test-value") as fake:
            self.assertEqual(odoo_rpc._hidden_prompt("Key: "), "typed-test-value")
        fake.assert_called_once_with("Key: ")

    def test_an_echo_fallback_warning_refuses_with_the_environment_variable_advice(self):
        def echoing(prompt):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            return "typed-in-the-open"

        with mock.patch("getpass.getpass", echoing):
            with self.assertRaises(odoo_rpc.GuardError) as caught:
                odoo_rpc._hidden_prompt("Key: ")
        message = str(caught.exception)
        for name in odoo_rpc.ENV_NAMES.values():
            self.assertIn(name, message)
        self.assertNotIn("typed-in-the-open", message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_an_unrelated_warning_is_not_turned_into_a_refusal_and_the_filters_are_restored(self):
        def other(prompt):
            warnings.warn("something else", RuntimeWarning)
            return "typed-test-value"

        with mock.patch("getpass.getpass", other), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(odoo_rpc._hidden_prompt("Key: "), "typed-test-value")
        self.assertEqual([w.category for w in caught], [RuntimeWarning])

    def test_the_process_warning_filters_are_restored_after_the_prompt_ends_either_way(self):
        def echoing(prompt):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            return "typed-in-the-open"

        with warnings.catch_warnings():                                 # a clean, private filter list to compare with
            warnings.resetwarnings()
            before = list(warnings.filters)
            with mock.patch("getpass.getpass", return_value="typed-test-value"):
                odoo_rpc._hidden_prompt("Key: ")
            self.assertEqual(list(warnings.filters), before)
            with mock.patch("getpass.getpass", echoing):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc._hidden_prompt("Key: ")
            self.assertEqual(list(warnings.filters), before)

    def test_init_config_without_ask_secret_uses_the_hidden_prompt_for_the_key_only(self):
        session = Session(flow())
        with mock.patch("getpass.getpass", return_value=FAKE_KEY) as fake:
            result = self.call(session, ask_secret=None)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(fake.call_count, 1)
        self.assertIn("API key", fake.call_args[0][0])
        self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).api_key, FAKE_KEY)

    def test_an_echo_fallback_inside_init_config_writes_nothing_and_the_key_is_not_stored(self):
        def echoing(prompt):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            return FAKE_KEY

        before, session = self.tree(), Session(flow()[:3])
        with mock.patch("getpass.getpass", echoing):
            result = self.call(session, ask_secret=None)
        self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)


# ================================================================== exit codes through main

class MainExitCodeTests(InitConfigCase):
    def run_main(self, *argv, side_effect=None, return_value=0):
        out, err = Terminal(), io.StringIO()
        with mock.patch.object(odoo_rpc, "init_config", side_effect=side_effect, return_value=return_value) as fake:
            code = odoo_rpc.main(list(argv), out, err)
        return code, out.getvalue(), err.getvalue(), fake, out

    def test_exit_codes_and_the_one_line_errors(self):
        cases = (("success", dict(return_value=0), 0, ""), ("cancelled", dict(return_value=1), 1, ""),
                 ("guard", dict(side_effect=odoo_rpc.GuardError("terminal needed")), 2, "error: terminal needed\n"),
                 ("config error", dict(side_effect=odoo_rpc.ConfigError("bad field")), 2, "error: bad field\n"),
                 ("rpc error", dict(side_effect=odoo_rpc.RpcError("server said no")), 1, "error: server said no\n"),
                 ("unexpected", dict(side_effect=ValueError("secret detail " + FAKE_KEY)), 1,
                  "error: unexpected ValueError\n"),
                 ("end of input", dict(side_effect=EOFError("")), 1, "error: unexpected EOFError\n"))
        for label, kw, code, err_text in cases:
            with self.subTest(case=label):
                got, out, err, fake, _ = self.run_main("init-config", **kw)
                self.assertEqual((got, err), (code, err_text))
                self.assertEqual(out, "")
                self.assertNotIn(FAKE_KEY, out + err)

    def test_main_passes_only_the_config_option_and_its_stdout_and_never_a_key_or_a_host(self):
        got, _, _, fake, out = self.run_main("init-config", "--config", "/x/y.ini", "--confirm-instance", "host.example")
        self.assertEqual(got, 0)
        fake.assert_called_once_with(path="/x/y.ini", stdout=out)
        got, _, _, fake, out = self.run_main("init-config")
        fake.assert_called_once_with(path=None, stdout=out)

    def test_no_option_takes_the_key(self):
        for flag in ("--api-key", "--key", "--password", "--token", "--secret"):
            with self.subTest(flag=flag):
                with mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch.object(odoo_rpc, "init_config") as fake:
                    with self.assertRaises(SystemExit) as caught:
                        odoo_rpc.main(["init-config", flag, FAKE_KEY], Terminal(), io.StringIO())
                self.assertEqual(caught.exception.code, 2)
                fake.assert_not_called()

    def test_a_keyboard_interrupt_is_not_swallowed_into_an_exit_code(self):
        with mock.patch.object(odoo_rpc, "init_config", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                odoo_rpc.main(["init-config"], Terminal(), io.StringIO())

    def test_claudecode_in_the_real_environment_gives_exit_2_and_nothing_is_written(self):
        target = os.path.join(self.tmp, "cfg", "config.ini")
        out, err = Terminal(), io.StringIO()
        before = self.tree()
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            code = odoo_rpc.main(["init-config", "--config", target], out, err)
        self.assertEqual(code, 2)
        self.assertIn("Claude Code", err.getvalue())
        self.assertEqual((out.getvalue(), self.tree()), ("", before))

    def test_a_stdout_that_is_not_a_terminal_gives_exit_2_and_nothing_is_written(self):
        before = self.tree()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", Terminal()):
            code = odoo_rpc.main(["init-config", "--config", os.path.join(self.tmp, "cfg", "c.ini")], out, err)
        self.assertEqual(code, 2)
        self.assertIn("needs a terminal", err.getvalue())
        self.assertEqual(self.tree(), before)

    def test_main_wiring_end_to_end_on_fake_terminals_reads_the_prompts_from_sys_stdin(self):
        target = os.path.join(self.tmp, "cfg", "config.ini")
        screen = Terminal()
        lines = "\n".join([URL, FAKE_DB, FAKE_LOGIN, ""]) + "\n"
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", Terminal(lines)), mock.patch.object(sys, "stdout", screen), \
                mock.patch("builtins.input", REAL_INPUT), mock.patch("getpass.getpass", return_value=FAKE_KEY):
            code = odoo_rpc.main(["init-config", "--config", target], screen, err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertNotIn(FAKE_KEY, screen.getvalue())
        self.assertIn("Config written", screen.getvalue())
        self.assertEqual(odoo_rpc.load_config(path=target, env={}).api_key, FAKE_KEY)

    def test_main_with_an_input_that_ends_early_exits_1_with_the_type_only_and_writes_nothing(self):
        target = os.path.join(self.tmp, "cfg", "config.ini")
        screen, err, before = Terminal(), io.StringIO(), self.tree()
        with mock.patch.object(sys, "stdin", Terminal("")), mock.patch.object(sys, "stdout", screen), \
                mock.patch("builtins.input", REAL_INPUT):
            code = odoo_rpc.main(["init-config", "--config", target], screen, err)
        self.assertEqual((code, err.getvalue()), (1, "error: unexpected EOFError\n"))
        self.assertEqual(self.tree(), before)


# ================================================================== an API key typed at the wrong prompt (review F2)

def hex40(case="lower"):
    """40 hex characters, the shape of an Odoo API key (built here, never a real one)."""
    text = "0123456789abcdef" * 2 + "01234567"
    return {"lower": text, "upper": text.upper(), "mixed": "".join(c.upper() if i % 2 else c for i, c in enumerate(text))}[case]


KEY_TEXT = ("what you typed as %s looks like an API key: %s. If it was a key, delete it in Odoo (Preferences > Account "
            "Security) and create a new one, because it was shown on the screen")
NOT_WRITTEN = "nothing was written"
ALREADY_WRITTEN = "the config file was already written, so run init-config again after creating a new key"


class WrongPromptKeyTests(InitConfigCase):
    """An address, db name, login or typed host that is 40 hex characters is refused with a STATIC text (never the value).

    The address, db and login are asked BEFORE the file is written (the text says nothing was written); the typed host is
    asked AFTER the write (the text says the file was already written, and what to do)."""

    def assertStaticRefusal(self, result, session, value, name, written=False):
        self.assertIsInstance(result.exc, odoo_rpc.ConfigError, (result.exc, result.trace))
        message = str(result.exc)
        self.assertIn("what you typed as %s looks like an API key" % name, message)
        self.assertEqual(message, KEY_TEXT % (name, ALREADY_WRITTEN if written else NOT_WRITTEN))   # fully static
        if written:
            self.assertIn(ALREADY_WRITTEN, message)
            self.assertNotIn(NOT_WRITTEN, message)
        else:
            self.assertIn(NOT_WRITTEN, message)
            self.assertNotIn("already written", message)
        self.assertIn("Preferences > Account Security", message)
        self.assertIn("delete it in Odoo", message)
        for text in (message, repr(result.exc), result.out, result.real, result.trace, " ".join(session.prompts)):
            self.assertNotIn(value.lower(), text.lower())
            self.assertNotIn(value[:8].lower(), text.lower())
        for name_in_tree in self.tree():
            self.assertNotIn(value[:8].lower(), name_in_tree.lower())

    def test_a_40_hex_value_at_the_database_prompt_is_refused_before_the_key_prompt_and_any_write(self):
        for case in ("lower", "upper", "mixed"):
            for padding in ("", "  ", "\t"):
                with self.subTest(case=case, padding=repr(padding)):
                    value = hex40(case)
                    before, session = self.tree(), Session([URL, padding + value + padding, FAKE_LOGIN, ""])
                    result = self.call(session)
                    self.assertStaticRefusal(result, session, value, "the database name")
                    self.assertEqual(self.tree(), before)
                    self.assertEqual((len(session.prompts), session.secret_prompts), (2, []))
                    self.assertFalse(os.path.exists(self.folder))

    def test_a_40_hex_value_at_the_login_prompt_is_refused_before_the_key_prompt_and_any_write(self):
        for case in ("lower", "upper", "mixed"):
            for padding in ("", " "):
                with self.subTest(case=case, padding=repr(padding)):
                    value = hex40(case)
                    before, session = self.tree(), Session([URL, FAKE_DB, padding + value + padding, ""])
                    result = self.call(session)
                    self.assertStaticRefusal(result, session, value, "the login")
                    self.assertEqual(self.tree(), before)
                    self.assertEqual((len(session.prompts), session.secret_prompts), (3, []))
                    self.assertFalse(os.path.exists(self.folder))

    def test_a_40_hex_value_at_the_host_prompt_is_refused_after_the_write_and_the_file_stays(self):
        for case in ("lower", "upper", "mixed"):
            for padding in ("", "  "):
                with self.subTest(case=case, padding=repr(padding)):
                    shutil.rmtree(self.folder, ignore_errors=True)
                    value = hex40(case)
                    session = Session(flow(host=padding + value + padding))
                    result = self.call(session, client_factory=never_called)
                    self.assertStaticRefusal(result, session, value, "the host name", written=True)
                    self.assertIn("Config written", result.out)
                    self.assertNotIn("login: ok", result.out)
                    self.assertEqual(len(session.prompts), 4)
                    self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).api_key, FAKE_KEY)

    def test_the_host_text_says_the_file_was_written_while_address_db_and_login_say_nothing_was_written(self):
        value, texts = hex40("lower"), {}
        for label, answers in (("the address", [value, FAKE_DB, FAKE_LOGIN, ""]),
                               ("the database name", [URL, value, FAKE_LOGIN, ""]),
                               ("the login", [URL, FAKE_DB, value, ""]),
                               ("the host name", flow(host=value))):
            shutil.rmtree(self.folder, ignore_errors=True)
            result = self.call(Session(answers), client_factory=never_called)
            self.assertIsInstance(result.exc, odoo_rpc.ConfigError, (label, result.trace))
            texts[label] = str(result.exc)
        for label in ("the address", "the database name", "the login"):
            self.assertIn(NOT_WRITTEN, texts[label], label)
            self.assertNotIn("already written", texts[label], label)
        self.assertNotIn(NOT_WRITTEN, texts["the host name"])
        self.assertIn(ALREADY_WRITTEN, texts["the host name"])
        # only the field name and the written/not written part differ
        self.assertEqual(texts["the host name"], KEY_TEXT % ("the host name", ALREADY_WRITTEN))
        self.assertEqual(texts["the login"], KEY_TEXT % ("the login", NOT_WRITTEN))

    def test_the_host_refusal_leaves_the_file_exactly_as_written_and_the_old_file_is_already_replaced(self):
        self.make_old()
        value = hex40("upper")
        session = Session(["yes"] + flow(host=value))
        result = self.call(session, client_factory=never_called)
        self.assertStaticRefusal(result, session, value, "the host name", written=True)
        config = odoo_rpc.load_config(path=self.target, env={})
        self.assertEqual((config.url, config.db, config.login, config.api_key), (URL, FAKE_DB, FAKE_LOGIN, FAKE_KEY))
        self.assertEqual([n for n in self.tree() if os.path.basename(n).startswith(TEMP_PREFIX)], [])

    # ---- the address prompt (the first of the typed fields)
    def test_a_40_hex_value_at_the_address_prompt_is_refused_after_one_prompt_and_before_any_write(self):
        for case in ("lower", "upper", "mixed"):
            for padding in ("", "  ", "\t"):
                with self.subTest(case=case, padding=repr(padding)):
                    value = hex40(case)
                    before, session = self.tree(), Session([padding + value + padding, FAKE_DB, FAKE_LOGIN, ""])
                    result = self.call(session, client_factory=never_called)
                    self.assertStaticRefusal(result, session, value, "the address")
                    self.assertEqual(self.tree(), before)
                    self.assertEqual((len(session.prompts), session.secret_prompts), (1, []))
                    self.assertFalse(os.path.exists(self.folder))
                    self.assertNotIn("Config written", result.out)
                    self.assertEqual(session.answers, [FAKE_DB, FAKE_LOGIN, ""])        # db / login never asked

    def test_the_address_key_refusal_comes_from_the_key_check_not_from_the_url_rules(self):
        # the same 40 hex is also an invalid URL; the key text must win, and the URL message must not be the one shown
        result = self.call(Session([hex40(), FAKE_DB, FAKE_LOGIN, ""]))
        self.assertIn("looks like an API key", str(result.exc))
        self.assertNotIn("the url must look like", str(result.exc))

    def test_a_40_hex_address_is_refused_even_with_an_existing_file_after_the_replace_question(self):
        self.make_old()
        before, value = self.tree(), hex40("mixed")
        session = Session(["yes", value, FAKE_DB, FAKE_LOGIN, ""])
        result = self.call(session)
        self.assertStaticRefusal(result, session, value, "the address")
        self.assertEqual(self.tree(), before)                                       # the old file is untouched
        self.assertEqual((len(session.prompts), session.secret_prompts), (2, []))

    def test_other_address_shapes_are_not_given_the_key_text_and_follow_the_url_rules(self):
        value = hex40()
        for label, address in (("39 hex", value[:-1]), ("41 hex", value + "a"), ("40 non-hex", "g" * 40),
                               ("40 hex + dot", value + "."), ("40 hex + slash", value + "/"),
                               ("host label of 40 hex", "https://%s.example.com" % value),
                               ("host of exactly 40 hex", "https://%s" % value),
                               ("http host of exactly 40 hex", "http://%s" % value),
                               ("40 hex in a path", "https://my-instance.example.com/%s" % value),
                               ("40 hex after the host", "https://%s.example.com/" % value),
                               ("40 hex with a port", "https://%s:8069" % value),
                               ("the normal address", URL)):
            with self.subTest(shape=label):
                shutil.rmtree(self.folder, ignore_errors=True)
                problem = odoo_rpc._url_problem(address)
                result = self.call(Session([address, FAKE_DB, FAKE_LOGIN, ""]))
                if problem is None:
                    self.assertEqual((result.code, result.exc), (0, None), result.trace)
                    self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).url, address.rstrip("/"))
                else:
                    self.assertIsInstance(result.exc, odoo_rpc.ConfigError, result.trace)
                    self.assertEqual(str(result.exc), problem)
                    self.assertNotIn("API key", str(result.exc))
                    self.assertFalse(os.path.exists(self.folder))

    def test_a_host_of_exactly_40_hex_after_the_scheme_is_a_valid_address_and_is_written(self):
        # pins that the check is on the WHOLE typed value, not on the part after the scheme
        value = hex40("lower")
        result = self.call(Session(flow(url="https://" + value)))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).url, "https://" + value)

    def test_the_39_and_41_hex_addresses_are_refused_by_the_url_rules_after_one_prompt(self):
        for value in (hex40()[:-1], hex40() + "a"):
            with self.subTest(length=len(value)):
                session = Session([value, FAKE_DB, FAKE_LOGIN, ""])
                result = self.call(session)
                self.assertEqual(str(result.exc), "the url must look like https://host (scheme and host are required)")
                self.assertEqual(len(session.prompts), 1)

    def test_the_real_key_at_the_key_prompt_is_not_refused_even_when_it_is_40_hex(self):
        value = hex40("lower")
        result = self.call(Session(flow(), typed_key=value))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).api_key, value)

    def test_other_lengths_and_shapes_are_not_refused_at_db_login_or_host(self):
        shapes = {"39 hex": hex40()[:-1], "41 hex": hex40() + "a", "40 non-hex": "g" * 40, "40 with one non-hex": "z" + hex40()[1:],
                  "prefix + 40 hex": "db_" + hex40(), "40 hex + suffix": hex40() + "_x", "40 hex with a dash": hex40()[:20] + "-" + hex40()[21:],
                  "short hex": "abcdef", "uppercase non-hex words": "A" * 40 + "G"}
        for label, value in shapes.items():
            with self.subTest(shape=label):
                shutil.rmtree(self.folder, ignore_errors=True)
                result = self.call(Session(flow(db=value, login=value, host=value)), client_factory=stub_factory())
                self.assertEqual((result.code, result.exc), (0, None), (label, result.trace))
                config = odoo_rpc.load_config(path=self.target, env={})
                self.assertEqual((config.db, config.login), (value, value))

    def test_main_prints_the_static_refusal_with_exit_2_and_never_the_value(self):
        value = hex40("mixed")
        target = os.path.join(self.tmp, "cfg", "config.ini")
        screen, err, before = Terminal(), io.StringIO(), self.tree()
        lines = "\n".join([URL, value]) + "\n"
        with mock.patch.object(sys, "stdin", Terminal(lines)), mock.patch.object(sys, "stdout", screen), \
                mock.patch("builtins.input", REAL_INPUT):
            code = odoo_rpc.main(["init-config", "--config", target], screen, err)
        self.assertEqual(code, 2)
        self.assertIn("looks like an API key", err.getvalue())
        self.assertEqual(self.tree(), before)
        self.assertNotIn(value[:8].lower(), (err.getvalue() + screen.getvalue().replace(value, "")).lower())

    def run_main(self, lines, target):
        """`main(["init-config"])` on fake terminals: the prompts are read by the real `input()` from a fake stdin."""
        screen, err = Terminal(), io.StringIO()
        with mock.patch.object(sys, "stdin", Terminal("\n".join(lines) + "\n")), mock.patch.object(sys, "stdout", screen), \
                mock.patch("builtins.input", REAL_INPUT), mock.patch.object(odoo_rpc, "_hidden_prompt", return_value=FAKE_KEY):
            code = odoo_rpc.main(["init-config", "--config", target], screen, err)
        return code, screen.getvalue(), err.getvalue()

    def test_main_refuses_a_40_hex_address_with_exit_2_the_static_text_and_nothing_written(self):
        for case in ("lower", "upper", "mixed"):
            with self.subTest(case=case):
                value, target, before = hex40(case), os.path.join(self.tmp, "cfg", "config.ini"), self.tree()
                code, screen, err = self.run_main([value, FAKE_DB, FAKE_LOGIN, FAKE_KEY], target)
                self.assertEqual(code, 2)
                self.assertEqual(err, "error: " + KEY_TEXT % ("the address", NOT_WRITTEN) + "\n")
                self.assertEqual(self.tree(), before)
                self.assertFalse(os.path.exists(os.path.dirname(target)))
                self.assertNotIn("Database name", screen)                           # the next prompt was never shown
                self.assertNotIn("Config written", screen)
                for text in (err, screen.replace(value, "")):
                    self.assertNotIn(value[:8].lower(), text.lower())

    def test_main_refuses_a_40_hex_host_with_exit_2_the_already_written_text_and_the_file_stays(self):
        value, target = hex40("mixed"), os.path.join(self.tmp, "cfg", "config.ini")
        code, screen, err = self.run_main([URL, FAKE_DB, FAKE_LOGIN, value], target)
        self.assertEqual(code, 2)
        self.assertEqual(err, "error: " + KEY_TEXT % ("the host name", ALREADY_WRITTEN) + "\n")
        self.assertNotIn(NOT_WRITTEN, err)
        self.assertIn("Config written", screen)
        self.assertEqual(odoo_rpc.load_config(path=target, env={}).db, FAKE_DB)
        for text in (err, screen.replace(value, "")):
            self.assertNotIn(value[:8].lower(), text.lower())

    def test_main_does_not_give_the_key_text_for_a_normal_address_or_a_39_hex_one(self):
        code, screen, err = self.run_main([hex40()[:-1], FAKE_DB, FAKE_LOGIN, FAKE_KEY], os.path.join(self.tmp, "cfg", "config.ini"))
        self.assertEqual(code, 2)
        self.assertEqual(err, "error: the url must look like https://host (scheme and host are required)\n")


# ================================================================== the environment-variables note (review F3)

NOTE = ("Note: ODOO_KPI_* environment variables are set here. While all four are set they are used INSTEAD of this file.")


class EnvironmentNoteTests(InitConfigCase):
    NAMES = list(odoo_rpc.ENV_NAMES.values())

    def run_with(self, env, **kw):
        session = Session(flow())
        result = self.call(session, env=env, **kw)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        return result

    def test_the_note_is_printed_when_any_single_name_is_set_and_its_text_is_exact(self):
        for name in self.NAMES:
            with self.subTest(only=name):
                shutil.rmtree(self.folder, ignore_errors=True)
                result = self.run_with({name: "x"})
                self.assertEqual(result.out.splitlines().count(NOTE), 1)

    def test_the_note_is_printed_for_all_four_and_for_an_empty_value(self):
        shutil.rmtree(self.folder, ignore_errors=True)
        result = self.run_with(dict.fromkeys(self.NAMES, "x"))
        self.assertEqual(result.out.splitlines().count(NOTE), 1)
        shutil.rmtree(self.folder, ignore_errors=True)
        result = self.run_with({self.NAMES[3]: ""})                     # the variable exists, even if empty
        self.assertEqual(result.out.splitlines().count(NOTE), 1)

    def test_no_note_when_none_of_the_four_is_set_even_with_other_variables(self):
        result = self.run_with({"PATH": "/usr/bin", "ODOO_KPI_CONFIG": self.target, "ODOO_KPI": "x", "KPI_URL": "x"})
        self.assertNotIn("Note:", result.out)
        self.assertNotIn("ODOO_KPI_*", result.out)

    def test_the_note_comes_right_after_the_path_line_and_before_any_other_output(self):
        self.make_old()
        session = Session(["no"])
        result = self.call(session, env={self.NAMES[0]: "x"})
        self.assertEqual(result.out.splitlines()[:3], ["The config file will be: %s" % self.target, NOTE,
                                                        "Cancelled: nothing was written."])

    def test_the_note_is_read_from_the_injected_env_not_the_real_one(self):
        with mock.patch.dict(os.environ, dict.fromkeys(self.NAMES, "x")):
            result = self.run_with({})
        self.assertNotIn("Note:", result.out)
        shutil.rmtree(self.folder, ignore_errors=True)
        result = self.run_with({self.NAMES[1]: "x"})                    # the real env has none of them (isolated)
        self.assertIn(NOTE, result.out)

    def test_the_note_goes_to_the_injected_stdout_only_and_carries_no_value(self):
        env = dict(zip(self.NAMES, ("https://env.example.com", "env_db", "env.login", "ENV-VALUE")))
        result = self.run_with(env)
        self.assertEqual(result.real, "")
        for value in env.values():
            self.assertNotIn(value, result.out)

    def test_a_refused_run_prints_no_note(self):
        session = Session([])
        result = self.call(session, env={"CLAUDECODE": "1", self.NAMES[0]: "x"})
        self.assertIsInstance(result.exc, odoo_rpc.GuardError)
        self.assertEqual(result.out, "")


# ================================================================== the optional project prompt (plan kpi-multi-project step 3)

class OrderedSession(Session):
    """A Session that also records, in ONE list, the order of the visible and the hidden prompts."""

    def __init__(self, answers, typed_key=FAKE_KEY):
        super().__init__(answers, typed_key)
        self.order = []

    def ask(self, prompt):
        self.order.append(("ask", prompt))
        return super().ask(prompt)

    def ask_secret(self, prompt):
        self.order.append(("secret", prompt))
        return super().ask_secret(prompt)


PROJECT_MARKER = "zqxword"                      # a typed text that no message or screen may repeat
PROJECT_PROMPT = "Project (alpha, beta; Enter = none): "
UNKNOWN_PROJECT = "that project is not one of the installed source catalogs (installed: alpha, beta)"


def project_flow(project="", url=URL, db=FAKE_DB, login=FAKE_LOGIN, host=""):
    """The answers of a run with two or more catalogs on a fresh target: address, db, login, PROJECT, host."""
    return [url, db, login, project, host]


class ProjectCase(InitConfigCase):
    """`init_config` with the folder of the catalogs redirected (D11): `kpi_catalogs.CATALOG_DIR`, in process."""

    def ini(self):
        """The written config as an INI parser (interpolation off, like the writer)."""
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(self.read().decode("utf-8"))
        return parser

    def written_keys(self):
        return list(self.ini()["odoo"])

    def read_back(self, **kw):
        return odoo_rpc.load_config(path=self.target, env={}, os_name="nt", sddl=lambda p: GOOD_SDDL, user_sid=USER, **kw)

    def run_ordered(self, answers, **kw):
        session = OrderedSession(answers)
        return session, self.call(session, **kw)

    def assertNothingWritten(self, result, session, before, typed=None):
        self.assertIsInstance(result.exc, odoo_rpc.ConfigError, (result.exc, result.trace))
        self.assertEqual(self.tree(), before, "something was created or changed")
        self.assertFalse(os.path.exists(self.folder), "the config folder was created before the project was validated")
        self.assertEqual(session.secret_prompts, [], "the hidden key prompt was reached after a refused project")
        self.assertNoLeak(result, session)
        if typed:
            shown = [str(result.exc), repr(result.exc), result.out, result.real, result.trace,
                     " ".join(session.prompts + session.secret_prompts)]
            for text in shown:
                self.assertNotIn(typed, text)


class NoProjectPromptTests(ProjectCase):
    """Fewer than two source catalogs: today's prompt sequence, no `project` key (D7)."""

    def assertToday(self, session, result):
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        kinds = [kind for kind, _ in session.order]
        self.assertEqual(kinds, ["ask", "ask", "ask", "secret", "ask"])
        for _, prompt in session.order:
            self.assertNotIn("Project", prompt)
            self.assertNotIn("project", prompt)
        self.assertEqual(self.written_keys(), ["url", "db", "login", "api_key"])
        self.assertNotIn(b"project", self.read())
        self.assertIsNone(self.read_back().project)

    def test_one_catalog_asks_no_project_and_writes_no_project_key(self):
        install_catalogs(self, "dksk")
        session, result = self.run_ordered(flow())
        self.assertToday(session, result)

    def test_the_shipped_folder_has_one_catalog_so_nothing_is_patched_and_nothing_is_asked(self):
        session, result = self.run_ordered(flow())
        self.assertEqual(len(kpi_catalogs.list_projects()), 1)           # the premise: the plugin ships one catalog
        self.assertToday(session, result)

    def test_an_empty_folder_asks_no_project(self):
        install_catalogs(self)
        session, result = self.run_ordered(flow())
        self.assertToday(session, result)

    def test_an_unreadable_folder_asks_no_project_and_the_run_completes_like_with_one_catalog(self):
        missing_catalogs(self)
        session, result = self.run_ordered(flow())
        self.assertToday(session, result)
        self.assertIn("Config written", result.out)

    def test_a_catalog_error_of_the_listing_is_no_prompt_and_never_an_error(self):
        with mock.patch.object(kpi_catalogs, "list_projects", side_effect=kpi_catalogs.CatalogError("x")):
            session, result = self.run_ordered(flow())
        self.assertToday(session, result)

    def test_names_the_rule_refuses_are_not_counted_as_catalogs(self):
        # two files, but only ONE valid name: still no prompt
        install_catalogs(self, "alpha", "BAD", "dashboard1", "x.aliases")
        session, result = self.run_ordered(flow())
        self.assertToday(session, result)

    def test_a_symlink_or_a_directory_is_not_a_catalog_for_the_count(self):
        folder = install_catalogs(self, "alpha")
        os.makedirs(os.path.join(folder, "beta.json"))
        try:
            os.symlink(os.path.join(folder, "alpha.json"), os.path.join(folder, "gamma.json"))
        except (OSError, NotImplementedError, AttributeError):
            pass
        session, result = self.run_ordered(flow())
        self.assertToday(session, result)

    def test_with_one_catalog_the_project_environment_variable_changes_nothing(self):
        install_catalogs(self, "dksk")
        session, result = self.run_ordered(flow(), env={"ODOO_KPI_PROJECT": "dksk"})
        self.assertToday(session, result)
        self.assertNotIn("ODOO_KPI_* environment variables", result.out)       # not one of the four credentials


class ProjectPromptTests(ProjectCase):
    def setUp(self):
        super().setUp()
        install_catalogs(self, "beta", "alpha")                             # listed sorted, whatever the disk order

    def test_the_prompt_is_asked_once_between_the_login_and_the_hidden_key(self):
        session, result = self.run_ordered(project_flow("beta"), client_factory=never_called)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        kinds = [kind for kind, _ in session.order]
        self.assertEqual(kinds, ["ask", "ask", "ask", "ask", "secret", "ask"])
        address, db, login, project, key, host = [prompt for _, prompt in session.order]
        self.assertIn("Odoo address", address)
        self.assertEqual(db, "Database name: ")
        self.assertEqual(login, "Your Odoo login: ")
        self.assertEqual(project, PROJECT_PROMPT)
        self.assertEqual(key, "Your API key (typing is hidden): ")
        self.assertIn("FULL host name", host)
        self.assertEqual(sum(1 for _, prompt in session.order if prompt.startswith("Project")), 1)

    def test_the_prompt_lists_every_catalog_sorted(self):
        install_catalogs(self, "zeta", "alpha", "mid")
        session, result = self.run_ordered(project_flow("mid"))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertIn("Project (alpha, mid, zeta; Enter = none): ", session.prompts)

    def test_with_an_existing_file_the_replace_question_comes_first_then_the_same_order(self):
        self.make_old()
        session, result = self.run_ordered(["yes"] + project_flow("alpha"))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        asked = [prompt for kind, prompt in session.order if kind == "ask"]
        self.assertTrue(asked[0].startswith("A config file exists"))
        self.assertEqual(asked[4], PROJECT_PROMPT)
        self.assertEqual(session.order[5][0], "secret")

    def test_a_member_is_written_as_a_project_line_and_read_back(self):
        for name in ("alpha", "beta"):
            with self.subTest(project=name):
                shutil.rmtree(self.folder, ignore_errors=True)
                session, result = self.run_ordered(project_flow(name))
                self.assertEqual((result.code, result.exc), (0, None), result.trace)
                self.assertEqual(self.written_keys(), ["url", "db", "login", "api_key", "project"])
                self.assertIn(("project = %s\n" % name).encode(), self.read())
                self.assertEqual(self.read().rstrip().splitlines()[-1],("project = %s" % name).encode())
                config = self.read_back()
                self.assertEqual((config.project, config.url, config.db, config.login, config.api_key),
                                 (name, URL, FAKE_DB, FAKE_LOGIN, FAKE_KEY))
                self.assertEqual(os.listdir(self.folder), ["config.ini"])

    @POSIX_ONLY
    def test_the_file_with_a_project_is_still_private(self):
        self.run_ordered(project_flow("beta"))
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)
        self.assertEqual(odoo_rpc.load_config(path=self.target, env={}).project, "beta")

    def test_the_typed_name_is_stripped(self):
        session, result = self.run_ordered(project_flow("  beta \t"))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertIn(b"project = beta\n", self.read())
        self.assertEqual(self.read_back().project, "beta")

    def test_enter_or_blanks_write_no_project_key(self):
        for answer in ("", "   ", "\t"):
            with self.subTest(answer=repr(answer)):
                shutil.rmtree(self.folder, ignore_errors=True)
                session, result = self.run_ordered(project_flow(answer))
                self.assertEqual((result.code, result.exc), (0, None), result.trace)
                self.assertEqual(self.written_keys(), ["url", "db", "login", "api_key"])
                self.assertNotIn(b"project", self.read())
                self.assertIsNone(self.read_back().project)

    def test_the_project_environment_variable_is_not_carried_into_the_file(self):
        session, result = self.run_ordered(project_flow(""), env={"ODOO_KPI_PROJECT": "alpha"})
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertNotIn(b"project", self.read())
        self.assertNotIn("ODOO_KPI_* environment variables", result.out)

    def test_the_connection_test_gets_the_chosen_project_in_the_loaded_config(self):
        seen = []
        session, result = self.run_ordered(project_flow("beta", host="my-instance.example.com"),
                                           client_factory=stub_factory(seen=seen))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        (config, host), = seen
        self.assertEqual((config.project, host), ("beta", "my-instance.example.com"))
        self.assertNotIn("beta", repr(config))

    def test_a_windows_target_writes_the_project_through_the_checked_temp_file(self):
        calls = []

        def sddl(path):
            calls.append(os.path.abspath(path))
            return GOOD_SDDL

        with Spy(self.target) as spy:
            session, result = self.run_ordered(project_flow("alpha"), os_name="nt", sddl=sddl, user_sid=USER)
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        (src,) = [event[1] for event in spy.of("replace")]
        self.assertEqual(calls, [src])
        self.assertEqual([event[2] for event in spy.of("check")], [0], "the check runs on the still EMPTY temp file")
        self.assertEqual(self.read_back().project, "alpha")
        self.assertEqual(os.listdir(self.folder), ["config.ini"])

    def test_a_windows_refusal_of_the_temp_file_leaves_nothing_even_with_a_project(self):
        os.makedirs(self.folder)
        before = self.tree()
        session = OrderedSession(project_flow("alpha"))
        result = self.call(session, os_name="nt", sddl=lambda p: OPEN_SDDL, user_sid=USER)
        self.assertRefusedUntouched(result, session, odoo_rpc.GuardError, before)

    def test_the_project_of_the_old_file_is_not_carried_over_by_a_rerun(self):
        # 2 catalogs and Enter: the hand-added `project = beta` of the OLD file does not survive the re-run
        self.make_old(data=(OLD_TEXT + "project = beta\n").encode())
        session, result = self.run_ordered(["yes"] + project_flow(""))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertNotIn(b"project", self.read())
        self.assertNotIn(b"old_db", self.read())

    def test_the_old_file_is_not_opened_for_a_project(self):
        self.make_old(data=(OLD_TEXT + "project = beta\n").encode())
        with Spy(self.target) as spy:
            session, result = self.run_ordered(["yes"] + project_flow("alpha"))
        self.assertEqual((result.code, result.exc), (0, None), result.trace)
        self.assertEqual([event for event in spy.touching_target() if event[0] == "open"], [],
                         "the old config file was opened (to read its project?)")
        self.assertEqual(self.read_back().project, "alpha")


class ProjectRefusalTests(ProjectCase):
    def setUp(self):
        super().setUp()
        install_catalogs(self, "alpha", "beta")

    def test_a_pasted_api_key_gets_the_revoke_it_text_and_nothing_is_written(self):
        for case in ("lower", "upper", "mixed"):
            for padding in ("", "  ", "\t"):
                with self.subTest(case=case, padding=repr(padding)):
                    value = hex40(case)
                    before, session = self.tree(), OrderedSession(project_flow(padding + value + padding))
                    with Spy(self.target) as spy:
                        result = self.call(session)
                    self.assertNothingWritten(result, session, before)
                    self.assertEqual(str(result.exc), KEY_TEXT % ("the project", NOT_WRITTEN))      # fully static
                    self.assertIn("delete it in Odoo", str(result.exc))
                    for text in (str(result.exc), repr(result.exc), result.out, result.real, result.trace,
                                 " ".join(session.prompts)):
                        self.assertNotIn(value.lower(), text.lower())
                        self.assertNotIn(value[:8].lower(), text.lower())
                    self.assertEqual(spy.of("replace") + spy.of("check") + spy.of("os.open"), [])
                    self.assertEqual(len(session.prompts), 4, "no prompt after the project may be shown")

    def test_a_key_shaped_name_is_judged_before_membership(self):
        # the text of a refused KEY and of a refused unknown name must differ: `_not_a_key` runs first
        session = OrderedSession(project_flow(hex40()))
        result = self.call(session)
        self.assertNotEqual(str(result.exc), UNKNOWN_PROJECT)
        self.assertIn("looks like an API key", str(result.exc))

    def test_an_unknown_or_malformed_name_is_refused_without_an_echo_and_nothing_is_written(self):
        cases = {"unknown": PROJECT_MARKER, "upper case": "ALPHA", "title case": "Beta", "upper marker": PROJECT_MARKER.upper(),
                 "parent folder": "../" + PROJECT_MARKER, "path": "alpha/" + PROJECT_MARKER, "backslash": "alpha\\x",
                 "absolute": "/etc/" + PROJECT_MARKER, "with the extension": "alpha.json", "newline inside": "alpha\n" + PROJECT_MARKER,
                 "control characters": "alpha\x1b[31m" + PROJECT_MARKER, "NUL": "\x00" + PROJECT_MARKER,
                 "two names": "alpha,beta", "two names, space": "alpha beta", "prefix": "alph", "10 KB": PROJECT_MARKER * 1500,
                 "wildcard": "*" + PROJECT_MARKER, "none": "none", "non ASCII": "K" + PROJECT_MARKER}
        for label, value in cases.items():
            with self.subTest(case=label):
                before, session = self.tree(), OrderedSession(project_flow(value))
                with Spy(self.target) as spy:
                    result = self.call(session)
                echoed = None if label in ("prefix", "none") else (PROJECT_MARKER if len(value) >= 40 else value.strip())
                self.assertNothingWritten(result, session, before, typed=echoed)
                self.assertEqual(str(result.exc), UNKNOWN_PROJECT)
                self.assertEqual(spy.of("replace") + spy.of("check") + spy.of("os.open"), [])
                self.assertEqual(len(session.prompts), 4)

    def test_a_refused_project_leaves_an_existing_config_untouched(self):
        old = self.make_old()
        before = self.tree()
        session = OrderedSession(["yes"] + project_flow(PROJECT_MARKER))
        result = self.call(session)
        self.assertIsInstance(result.exc, odoo_rpc.ConfigError)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.read(old), OLD_TEXT.encode("utf-8"))
        self.assertEqual(session.secret_prompts, [])

    def test_a_refusal_never_prints_the_typed_name_through_main_and_exits_2_with_one_error_line(self):
        target = os.path.join(self.tmp, "cfg", "config.ini")
        before = self.tree()
        code, screen, err = self.run_main([URL, FAKE_DB, FAKE_LOGIN, PROJECT_MARKER], target)
        self.assertEqual(code, 2)
        self.assertEqual(err, "error: " + UNKNOWN_PROJECT + "\n")
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.exists(os.path.dirname(target)))
        self.assertNotIn(PROJECT_MARKER, err)

    def test_main_maps_a_pasted_key_at_the_project_prompt_to_exit_2_and_the_static_text(self):
        value = hex40("mixed")
        target = os.path.join(self.tmp, "cfg", "config.ini")
        before = self.tree()
        code, screen, err = self.run_main([URL, FAKE_DB, FAKE_LOGIN, value], target)
        self.assertEqual(code, 2)
        self.assertEqual(err, "error: " + KEY_TEXT % ("the project", NOT_WRITTEN) + "\n")
        self.assertEqual(self.tree(), before)
        self.assertNotIn(value[:8].lower(), (err + screen.replace(value, "")).lower())

    def test_main_with_a_member_writes_the_file_and_exits_0(self):
        target = os.path.join(self.tmp, "cfg", "config.ini")
        code, screen, err = self.run_main([URL, FAKE_DB, FAKE_LOGIN, "beta", ""], target)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Project (alpha, beta; Enter = none): ", screen)
        self.assertNotIn(FAKE_KEY, screen)
        self.assertEqual(odoo_rpc.load_config(path=target, env={}).project, "beta")

    def run_main(self, lines, target):
        """`main(["init-config"])` on fake terminals: the prompts are read by the real `input()` from a fake stdin."""
        screen, err = Terminal(), io.StringIO()
        with mock.patch.object(sys, "stdin", Terminal("\n".join(lines) + "\n")), mock.patch.object(sys, "stdout", screen), \
                mock.patch("builtins.input", REAL_INPUT), mock.patch.object(odoo_rpc, "_hidden_prompt", return_value=FAKE_KEY):
            code = odoo_rpc.main(["init-config", "--config", target], screen, err)
        return code, screen.getvalue(), err.getvalue()


# ================================================================== one real pseudo terminal (POSIX only)

@unittest.skipIf(os.name == "nt", "a pseudo terminal exists on POSIX only")
class PtyTests(IsolatedTestCase):
    """A real child process on a real pty: the human flow with a fake key. The child has an explicit environment (no
    CLAUDECODE, a temp HOME and ODOO_KPI_CONFIG), a hard timeout, and is always killed."""

    HARD_TIMEOUT = 30

    def child_env(self, **extra):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "xterm",
               "ODOO_KPI_CONFIG": os.path.join(self.tmp, "ptycfg", "odoo-kpi", "config.ini")}
        env.update(extra)
        return env

    def drive(self, env, steps, script=None):
        """Run `odoo_rpc.py init-config` on a pty; for each (marker, answer) wait for the marker, then type the answer.
        Returns (all bytes read from the pty, exit status or None).

        Plain `python script` (no `-I`): odoo_rpc.py imports its stdlib-only sibling `kpi_catalogs`, which is found
        through the script folder that plain python puts first on sys.path (the documented way to run the scripts;
        isolated mode would drop that folder). `script` = another copy of odoo_rpc.py (a tmp tree with its own `catalogs/`)."""
        import pty
        import select
        import signal
        script = script or str(REPO / "scripts" / "odoo_rpc.py")
        deadline = time.monotonic() + self.HARD_TIMEOUT
        pid, master = pty.fork()
        if pid == 0:                                                    # child: never returns into the test runner
            try:
                try:
                    import resource
                    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
                except (ImportError, ValueError, OSError):
                    pass
                os.execve(sys.executable, [sys.executable, script, "init-config"], env)
            finally:
                os._exit(127)
        seen, position, status = bytearray(), 0, None
        try:
            def pump(until=None):
                nonlocal position
                while time.monotonic() < deadline:
                    if until is not None and until in seen[position:]:
                        position = seen.find(until, position) + len(until)
                        return True
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        try:
                            chunk = os.read(master, 4096)
                        except OSError:                                 # EIO: the child closed its side
                            return False
                        if not chunk:
                            return False
                        seen.extend(chunk)
                return False
            for marker, answer in steps:
                if not pump(marker):
                    self.fail("did not see %r before the child ended or the timeout; saw: %r" % (marker, bytes(seen)[-300:]))
                os.write(master, answer.encode("utf-8") + b"\n")
            pump()                                                      # until EOF or the deadline
            while time.monotonic() < deadline:
                done, raw = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = os.WEXITSTATUS(raw) if os.WIFEXITED(raw) else -os.WTERMSIG(raw)
                    pid = None
                    break
                time.sleep(0.05)
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except OSError:
                    pass
            os.close(master)
        return bytes(seen), status

    def test_the_human_flow_shows_the_prompts_never_echoes_the_key_and_writes_a_private_file(self):
        typed = "FAKE-PTY-KEY-9f2c71-not-a-real-secret"
        env = self.child_env()
        screen, status = self.drive(env, [(b"Odoo address", "http://127.0.0.1:8069"), (b"Database name", "pty_db"),
                                         (b"Your Odoo login", "pty.login"), (b"typing is hidden): ", typed),
                                         (b"Enter skips the test): ", "")])
        self.assertEqual(status, 0, screen[-400:])
        self.assertNotIn(typed.encode(), screen, "the key was echoed on the terminal")
        self.assertNotIn(typed[:12].encode(), screen)
        for text in (b"The config file will be:", b"Config written", b"Connection test skipped", b"pty_db", b"pty.login"):
            self.assertIn(text, screen)
        self.assertNotIn(b"Project (", screen, "the shipped tree has one catalog: no project prompt")
        target = env["ODOO_KPI_CONFIG"]
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(target)).st_mode), 0o700)
        self.assertEqual(os.listdir(os.path.dirname(target)), ["config.ini"])
        config = odoo_rpc.load_config(path=target, env={})
        self.assertEqual((config.url, config.db, config.login, config.api_key),
                         ("http://127.0.0.1:8069", "pty_db", "pty.login", typed))

    def two_catalog_script(self):
        """A copy of the scripts next to a `catalogs/` folder with two valid names (plain python resolves
        `kpi_catalogs.CATALOG_DIR` from the script's own place, so a subprocess sees this copy and not the real tree)."""
        tree = os.path.join(self.tmp, "plugin")
        shutil.copytree(str(REPO / "scripts"), os.path.join(tree, "scripts"), ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(tree, "catalogs"))
        for name in ("alpha", "beta"):
            with open(os.path.join(tree, "catalogs", name + ".json"), "wb") as handle:
                handle.write(b"{}")
        return os.path.join(tree, "scripts", "odoo_rpc.py")

    def test_with_two_catalogs_the_project_prompt_appears_and_the_key_is_still_not_echoed(self):
        typed = "FAKE-PTY-KEY-9f2c71-not-a-real-secret"
        env = self.child_env()
        screen, status = self.drive(env, [(b"Odoo address", "http://127.0.0.1:8069"), (b"Database name", "pty_db"),
                                         (b"Your Odoo login", "pty.login"),
                                         (b"Project (alpha, beta; Enter = none): ", "beta"),
                                         (b"typing is hidden): ", typed), (b"Enter skips the test): ", "")],
                                   script=self.two_catalog_script())
        self.assertEqual(status, 0, screen[-400:])
        self.assertNotIn(typed.encode(), screen, "the key was echoed on the terminal")
        self.assertNotIn(typed[:12].encode(), screen)
        self.assertNotIn(b"dksk", screen, "the copy of the plugin must list ITS catalogs, not the shipped one")
        self.assertLess(screen.index(b"Project (alpha, beta"), screen.index(b"typing is hidden"))
        target = env["ODOO_KPI_CONFIG"]
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(target)), ["config.ini"])
        with open(target, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertIn("project = beta", lines)
        self.assertFalse([line for line in lines if typed in line and not line.startswith("api_key")])
        config = odoo_rpc.load_config(path=target, env={})
        self.assertEqual((config.url, config.db, config.login, config.api_key, config.project),
                         ("http://127.0.0.1:8069", "pty_db", "pty.login", typed, "beta"))

    def test_with_two_catalogs_a_pasted_key_at_the_project_prompt_is_refused_on_a_real_terminal(self):
        pasted = hex40("lower")
        env = self.child_env()
        screen, status = self.drive(env, [(b"Odoo address", "http://127.0.0.1:8069"), (b"Database name", "pty_db"),
                                         (b"Your Odoo login", "pty.login"),
                                         (b"Project (alpha, beta; Enter = none): ", pasted)],
                                   script=self.two_catalog_script())
        self.assertEqual(status, 2, screen[-400:])
        self.assertIn(b"looks like an API key", screen)
        self.assertNotIn(b"typing is hidden", screen)
        self.assertFalse(os.path.exists(os.path.dirname(env["ODOO_KPI_CONFIG"])), "something was written")

    def test_inside_claude_code_the_command_is_refused_even_on_a_real_terminal(self):
        env = self.child_env(CLAUDECODE="1")
        screen, status = self.drive(env, [])
        self.assertEqual(status, 2, screen[-400:])
        self.assertIn(b"Claude Code", screen)
        self.assertNotIn(b"Odoo address", screen)
        self.assertFalse(os.path.exists(os.path.dirname(env["ODOO_KPI_CONFIG"])))


if __name__ == "__main__":
    unittest.main()
