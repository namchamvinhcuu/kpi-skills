"""Command line of `scripts/validate_formulas.py`: `check`, `derive`, `check-drift`, exit codes and one-line errors.

Synthetic trees and dashboards only (see `formula_fixtures.py`). The real `client_functions.json` is never written:
every `derive` run gets an explicit `--out` inside the test's own temporary folder.
"""
import ast
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import formula_fixtures as fx
from formula_fixtures import dashboard, make_tree, registry_js, write, write_frozen, write_json
from helpers import IsolatedTestCase

import validate_formulas as vf  # noqa: E402

SCRIPT = os.path.join(str(fx.REPO), "scripts", "validate_formulas.py")


class CliTestCase(IsolatedTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = vf.main(list(argv), out, err)
        return code, out.getvalue(), err.getvalue()

    def assertCleanError(self, code, out, err, contains):
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(err.count("\n"), 1, err)
        self.assertNotIn("Traceback", err)
        self.assertIn(contains, err)

    def tree(self, name, adds=("ODOO.A", "ODOO.B"), categories=None):
        modules = {"spreadsheet_edition": {"a.js": registry_js(*adds)}} if adds else None
        return make_tree(os.path.join(self.tmp, name), core=fx.core_source(categories), modules=modules)

    def dashboard_file(self, cells, name="dashboard.json"):
        return write_json(os.path.join(self.tmp, name), dashboard(cells))


class CheckCommandTests(CliTestCase):
    def test_a_dashboard_of_known_functions_is_ok(self):
        path = self.dashboard_file({"A1": '=IFERROR(ODOO.LIST(1,1,"f")+0,0)', "A2": "=SUM(A1:A3)", "A3": "=1"})
        self.assertEqual(self.run_cli("check", path), (0, "functions: ok\n", ""))

    def test_an_unknown_function_gives_exit_1_the_problems_on_stderr_and_the_count_on_stdout(self):
        path = self.dashboard_file({"A1": "=IFERROR(N(A1),0)", "A2": "=FOO(1)+SUM(1)"})
        code, out, err = self.run_cli("check", path)
        self.assertEqual(code, 1)
        self.assertEqual(out, "functions: 2 unknown\n")
        self.assertEqual(err, "function FOO() is not defined by the vendored client (cells: A2)\n"
                              "function N() is not defined by the vendored client (cells: A1)\n")

    def test_n_alone_is_one_problem(self):
        code, out, err = self.run_cli("check", self.dashboard_file({"G22": '=IFERROR(N(ODOO.LIST(1,1,"m")),0)'}))
        self.assertEqual((code, out), (1, "functions: 1 unknown\n"))
        self.assertIn("function N()", err)
        self.assertIn("G22", err)

    def test_the_default_list_is_the_real_frozen_one(self):
        self.assertEqual(self.run_cli("check", self.dashboard_file({"A1": "=SUM(1)+ODOO.PIVOT(1,\"x\")+_t(\"a\")"}))[0], 0)
        self.assertEqual(self.run_cli("check", self.dashboard_file({"A1": "=FOO(1)"}))[0], 1)

    def test_functions_option_points_at_another_frozen_file(self):
        frozen = write_frozen(os.path.join(self.tmp, "mine.json"), {"SUM"})
        ok = self.dashboard_file({"A1": "=SUM(1)"})
        self.assertEqual(self.run_cli("check", ok, "--functions", frozen), (0, "functions: ok\n", ""))
        other = self.dashboard_file({"A1": "=IF(1,2,3)"}, name="other.json")           # known to the default list only
        code, out, err = self.run_cli("check", other, "--functions", frozen)
        self.assertEqual((code, out), (1, "functions: 1 unknown\n"))
        self.assertIn("function IF()", err)
        self.assertEqual(self.run_cli("check", other)[0], 0)

    def test_a_missing_or_broken_functions_file_is_a_clean_error(self):
        path = self.dashboard_file({"A1": "=SUM(1)"})
        code, out, err = self.run_cli("check", path, "--functions", os.path.join(self.tmp, "none.json"))
        self.assertCleanError(code, out, err, "cannot read the frozen function list")
        bad = write(os.path.join(self.tmp, "bad.json"), '{"schema": 9, "functions": ["SUM"]}')
        self.assertCleanError(*self.run_cli("check", path, "--functions", bad), "unexpected shape")

    def test_a_functions_file_that_is_a_json_list_is_a_clean_error(self):
        # RED today: load_frozen calls `.get` on the list -> AttributeError -> `error: unexpected AttributeError`, exit 1
        path = self.dashboard_file({"A1": "=SUM(1)"})
        listing = write(os.path.join(self.tmp, "list.json"), '["SUM"]')
        self.assertCleanError(*self.run_cli("check", path, "--functions", listing), "unexpected shape")

    def test_a_file_that_is_not_json_is_a_clean_error(self):
        path = write(os.path.join(self.tmp, "x.json"), "{not json")
        code, out, err = self.run_cli("check", path)
        self.assertCleanError(code, out, err, "%s is not JSON" % path)

    def test_a_json_document_that_is_not_a_dashboard_is_a_clean_error(self):
        for text in ("[]", '"text"', "5", "null", "{}", '{"sheets": 5}', '{"sheets": {}}', '{"sheets": null}'):
            with self.subTest(text=text):
                path = write(os.path.join(self.tmp, "x.json"), text)
                self.assertCleanError(*self.run_cli("check", path), "is not a spreadsheet JSON (no sheets)")

    def test_a_dashboard_with_an_empty_sheet_list_is_ok(self):
        path = write(os.path.join(self.tmp, "x.json"), '{"sheets": []}')
        self.assertEqual(self.run_cli("check", path), (0, "functions: ok\n", ""))

    def test_odd_cells_do_not_break_the_command(self):
        path = write(os.path.join(self.tmp, "x.json"),
                     '{"sheets": [{"cells": {"A1": "=SUM(1)", "A2": null, "A3": {"content": 5}, "A4": {"content": "=SUM(1)"}}}]}')
        self.assertEqual(self.run_cli("check", path), (0, "functions: ok\n", ""))

    def test_a_missing_file_or_a_directory_is_a_clean_error(self):
        missing = os.path.join(self.tmp, "missing.json")
        code, out, err = self.run_cli("check", missing)
        self.assertCleanError(code, out, err, "cannot read %s" % missing)
        self.assertCleanError(*self.run_cli("check", self.tmp), "cannot read")

    def test_a_non_utf8_file_is_a_clean_error(self):
        path = write(os.path.join(self.tmp, "x.json"), data=b'{"sheets": []} \xff\xfe')
        self.assertCleanError(*self.run_cli("check", path), "is not UTF-8 text")

    def test_a_file_over_the_size_limit_is_refused(self):
        path = write(os.path.join(self.tmp, "x.json"), '{"sheets": []}' + " " * 100)
        with mock.patch.object(vf, "MAX_FILE", 50):
            self.assertCleanError(*self.run_cli("check", path), "is larger than")

    def test_a_file_exactly_at_the_size_limit_is_read(self):
        text = '{"sheets": []}'
        path = write(os.path.join(self.tmp, "x.json"), text)
        with mock.patch.object(vf, "MAX_FILE", len(text)):
            self.assertEqual(vf._read_json(path), {"sheets": []})      # (the frozen list is read under the same limit)
        with mock.patch.object(vf, "MAX_FILE", len(text) - 1):
            self.assertCleanError(*self.run_cli("check", path), "is larger than")

    def test_a_deeply_nested_json_is_not_json_and_not_a_crash(self):
        for text in ("[" * 100000, '{"a":' * 100000, "[" * 100000 + "]" * 100000):
            with self.subTest(head=text[:6]):
                path = write(os.path.join(self.tmp, "x.json"), text)
                self.assertCleanError(*self.run_cli("check", path), "is not JSON")

    def test_check_never_writes_anything(self):
        path = self.dashboard_file({"A1": "=SUM(1)"})
        before = sorted(os.listdir(self.tmp))
        self.run_cli("check", path)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)


class DeriveCommandTests(CliTestCase):
    def test_it_writes_the_file_prints_the_count_and_returns_0(self):
        first, second, third = self.tree("a"), os.path.join(self.tmp, "b"), self.tree("c", adds=("ODOO.A",))
        write(os.path.join(second, "spreadsheet_dashboard", "static", "src", "x.js"), registry_js("ODOO.D"))
        out_path = os.path.join(self.tmp, "frozen.json")
        code, out, err = self.run_cli("derive", "--addons-path", "%s,%s" % (first, second), "--addons-path", third,
                                      "--out", out_path)
        document = vf.derive([[first, second], [third]])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "wrote %s: %d functions from 2 tree(s)\n" % (out_path, len(document["functions"])))
        with open(out_path, "rb") as handle:
            self.assertEqual(handle.read(), vf.dump_bytes(document))
        self.assertIn("ODOO.A", document["functions"])
        self.assertNotIn("ODOO.B", document["functions"])             # tree c lacks it: intersection
        self.assertNotIn("ODOO.D", document["functions"])

    def test_one_tree_says_one_tree_and_empty_path_entries_are_ignored(self):
        first, second = self.tree("a"), self.tree("b", adds=())
        out_path = os.path.join(self.tmp, "frozen.json")
        code, out, _ = self.run_cli("derive", "--addons-path", ",%s,,%s," % (second, first), "--out", out_path)
        self.assertEqual(code, 0)
        self.assertIn("from 1 tree(s)", out)

    def test_an_empty_path_entry_is_not_the_current_folder(self):
        here = make_tree(os.path.join(self.tmp, "cwd"), core=fx.core_source(version="17.0.7"))
        real = self.tree("real")
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(here)                                   # `cwd/spreadsheet` exists: an empty entry would find it
        path = os.path.join(self.tmp, "frozen.json")
        self.assertEqual(self.run_cli("derive", "--addons-path", ",%s" % real, "--out", path)[0], 0)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.loads(handle.read())["sources"][0]["o_spreadsheet_version"], fx.CORE_VERSION)

    def test_the_same_tree_gives_the_same_bytes_twice(self):
        tree = self.tree("a")
        files = []
        for name in ("one.json", "two.json"):
            path = os.path.join(self.tmp, name)
            self.assertEqual(self.run_cli("derive", "--addons-path", tree, "--out", path)[0], 0)
            with open(path, "rb") as handle:
                files.append(handle.read())
        self.assertEqual(files[0], files[1])

    def test_the_written_file_has_no_path_of_the_machine(self):
        tree = self.tree("a")
        path = os.path.join(self.tmp, "frozen.json")
        self.run_cli("derive", "--addons-path", tree, "--out", path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn(self.tmp, text)
        self.assertNotIn("/", text)

    def test_a_missing_spreadsheet_module_is_exit_2_and_writes_nothing(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        out_path = os.path.join(self.tmp, "frozen.json")
        code, out, err = self.run_cli("derive", "--addons-path", empty, "--out", out_path)
        self.assertCleanError(code, out, err, "the module `spreadsheet` is not in %s" % empty)
        self.assertFalse(os.path.exists(out_path))

    def test_a_failed_derivation_leaves_an_existing_file_untouched(self):
        out_path = write(os.path.join(self.tmp, "frozen.json"), "OLD CONTENT")
        garbage = make_tree(os.path.join(self.tmp, "garbage"), core="const nothing = 1;")
        code, out, err = self.run_cli("derive", "--addons-path", garbage, "--out", out_path)
        self.assertCleanError(code, out, err, "o_spreadsheet.js")
        with open(out_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "OLD CONTENT")

    def test_one_bad_tree_among_good_ones_stops_everything(self):
        good = self.tree("good")
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        out_path = os.path.join(self.tmp, "frozen.json")
        code, out, err = self.run_cli("derive", "--addons-path", good, "--addons-path", empty, "--out", out_path)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_path))

    def test_an_empty_addons_path_is_exit_2(self):
        code, out, err = self.run_cli("derive", "--addons-path", "", "--out", os.path.join(self.tmp, "f.json"))
        self.assertCleanError(code, out, err, "the module `spreadsheet` is not in")

    def test_no_addons_path_is_an_argparse_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            vf.main(["derive", "--out", os.path.join(self.tmp, "f.json")], io.StringIO(), io.StringIO())
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--addons-path", stderr.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "f.json")))

    def test_an_out_folder_that_does_not_exist_is_a_clean_error_and_is_not_created(self):
        tree = self.tree("a")
        target = os.path.join(self.tmp, "no", "such", "frozen.json")
        code, out, err = self.run_cli("derive", "--addons-path", tree, "--out", target)
        self.assertCleanError(code, out, err, os.strerror(errno.ENOENT))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "no")))

    def test_an_out_that_is_a_directory_is_a_clean_error(self):
        tree = self.tree("a")
        code, out, err = self.run_cli("derive", "--addons-path", tree, "--out", self.tmp)
        self.assertCleanError(code, out, err, os.strerror(errno.EISDIR))

    def test_an_existing_out_file_is_replaced(self):
        tree = self.tree("a")
        path = write(os.path.join(self.tmp, "frozen.json"), "OLD CONTENT")
        self.assertEqual(self.run_cli("derive", "--addons-path", tree, "--out", path)[0], 0)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), vf.dump_bytes(vf.derive([[tree]])))

    def test_the_default_out_is_the_real_frozen_file_and_that_is_never_run_here(self):
        args = vf.build_parser().parse_args(["derive", "--addons-path", "x"])
        self.assertEqual(args.out, vf.FROZEN_PATH)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                vf.build_parser().parse_args(["check-drift", "--addons-path", "x", "--out", "y"])


class CheckDriftCommandTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.tree("base")
        self.derived = set(vf.derive([[self.base]])["functions"])

    def frozen(self, names):
        return write_frozen(os.path.join(self.tmp, "frozen.json"), names)

    def test_no_drift_is_exit_0(self):
        code, out, err = self.run_cli("check-drift", "--addons-path", self.base, "--functions", self.frozen(self.derived))
        self.assertEqual((code, out, err), (0, "drift: none\n", ""))

    def test_a_function_that_the_client_no_longer_registers_is_exit_1_with_its_name(self):
        code, out, err = self.run_cli("check-drift", "--addons-path", self.base, "--functions",
                                      self.frozen(self.derived | {"GONE.FN"}))
        self.assertEqual(code, 1)
        self.assertEqual(err, "no longer registered by the client: GONE.FN\n")
        self.assertEqual(out, "drift: 1 removed, 0 added\n")

    def test_a_function_that_is_new_in_the_client_is_exit_1_with_its_name(self):
        code, out, err = self.run_cli("check-drift", "--addons-path", self.base, "--functions",
                                      self.frozen(self.derived - {"ODOO.B"}))
        self.assertEqual(code, 1)
        self.assertEqual(err, "new in the client, not in the frozen list: ODOO.B\n")
        self.assertEqual(out, "drift: 0 removed, 1 added\n")

    def test_removed_come_first_then_added_each_sorted_and_the_counts_are_on_stdout(self):
        frozen = (self.derived - {"ODOO.B", "FUNC2.B"}) | {"ZED", "ALPHA"}
        code, out, err = self.run_cli("check-drift", "--addons-path", self.base, "--functions", self.frozen(frozen))
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), ["no longer registered by the client: ALPHA",
                                            "no longer registered by the client: ZED",
                                            "new in the client, not in the frozen list: FUNC2.B",
                                            "new in the client, not in the frozen list: ODOO.B"])
        self.assertEqual(out, "drift: 2 removed, 2 added\n")

    def test_several_trees_are_compared_through_their_intersection(self):
        base = fx.default_categories()
        wider = self.tree("wider", adds=("ODOO.A", "ODOO.ONLY_WIDER"), categories=dict(base, Cat2=base["Cat2"] + ["EXTRA_W"]))
        narrower = self.tree("narrower", adds=("ODOO.A",))
        common = set(vf.derive([[wider], [narrower]])["functions"])
        self.assertNotIn("EXTRA.W", common)
        code, out, err = self.run_cli("check-drift", "--addons-path", wider, "--addons-path", narrower,
                                      "--functions", self.frozen(common))
        self.assertEqual((code, out, err), (0, "drift: none\n", ""))
        code, out, _ = self.run_cli("check-drift", "--addons-path", wider, "--functions", self.frozen(common))
        self.assertEqual((code, out), (1, "drift: 0 removed, 2 added\n"))                 # EXTRA.W and ODOO.ONLY_WIDER

    def test_the_default_list_is_the_real_frozen_one(self):
        code, out, err = self.run_cli("check-drift", "--addons-path", self.base)
        self.assertEqual(code, 1)
        self.assertIn("no longer registered by the client: SUM\n", err)
        self.assertTrue(out.startswith("drift: %d removed, " % len(vf.load_frozen() - self.derived)), out)

    def test_a_missing_or_broken_frozen_file_is_a_clean_error(self):
        code, out, err = self.run_cli("check-drift", "--addons-path", self.base, "--functions",
                                      os.path.join(self.tmp, "none.json"))
        self.assertCleanError(code, out, err, "cannot read the frozen function list")
        wrong = write(os.path.join(self.tmp, "wrong.json"), '{"schema": 9, "functions": ["SUM"]}')
        self.assertCleanError(*self.run_cli("check-drift", "--addons-path", self.base, "--functions", wrong),
                              "unexpected shape")

    def test_a_frozen_file_that_is_a_json_list_is_a_clean_error(self):
        # RED today: same AttributeError as for `check` (see CheckCommandTests)
        broken = write(os.path.join(self.tmp, "broken.json"), "[]")
        self.assertCleanError(*self.run_cli("check-drift", "--addons-path", self.base, "--functions", broken),
                              "unexpected shape")

    def test_a_missing_spreadsheet_module_is_exit_2(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        code, out, err = self.run_cli("check-drift", "--addons-path", empty, "--functions", self.frozen(self.derived))
        self.assertCleanError(code, out, err, "the module `spreadsheet` is not in")

    def test_check_drift_never_writes_anything(self):
        frozen = self.frozen(self.derived)
        before = sorted(os.listdir(self.tmp))
        self.run_cli("check-drift", "--addons-path", self.base, "--functions", frozen)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_a_derived_file_has_no_drift_against_its_own_tree(self):
        path = os.path.join(self.tmp, "derived.json")
        self.assertEqual(self.run_cli("derive", "--addons-path", self.base, "--out", path)[0], 0)
        self.assertEqual(self.run_cli("check-drift", "--addons-path", self.base, "--functions", path),
                         (0, "drift: none\n", ""))


class LastResortHandlerTests(CliTestCase):
    def test_an_unexpected_exception_is_one_line_without_traceback_or_detail(self):
        with mock.patch.object(vf, "run", side_effect=ZeroDivisionError("secret detail")):
            code, out, err = self.run_cli("check", "x.json")
        self.assertEqual((code, out, err), (1, "", "error: unexpected ZeroDivisionError\n"))

    def test_an_os_error_without_a_message_is_a_generic_write_error(self):
        with mock.patch.object(vf, "run", side_effect=OSError("detail")):
            code, out, err = self.run_cli("check", "x.json")
        self.assertEqual((code, out, err), (2, "", "error: cannot write the file\n"))

    def test_an_os_error_with_a_message_prints_the_message(self):
        with mock.patch.object(vf, "run", side_effect=PermissionError(errno.EACCES, os.strerror(errno.EACCES))):
            code, out, err = self.run_cli("check", "x.json")
        self.assertEqual((code, out, err), (2, "", "error: %s\n" % os.strerror(errno.EACCES)))

    def test_a_validate_error_is_exit_2_with_its_message(self):
        with mock.patch.object(vf, "run", side_effect=vf.ValidateError("a problem")):
            self.assertEqual(self.run_cli("check", "x.json"), (2, "", "error: a problem\n"))

    def test_an_unexpected_failure_inside_the_real_flow_is_caught(self):
        path = self.dashboard_file({"A1": "=SUM(1)"})
        with mock.patch.object(vf, "unknown_function_problems", side_effect=KeyError("k")):
            self.assertEqual(self.run_cli("check", path), (1, "", "error: unexpected KeyError\n"))


class StandardStreamsAndScriptTests(IsolatedTestCase):
    def test_main_writes_to_the_process_streams_by_default(self):
        path = write_json(os.path.join(self.tmp, "d.json"), dashboard({"A1": "=SUM(1)"}))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = vf.main(["check", path])
        self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (0, "functions: ok\n", ""))

    def run_script(self, *args):
        return subprocess.run([sys.executable, "-I", SCRIPT, *args], capture_output=True, text=True, timeout=60,
                              cwd=self.tmp)

    def test_the_script_runs_as_a_program_with_the_exit_code_of_main(self):
        good = write_json(os.path.join(self.tmp, "good.json"), dashboard({"A1": "=SUM(1)"}))
        bad = write_json(os.path.join(self.tmp, "bad.json"), dashboard({"A1": "=N(1)"}))
        done = self.run_script("check", good)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "functions: ok\n", ""))
        done = self.run_script("check", bad)
        self.assertEqual((done.returncode, done.stdout), (1, "functions: 1 unknown\n"))
        self.assertIn("function N()", done.stderr)
        done = self.run_script("check", os.path.join(self.tmp, "missing.json"))
        self.assertEqual(done.returncode, 2)
        self.assertTrue(done.stderr.startswith("error: "))
        self.assertNotIn("Traceback", done.stderr)

    def test_help_lists_the_three_commands_and_a_bad_command_is_exit_2(self):
        done = self.run_script("--help")
        self.assertEqual(done.returncode, 0)
        for command in ("check", "derive", "check-drift"):
            self.assertIn(command, done.stdout)
        self.assertEqual(self.run_script().returncode, 2)
        self.assertEqual(self.run_script("nonsense").returncode, 2)
        self.assertEqual(self.run_script("check").returncode, 2)            # the file argument is required

    def test_the_module_is_standard_library_only(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"odoo", "openerp"}, imported)
        stdlib = getattr(sys, "stdlib_module_names", {"argparse", "functools", "hashlib", "json", "math", "os", "re", "sys"})
        self.assertFalse(imported - set(stdlib), imported)

    def test_importing_the_module_loads_no_odoo(self):
        code = "import sys; sys.path.insert(0, %r); import validate_formulas; print('odoo' in sys.modules)" % os.path.dirname(SCRIPT)
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "False"))


if __name__ == "__main__":
    unittest.main()
