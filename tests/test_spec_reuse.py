"""Plan kpi-skill-improvements B4 / AC6b: reusing a spec when the Excel comes again.

The convention of the skill: the work folder keeps `<excel name>.spec.json`; when the customer sends the Excel again the
user runs the intake again, copies the spec to `<excel name>.v2.spec.json` with `baseline_sha256` replaced by the new
intake's sha256, then binds. The binder checks that hash BEFORE anything else, so an old spec stops at `sha256 differs`
until it is refreshed on purpose, and after the refresh the `labels` guard stops a spec whose rows moved (`label shift`).
Everything here runs `bind_kpi.main` over synthetic files built by `bind_fixtures` (no network, no real file).
"""
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import bind_fixtures as bf
from helpers import REPO
from test_bind_cli import BindCliCase
from test_repo_hygiene import WORKFLOW_OUTPUT_PATTERNS, gitignore_patterns, is_ignored, workflow_outputs

import intake_xlsx

LABELS = {"A5": "Row label one", "A6": "Row label two"}
OLD_SPEC_NAME, REFRESHED_SPEC_NAME = "report.spec.json", "report.v2.spec.json"
REF = re.compile(r"(?<![A-Za-z0-9_.])(\$?[A-Z]{1,3}\$?)([0-9]+)(?![0-9A-Za-z(])")
REF_KEY = re.compile(r"([A-Z]+)([0-9]+)")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def shift_formula(text, at):
    """`text` with every row number >= `at` moved down by one (a row was inserted before row `at`)."""
    return REF.sub(lambda m: "%s%d" % (m.group(1), int(m.group(2)) + (1 if int(m.group(2)) >= at else 0)), text)


def baseline_with_inserted_row(at=5, text="Inserted row"):
    """The small baseline after the customer inserted one row before row `at`: every row from `at` down moves by one,
    a new text label sits in A<at>; formulas and merges are rewritten like a spreadsheet would."""
    cells = {}
    for ref, cell in bf.small_cells().items():
        col, row = REF_KEY.fullmatch(ref).groups()
        cell = dict(cell)
        if isinstance(cell.get("content"), str) and cell["content"].startswith("="):
            cell["content"] = shift_formula(cell["content"], at)
        cells["%s%d" % (col, int(row) + (1 if int(row) >= at else 0))] = cell
    cells["A%d" % at] = {"content": text, "style": 1}
    merges = [shift_formula(merge, at) for merge in ["A1:C1", "P8:P9"]]
    return bf.make_baseline(cells=cells, merges=merges, row_number=bf.AREA_ROWS + 1)


def shifted_bindings():
    """The bindings of `bf.small_bindings()` after the same row insertion (rows 5 and 6 are now 6 and 7)."""
    return [bf.plain("D6:F6", "A", zero="dash"), bf.plain("D7:F7", "B")]


class SpecReuseCase(BindCliCase):
    """`BindCliCase` plus the reuse procedure of the skill, on files in a work folder."""

    def setUp(self):
        super().setUp()
        self.old_spec = bf.make_spec(labels=dict(LABELS), baseline_sha256=sha(self.baseline_bytes))
        self.old_spec_path = os.path.join(self.work, OLD_SPEC_NAME)
        self.v2_spec_path = os.path.join(self.work, REFRESHED_SPEC_NAME)
        self.write(self.old_spec_path, json.dumps(self.old_spec, indent=1))
        self.spec_path = self.old_spec_path

    def argv_for(self, spec_path):
        return ["--baseline", self.baseline_path, "--spec", spec_path, "--out", self.out_path,
                "--catalog", self.catalog_path]

    def put_baseline(self, baseline):
        data = intake_xlsx.dump_bytes(baseline)
        self.write(self.baseline_path, data)
        return data

    def refresh(self, new_baseline_data, **changes):
        """The documented refresh: the old spec copied to the v2 name with ONLY `baseline_sha256` replaced (and the
        optional `changes`, for a spec whose cells the user fixed together with Claude)."""
        with open(self.old_spec_path, encoding="utf-8") as handle:
            spec = json.load(handle)
        spec["baseline_sha256"] = sha(new_baseline_data)
        spec.update(changes)
        self.write(self.v2_spec_path, json.dumps(spec, indent=1))
        return spec

    def assert_nothing_written(self):
        self.assertFalse(os.path.exists(self.out_path))
        self.assertEqual(self.leftovers(), [])


# ---------------------------------------------------------------- the fixture itself

class FixtureTests(SpecReuseCase):
    """The synthetic Excel really changes the way the tests below say it does (a fixture that moved nothing would turn
    every test of this file into a tautology)."""

    def test_the_old_spec_binds_the_old_baseline(self):
        rc, out, err = self.run_cli(self.argv_for(self.old_spec_path))
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(os.path.exists(self.out_path))

    def test_the_inserted_row_moves_the_labels_and_changes_the_bytes(self):
        old, new = bf.small_baseline(), baseline_with_inserted_row()
        old_cells, new_cells = old["sheets"][0]["cells"], new["sheets"][0]["cells"]
        self.assertEqual((old_cells["A5"]["content"], old_cells["A6"]["content"]), ("Row label one", "Row label two"))
        self.assertEqual(new_cells["A5"]["content"], "Inserted row")
        self.assertEqual((new_cells["A6"]["content"], new_cells["A7"]["content"]), ("Row label one", "Row label two"))
        self.assertEqual(new["sheets"][0]["rowNumber"], old["sheets"][0]["rowNumber"] + 1)
        self.assertNotEqual(sha(intake_xlsx.dump_bytes(old)), sha(intake_xlsx.dump_bytes(new)))
        self.assertIn("=D7*2", new_cells["D8"]["content"])          # the doubling formula follows its row

    def test_the_old_spec_names_the_labels_it_would_stop_at(self):
        self.assertEqual(self.old_spec["labels"], LABELS)
        self.assertEqual(self.old_spec["baseline_sha256"], sha(self.baseline_bytes))


# ---------------------------------------------------------------- (a) the sha check comes first

class StaleSpecIsRefusedFirstTests(SpecReuseCase):
    def test_the_old_spec_against_a_baseline_with_an_inserted_row_stops_at_sha256_differs(self):
        # Regression test for AC6b: the labels would ALSO shift here, and the message must still be the sha one
        self.put_baseline(baseline_with_inserted_row())
        err = self.expect_error(self.argv_for(self.old_spec_path), rc=2, contains=("sha256 differs",
                                                                                   "run the intake again"))
        self.assertNotIn("label shift", err)
        self.assert_nothing_written()

    def test_the_sha_check_runs_before_the_label_guard_even_for_an_unmoved_baseline(self):
        # bytes changed, rows did not move: the old spec still stops at the sha, not at anything else
        self.put_baseline(bf.edit(bf.small_baseline(), {"H9": {"content": "A note"}}))
        err = self.expect_error(self.argv_for(self.old_spec_path), rc=2, contains="sha256 differs")
        self.assertNotIn("label shift", err)
        self.assert_nothing_written()

    def test_a_stale_spec_leaves_an_existing_output_untouched(self):
        self.run_cli(self.argv_for(self.old_spec_path))
        before = self.read()
        self.put_baseline(baseline_with_inserted_row())
        self.expect_error(self.argv_for(self.old_spec_path) + ["--force"], rc=2, contains="sha256 differs")
        self.assertEqual(self.read(), before)
        self.assertEqual(self.leftovers(), [])

    def test_the_message_is_one_line_with_no_traceback(self):
        self.put_baseline(baseline_with_inserted_row())
        rc, out, err = self.run_cli(self.argv_for(self.old_spec_path))
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("Traceback", err)


# ---------------------------------------------------------------- (b) refreshed hash + moved rows = label shift

class RefreshedHashDoesNotHideAShiftTests(SpecReuseCase):
    def test_the_refreshed_spec_on_a_baseline_with_an_inserted_row_stops_at_label_shift(self):
        # Regression test for AC6b: the user refreshed the hash, the rows moved: the label guard is the net
        data = self.put_baseline(baseline_with_inserted_row())
        refreshed = self.refresh(data)
        self.assertEqual(refreshed["baseline_sha256"], sha(data))            # the first guard no longer fires
        err = self.expect_error(self.argv_for(self.v2_spec_path), rc=2, contains="label shift")
        self.assertNotIn("sha256 differs", err)
        self.assertIn("A5", err)                                              # it names the first cell that moved
        self.assertIn("Row label one", err)                                   # what the spec expected there
        self.assertIn("Inserted row", err)                                    # what the Excel holds now
        self.assert_nothing_written()

    def test_it_names_the_cell_of_the_label_that_moved_not_a_later_one(self):
        # a spec that only anchors the SECOND label names that one: the message follows the spec's own cells
        data = self.put_baseline(baseline_with_inserted_row())
        self.refresh(data, labels={"A6": "Row label two"})
        err = self.expect_error(self.argv_for(self.v2_spec_path), rc=2, contains="label shift")
        self.assertIn("A6", err)
        self.assertIn("Row label one", err)                                   # A6 now holds the first label
        self.assert_nothing_written()

    def test_refreshing_the_hash_and_fixing_the_cells_together_binds(self):
        # the legitimate path (the user decided with Claude): the cells and the labels follow the moved rows
        data = self.put_baseline(baseline_with_inserted_row())
        self.refresh(data, labels={"A6": "Row label one", "A7": "Row label two"}, bindings=shifted_bindings())
        rc, out, err = self.run_cli(self.argv_for(self.v2_spec_path))
        self.assertEqual((rc, err), (0, ""))
        document = json.loads(self.read())
        cells = document["sheets"][0]["cells"]
        self.assertTrue(cells["D6"]["content"].startswith("="), cells["D6"])             # the `-` wrapper of row 6
        self.assertIn("ODOO.LIST", cells["D7"]["content"])                               # the plain list of row 7

    def test_the_label_guard_is_what_stopped_it_not_a_second_problem(self):
        # control: the same refreshed spec WITHOUT the labels block does not stop on a label (it binds the wrong rows,
        # which is exactly why the labels exist)
        data = self.put_baseline(baseline_with_inserted_row())
        self.refresh(data)
        with open(self.v2_spec_path, encoding="utf-8") as handle:
            spec = json.load(handle)
        del spec["labels"]
        self.write(self.v2_spec_path, json.dumps(spec))
        err = self.run_cli(self.argv_for(self.v2_spec_path))[2]
        self.assertNotIn("label shift", err)
        self.assertNotIn("sha256 differs", err)


# ---------------------------------------------------------------- (c) unchanged baseline

class UnchangedBaselineTests(SpecReuseCase):
    def test_the_refreshed_spec_on_an_unchanged_baseline_binds_to_identical_bytes_twice(self):
        spec = self.refresh(self.baseline_bytes)
        self.assertEqual(spec["baseline_sha256"], self.old_spec["baseline_sha256"])   # same value rewritten
        rc, first_out, err = self.run_cli(self.argv_for(self.v2_spec_path))
        self.assertEqual((rc, err), (0, ""))
        first = self.read()
        rc, second_out, err = self.run_cli(self.argv_for(self.v2_spec_path) + ["--force"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(first, self.read())
        self.assertEqual(json.loads(first_out)["sha256"], json.loads(second_out)["sha256"])
        self.assertEqual(json.loads(first_out)["sha256"], sha(first))

    def test_the_refreshed_copy_gives_the_bytes_of_the_old_spec(self):
        self.refresh(self.baseline_bytes)
        self.run_cli(self.argv_for(self.old_spec_path))
        from_old = self.read()
        self.run_cli(self.argv_for(self.v2_spec_path) + ["--force"])
        self.assertEqual(self.read(), from_old)

    def test_the_refresh_changes_nothing_but_the_hash(self):
        data = self.put_baseline(bf.edit(bf.small_baseline(), {"H9": {"content": "A note"}}))
        refreshed = self.refresh(data)
        expected = copy.deepcopy(self.old_spec)
        expected["baseline_sha256"] = sha(data)
        self.assertEqual(refreshed, expected)
        with open(self.old_spec_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), self.old_spec)               # the old file is not touched

    def test_no_other_file_appears_next_to_the_output(self):
        self.refresh(self.baseline_bytes)
        self.run_cli(self.argv_for(self.v2_spec_path))
        self.assertEqual(self.listing(), ["baseline.json", "catalog.json", "dashboard.json", OLD_SPEC_NAME,
                                          REFRESHED_SPEC_NAME, "spec.json"])


# ---------------------------------------------------------------- (d) bytes changed, rows did not move

class ChangedBytesUnmovedRowsTests(SpecReuseCase):
    def test_a_baseline_changed_in_an_unrelated_cell_binds_after_the_refresh(self):
        data = self.put_baseline(bf.edit(bf.small_baseline(), {"H9": {"content": "A note", "style": 1}}))
        self.assertNotEqual(sha(data), self.old_spec["baseline_sha256"])
        self.expect_error(self.argv_for(self.old_spec_path), rc=2, contains="sha256 differs")
        self.refresh(data)
        rc, out, err = self.run_cli(self.argv_for(self.v2_spec_path))
        self.assertEqual((rc, err), (0, ""))
        document = json.loads(self.read())
        cells = document["sheets"][0]["cells"]
        self.assertEqual(cells["H9"]["content"], "A note")                    # the Excel's own text is carried over
        self.assertTrue(cells["D5"]["content"].startswith("="), cells["D5"])  # the bound cells are formulas again
        self.assertIn("ODOO.LIST", cells["D6"]["content"])

    def test_the_new_output_differs_from_the_old_one_only_because_the_excel_did(self):
        self.run_cli(self.argv_for(self.old_spec_path))
        old_out = json.loads(self.read())
        data = self.put_baseline(bf.edit(bf.small_baseline(), {"H9": {"content": "A note", "style": 1}}))
        self.refresh(data)
        self.run_cli(self.argv_for(self.v2_spec_path) + ["--force"])
        new_out = json.loads(self.read())
        old_cells, new_cells = old_out["sheets"][0]["cells"], new_out["sheets"][0]["cells"]
        changed = sorted(ref for ref in set(old_cells) | set(new_cells) if old_cells.get(ref) != new_cells.get(ref))
        self.assertEqual(changed, ["H9"])


# ---------------------------------------------------------------- (e) the v2 name is ignored like the others

class V2SpecNameIsIgnoredTests(unittest.TestCase):
    GITIGNORE = (REPO / ".gitignore").read_text(encoding="utf-8")
    NAMES = ("report.v2.spec.json", "My Excel.v2.spec.json", "report.v3.spec.json", "kpi.v2.spec.json",
             "report.spec.json", "kpi_spec.json")

    def test_the_gitignore_has_the_pattern_that_covers_the_v2_name(self):
        lines = [line.strip() for line in self.GITIGNORE.splitlines()]
        self.assertEqual(lines.count("*.spec.json"), 1)
        self.assertIn("*.spec.json", WORKFLOW_OUTPUT_PATTERNS)
        self.assertIn("kpi_spec*.json", lines)                               # the old name stays covered too

    def test_every_name_of_the_reuse_convention_is_ignored(self):
        patterns = gitignore_patterns(self.GITIGNORE)
        self.assertEqual([name for name in self.NAMES if not is_ignored(name, patterns)], [])

    def test_names_that_are_not_specs_stay_unignored(self):
        patterns = gitignore_patterns(self.GITIGNORE)
        for name in ("report.v2.spec.txt", "report.v2.spec.json.md", "spec.json", "report.spec.py", "specs.json",
                     "bind_kpi.py", "catalog.json"):
            with self.subTest(name=name):
                self.assertFalse(is_ignored(name, patterns))

    def test_the_hygiene_output_finder_catches_the_v2_name_and_the_old_name(self):
        with tempfile.TemporaryDirectory(prefix="kpi-reuse-out-") as tmp:
            for rel in ("work/report.v2.spec.json", "work/REPORT.V2.SPEC.JSON", "kpi_spec.json", "work/ok.json",
                        "catalog.json", "work/report.spec.txt"):
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as handle:
                    handle.write("{}")
            self.assertEqual(workflow_outputs(tmp), ["kpi_spec.json", "work/REPORT.V2.SPEC.JSON",
                                                     "work/report.v2.spec.json"])

    def test_git_itself_ignores_the_v2_name_when_there_is_a_git_folder(self):
        if not shutil.which("git") or not (REPO / ".git").exists():
            self.skipTest("needs git and a .git folder (the plugin may be checked out without one)")
        for name in ("work/report.v2.spec.json", "report.v2.spec.json", "work/kpi_spec.json"):
            with self.subTest(name=name):
                done = subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=str(REPO),
                                      capture_output=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
        done = subprocess.run(["git", "check-ignore", "--no-index", "-q", "scripts/bind_kpi.py"], cwd=str(REPO),
                              capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 1)

    def test_no_spec_file_sits_in_the_repository_tree(self):
        self.assertEqual([name for name in workflow_outputs(REPO) if name.lower().endswith(".spec.json")], [])


if __name__ == "__main__":
    unittest.main()
