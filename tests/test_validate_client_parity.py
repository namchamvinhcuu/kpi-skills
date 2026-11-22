"""The function-name guard of `scripts/validate_formulas.py` against the REAL client parser (o_spreadsheet.js, run in node).

A guard that reads formulas differently from the client is bypassable: a call the guard cannot see still runs in the
browser of every viewer. These tests feed the same formulas to both readers and compare. They need `node` and the
`o_spreadsheet.js` of an Odoo 17 tree (the plugin repo holds neither): the tests are SKIPPED when one of the two is
missing, and for no other reason. Where to look for the file: the environment variable KPI_O_SPREADSHEET_JS (the file
itself), else KPI_ODOO_ADDONS_PATH (comma separated addons folders, like the drift test), else a sibling checkout
`../../odoo17/odoo/addons` of this repository.

The node side is `tests/client_function_calls.js` (read-only, no network, one call per run with the whole corpus).
"""
import collections
import itertools
import json
import os
import shutil
import subprocess
import unittest

from helpers import REPO

import validate_formulas as vf  # noqa: E402

try:
    import resource
except ImportError:                                     # not POSIX
    resource = None

FEFF, NBSP = "\N{ZERO WIDTH NO-BREAK SPACE}", "\N{NO-BREAK SPACE}"
CLIENT_JS_PARTS = ("spreadsheet",) + vf.O_SPREADSHEET_JS
HELPER = str(REPO / "tests" / "client_function_calls.js")
MEMORY_LIMIT = 2 * 1024 ** 3
TIMEOUT = 120


def find_client_js():
    explicit = os.environ.get("KPI_O_SPREADSHEET_JS", "")
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    folders = [p for p in os.environ.get("KPI_ODOO_ADDONS_PATH", "").split(",") if p]
    folders.append(str(REPO.parent.parent / "odoo17" / "odoo" / "addons"))
    for folder in folders:
        candidate = os.path.join(folder, *CLIENT_JS_PARTS)
        if os.path.isfile(candidate):
            return candidate
    return ""


NODE = shutil.which("node")
CLIENT_JS = find_client_js()
NEEDS = unittest.skipUnless(NODE and CLIENT_JS, "needs node and the o_spreadsheet.js of an Odoo 17 tree (%s; %s)" % (
    "node found" if NODE else "node missing", "client file found" if CLIENT_JS else "client file missing"))


def _cap_memory():
    if resource is not None:
        resource.setrlimit(resource.RLIMIT_DATA, (MEMORY_LIMIT, MEMORY_LIMIT))


def client_answers(formulas):
    """One answer per formula: {"calls": [function names the client parser sees]} or {"error": ...}."""
    done = subprocess.run([NODE, HELPER, CLIENT_JS], input=json.dumps(formulas), capture_output=True, text=True,
                          encoding="utf-8", timeout=TIMEOUT, preexec_fn=_cap_memory if resource is not None else None)
    if done.returncode != 0:
        raise AssertionError("the node helper failed (rc %s): %s" % (done.returncode, done.stderr[:300]))
    answers = json.loads(done.stdout)
    if len(answers) != len(formulas):
        raise AssertionError("the node helper answered %d of %d formulas" % (len(answers), len(formulas)))
    return answers


def doc_of(formula):
    return {"sheets": [{"cells": {"A1": {"content": formula}}}]}


# the five bypasses of the combined security review (all REAL calls in the client) and the shapes that must stay clean
BYPASSES = {
    "two backslashes then a quote": ('="A\\\\"B"&EVIL(1)&"C"', "EVIL"),
    "U+FEFF before the parenthesis": ("=EVIL" + FEFF + "(1)", "EVIL"),
    "a quote inside a sheet name": ("='a\"b'!A1+EVIL(1)+\"c\"", "EVIL"),
    "dollar prefix": ("=$SUM(1)", "$SUM"),
    "accented prefix": ("=ÉSUM(1)", "ÉSUM"),
}
CLEAN = ('="EVIL(1)"', "=SUM(1,2)", '=IF(A1>0,ODOO.LIST(1,1,"x")+0,0)')

# pieces of formulas the corpus joins with `&`: literals with every quote / backslash shape, sheet references, the
# call spellings of the review and a few that must not be calls
PIECES = (
    '"x"', '"a\\"b"', '"\\\\"', '"\\\\\\"', '"', "'s'!A1", "'a\"b'!A1", "A1", "1", "EVIL(1)", "SUM(1)", "$SUM(1)",
    "\N{LATIN CAPITAL LETTER E WITH ACUTE}SUM(1)", "EVIL" + FEFF + "(1)", "EVIL (1)", "EVIL" + NBSP + "(1)", "_T(1)",
    "ODOO.LIST(1,1,\"x\")", "A.B(1)", "(1)",
    "1SUM(1)", ".SUM(1)", "x$SUM(1)", "!", "'",
)
TOKENS = ('"', "\\", "'", "!", "$", "\N{LATIN CAPITAL LETTER E WITH ACUTE}", FEFF, " ", "EVIL(", "SUM(", ")", ",", "+", "A1",
          "x", "&", "_")


def corpus():
    formulas = [f for f, _ in BYPASSES.values()] + list(CLEAN)
    formulas += ["=" + "&".join(parts) for size in (1, 2, 3) for parts in itertools.product(PIECES, repeat=size)]
    formulas += ["=" + "".join(parts) for size in (1, 2, 3, 4) for parts in itertools.product(TOKENS, repeat=size)]
    return list(dict.fromkeys(formulas))


@NEEDS
class ClientPremiseTests(unittest.TestCase):
    """What the client really does with the formulas of the review (so the guard tests below are not vacuous)."""

    def test_the_client_sees_a_call_in_every_bypass_and_none_in_the_clean_shapes(self):
        formulas = [f for f, _ in BYPASSES.values()] + list(CLEAN)
        answers = client_answers(formulas)
        for (label, (formula, name)), answer in zip(BYPASSES.items(), answers):
            with self.subTest(label=label):
                self.assertEqual(answer, {"calls": [name]})
        self.assertEqual(answers[len(BYPASSES):],
                         [{"calls": []}, {"calls": ["SUM"]}, {"calls": ["IF", "ODOO.LIST"]}])

    def test_the_client_closes_a_string_at_a_quote_not_preceded_by_a_backslash(self):
        # a quote preceded by ONE backslash does not close the string, whatever stands before that backslash
        answers = client_answers(['="x\\"&FOO(1)"', '="a\\\\"b"&FOO(1)&"c"', '="a"&FOO(1)', '="\\\\"'])
        self.assertEqual(answers, [{"calls": []}, {"calls": ["FOO"]}, {"calls": ["FOO"]}, {"calls": []}])


@NEEDS
class GuardAgainstTheClientTests(unittest.TestCase):
    KNOWN = {"SUM", "IF", "ODOO.LIST", "_T"}

    def test_every_bypass_is_reported_and_the_clean_shapes_stay_clean(self):
        for label, (formula, _) in BYPASSES.items():
            with self.subTest(label=label):
                self.assertTrue(vf.unknown_function_problems(doc_of(formula), self.KNOWN), formula)
                self.assertTrue(vf.unknown_function_problems(doc_of(formula), vf.load_frozen()), formula)
        for formula in CLEAN:
            with self.subTest(formula=formula):
                self.assertEqual(vf.unknown_function_problems(doc_of(formula), vf.load_frozen()), [])

    def test_a_name_that_starts_with_a_digit_or_a_dot_is_a_name_of_its_own(self):
        # the client reads `1SUM(` and `.SUM(` as calls of functions called `1SUM` and `.SUM` (not registered)
        for formula in ("=1SUM(1)", "=.SUM(1)", "=1_EVIL(1)", "=.$SUM(1)", "=1x(1)"):
            answer = client_answers([formula])[0]
            with self.subTest(formula=formula):
                self.assertEqual(len(answer.get("calls", [])), 1, answer)
                self.assertTrue(vf.unknown_function_problems(doc_of(formula), self.KNOWN),
                                "the client calls %r, the guard reports nothing" % answer["calls"])

    def test_the_guard_and_the_client_agree_on_a_corpus_of_formulas(self):
        formulas = corpus()
        answers = client_answers(formulas)
        compared = with_calls = unreadable_with_calls = 0
        divergences = []
        for formula, answer in zip(formulas, answers):
            if "calls" not in answer:
                continue                                     # the client refuses the formula: nothing to compare
            real = collections.Counter(name.upper() for name in answer["calls"])
            problems = vf.unknown_function_problems(doc_of(formula), self.KNOWN)
            if vf._blank_literals(formula)[1]:               # the guard says "cannot read it": fail closed
                unreadable_with_calls += bool(real)
                if not problems:
                    divergences.append((formula, "unreadable but reported nothing"))
                continue
            compared += 1
            with_calls += bool(real)
            seen = collections.Counter({name: len(refs) for name, refs in
                                        vf.formula_function_names(doc_of(formula)).items()})
            if seen != real:
                divergences.append((formula, "client %s, guard %s" % (dict(real), dict(seen))))
            unknown = sorted(name for name in real if name not in {k.upper() for k in self.KNOWN})
            if unknown and not problems:
                divergences.append((formula, "client calls %s, the guard is clean" % unknown))
        # not vacuous: thousands of formulas read by both, hundreds with a call, and the unreadable ones met too
        self.assertGreater(compared, 2000)
        self.assertGreater(with_calls, 600)
        self.assertGreater(unreadable_with_calls, 100)
        self.assertEqual(len(divergences), 0, "%d divergences between the guard and the client, first: %r"
                         % (len(divergences), divergences[:6]))


if __name__ == "__main__":
    unittest.main()
