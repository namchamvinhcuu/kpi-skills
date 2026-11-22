"""Static checks over the plugin sources (no network, nothing is run): Python 3.8 syntax and the imports of the scripts.

Plan kpi-skill-improvements R5: the plugin must run on Python 3.8 (stdlib only), but only 3.10 / 3.12 are installed
here, so the 3.8 rules are checked on the syntax tree: `ast.parse(..., feature_version=(3, 8))` plus a scan for what that
parse still accepts (`removeprefix`, `X | Y` and `list[int]` annotations, `import zoneinfo`). The scanner is tested on
sources that break each rule, so a scanner that finds nothing cannot pass for a clean tree.
"""
import ast
import glob
import os
import unittest

from helpers import REPO
from test_bind_cli import is_stdlib_module

SCRIPTS = sorted(glob.glob(os.path.join(str(REPO), "scripts", "*.py")))
TESTS = sorted(glob.glob(os.path.join(str(REPO), "tests", "*.py")))
BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}        # subscripted at run time only from 3.9


def py38_problems(source):
    """The reasons `source` would not run on Python 3.8 ([] when it would), found on its syntax tree."""
    try:
        tree = ast.parse(source, feature_version=(3, 8))
    except SyntaxError as exc:
        return ["syntax: %s" % exc.msg]
    problems, annotations = [], []
    match_class = getattr(ast, "Match", None)
    for node in ast.walk(tree):
        if match_class is not None and isinstance(node, match_class):
            problems.append("match statement (line %d)" % node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in ("removeprefix", "removesuffix"):
            problems.append("%s (line %d)" % (node.attr, node.lineno))
        elif isinstance(node, ast.Import) and any(a.name.split(".")[0] == "zoneinfo" for a in node.names):
            problems.append("import zoneinfo (line %d)" % node.lineno)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "zoneinfo":
            problems.append("from zoneinfo (line %d)" % node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every = arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [arguments.vararg, arguments.kwarg]
            annotations += [a.annotation for a in every if a is not None] + [node.returns]
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    for annotation in annotations:
        for part in ast.walk(annotation) if annotation is not None else ():
            if isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr):
                problems.append("X | Y annotation (line %d)" % part.lineno)
            elif (isinstance(part, ast.Subscript) and isinstance(part.value, ast.Name)
                  and part.value.id in BUILTIN_GENERICS):
                problems.append("%s[...] annotation (line %d)" % (part.value.id, part.lineno))
    return problems


def imported_names(source):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    return names


class Python38ScannerTests(unittest.TestCase):
    """The scanner finds every rule it claims to check, and nothing in plain 3.8 code."""

    def test_each_rule_is_found(self):
        for label, source, expected in (
                ("match", "def f(x):\n    match x:\n        case 1:\n            return 2\n", "syntax"),
                ("removeprefix", "name = 'abc'.removeprefix('a')\n", "removeprefix"),
                ("removesuffix", "name = 'abc'.removesuffix('c')\n", "removesuffix"),
                ("union annotation", "def f(a: int | None):\n    return a\n", "X | Y annotation"),
                ("union in a return", "def f() -> str | None:\n    return None\n", "X | Y annotation"),
                ("union in a variable", "x: int | None = None\n", "X | Y annotation"),
                ("union nested", "def f(a: 'x', b: dict[str, int | None]):\n    return a\n", "X | Y annotation"),
                ("builtin generic", "def f(a: list[int]):\n    return a\n", "list[...] annotation"),
                ("builtin generic in a variable", "x: dict[str, int] = {}\n", "dict[...] annotation"),
                ("import zoneinfo", "import zoneinfo\n", "import zoneinfo"),
                ("import zoneinfo as", "import zoneinfo as zi\n", "import zoneinfo"),
                ("from zoneinfo", "from zoneinfo import ZoneInfo\n", "from zoneinfo"),
                ("keyword-only union", "def f(*, a: int | None = None):\n    return a\n", "X | Y annotation"),
                ("vararg union", "def f(*a: int | str):\n    return a\n", "X | Y annotation"),
                ("walrus is fine in 3.8 but a 3.10 match is not", "match = 1\nif (n := 3) > 2:\n    pass\nmatch x:\n    case _:\n        pass\n", "syntax")):
            with self.subTest(case=label):
                problems = py38_problems(source)
                self.assertTrue(any(expected in problem for problem in problems), (expected, problems))

    def test_a_plain_python_38_source_is_clean(self):
        source = ("import json\nfrom typing import Dict, List, Optional\n\n\n"
                  "def f(a: Optional[int] = None, *rest: str, **kw: Dict[str, int]) -> List[int]:\n"
                  "    match = 3\n    if (n := match) > 2:\n        return [n]\n"
                  "    return list(rest)\n\n\nx: Dict[str, int] = {}\ntext = 'removeprefix zoneinfo'\nname = 'a'[1:]\n")
        self.assertEqual(py38_problems(source), [])

    def test_a_word_in_a_string_or_a_comment_is_not_a_finding(self):
        self.assertEqual(py38_problems("# import zoneinfo; x.removeprefix\nnote = 'match x: removeprefix | zoneinfo'\n"),
                         [])

    def test_a_syntax_error_is_a_finding_not_a_crash(self):
        self.assertTrue(py38_problems("def (:\n"))

    def test_the_parse_really_runs_at_the_3_8_grammar(self):
        # a construct of Python 3.10 must be refused by this very call, or `feature_version` would not be checked
        with self.assertRaises(SyntaxError):
            ast.parse("match x:\n    case 1:\n        pass\n", feature_version=(3, 8))


class Python38SourcesTests(unittest.TestCase):
    def test_there_are_scripts_and_tests_to_scan(self):
        names = {os.path.basename(path) for path in SCRIPTS}
        self.assertTrue({"bind_kpi.py", "intake_xlsx.py", "odoo_rpc.py", "validate_formulas.py", "publish_dashboard.py",
                         "setup_instance.py", "reconcile_kpi.py", "scan_excel.py"} <= names, names)
        self.assertGreater(len(TESTS), 20)

    def test_every_script_parses_and_runs_on_python_38(self):
        for path in SCRIPTS:
            with self.subTest(script=os.path.basename(path)):
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(py38_problems(handle.read()), [])

    def test_every_test_and_fixture_file_is_python_38_too(self):
        for path in TESTS:
            with self.subTest(file=os.path.basename(path)):
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(py38_problems(handle.read()), [])


class ReconcileImportsTests(unittest.TestCase):
    def test_reconcile_imports_only_the_standard_library_and_four_sibling_scripts(self):
        with open(os.path.join(str(REPO), "scripts", "reconcile_kpi.py"), encoding="utf-8") as handle:
            names = imported_names(handle.read())
        ours = {name for name in names if not is_stdlib_module(name)}
        self.assertEqual(ours, {"bind_kpi", "intake_xlsx", "odoo_rpc", "validate_formulas"})
        for third_party in ("requests", "urllib3", "yaml", "dateutil", "pytz", "zoneinfo", "numpy", "pandas", "openpyxl"):
            self.assertNotIn(third_party, names)

    def test_the_four_siblings_are_the_scripts_of_this_folder(self):
        folder = {os.path.splitext(os.path.basename(path))[0] for path in SCRIPTS}
        self.assertTrue({"bind_kpi", "intake_xlsx", "odoo_rpc", "validate_formulas"} <= folder)

    def test_the_imports_of_the_tests_helper_agree_with_the_scanner(self):
        self.assertEqual(imported_names("import os\nfrom json import loads\nimport a.b\nfrom c.d import e\n"),
                         {"os", "json", "a", "c"})


class RpcImportsTests(unittest.TestCase):
    """Plan kpi-skill-improvements step 4: `init-config` adds `getpass`, `io` and `warnings` (standard library) and no
    terminal-driving module: the plugin never allocates a pseudo terminal itself."""

    def names(self):
        with open(os.path.join(str(REPO), "scripts", "odoo_rpc.py"), encoding="utf-8") as handle:
            return imported_names(handle.read())

    def test_odoo_rpc_imports_only_the_standard_library_and_uses_the_three_new_modules(self):
        names = self.names()
        # DELIBERATE (plan kpi-multi-project step 3, AC5): odoo_rpc.py imports its stdlib-only sibling `kpi_catalogs`
        # (the catalog list for the `project` of the config); it must still import nothing else of this plugin
        self.assertEqual({name for name in names if not is_stdlib_module(name)}, {"kpi_catalogs"})
        self.assertTrue({"getpass", "io", "warnings"} <= names, names)

    def test_odoo_rpc_does_not_drive_a_terminal_or_a_spawned_shell_for_the_key(self):
        for module in ("pty", "termios", "tty", "pexpect", "curses", "readline", "keyring"):
            self.assertNotIn(module, self.names())


NETWORK_MODULES = ("odoo_rpc","socket", "ssl", "urllib", "http", "xmlrpc", "ftplib", "smtplib", "telnetlib", "poplib",
                   "imaplib", "asyncio", "selectors", "requests", "urllib3", "httpx", "aiohttp", "subprocess")


class ScanImportsTests(unittest.TestCase):
    """Plan kpi-skill-improvements B2: scan_excel.py is offline and stdlib only; it uses two sibling scripts."""

    def names(self):
        with open(os.path.join(str(REPO), "scripts", "scan_excel.py"), encoding="utf-8") as handle:
            return imported_names(handle.read())

    def test_scan_imports_only_the_standard_library_and_two_sibling_scripts(self):
        names = self.names()
        self.assertEqual({name for name in names if not is_stdlib_module(name)}, {"bind_kpi", "intake_xlsx"})
        for third_party in ("requests", "urllib3", "yaml", "dateutil", "pytz", "zoneinfo", "numpy", "pandas", "openpyxl",
                            "unidecode", "regex"):
            self.assertNotIn(third_party, names)

    def test_scan_imports_no_network_module_and_not_the_rpc_client(self):
        names = self.names()
        for module in NETWORK_MODULES:
            self.assertNotIn(module, names)

    def test_the_standard_library_part_is_the_documented_one(self):
        self.assertEqual({name for name in self.names() if is_stdlib_module(name)},
                         {"argparse", "hashlib", "os", "re", "sys"})

    def test_the_network_denylist_catches_a_scan_that_would_import_one(self):
        for line in ("import odoo_rpc", "import socket", "import urllib.request", "from http import client",
                     "from urllib.request import urlopen", "import requests"):
            with self.subTest(line=line):
                self.assertTrue(imported_names(line + "\n") & set(NETWORK_MODULES))
        self.assertFalse(imported_names("import argparse\nimport hashlib\n") & set(NETWORK_MODULES))

    def test_the_imports_scanner_sees_an_import_inside_a_function_too(self):
        self.assertEqual(imported_names("def f():\n    import socket\n    from odoo_rpc import x\n"),
                         {"socket", "odoo_rpc"})


if __name__ == "__main__":
    unittest.main()
