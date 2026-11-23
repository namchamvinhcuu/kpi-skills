"""0.1.4: the simplest way for QA/QC staff to ask Claude Code for a KPI: two commands, no long prompt to paste.

`/odoo-kpi-skills:kpi-setup` (once per computer) and `/odoo-kpi-skills:kpi` (every KPI) carry the rules that used to
live in two prompt files the staff had to copy: plain words, one step at a time, a recommendation per question, the
dashboard title question, the clean-up offer. The text is not code, so these checks look for each rule by its words
(the helpers of `test_docs_contract`) and keep the safety rules of the first-time setup (the key never passes through
the session, `init-config` only in the user's own terminal, the host name typed by the human).
"""
import json
import re
import unittest

from helpers import REPO
from test_docs_contract import (COMMAND, NEG, README, README_VI, SKILL, DocCase, gap, parse_frontmatter, read, says,
                                section, sections, squash)

import odoo_rpc

SETUP = REPO / "commands" / "kpi-setup.md"


class SetupCommandTests(DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SETUP)
        cls.fields, cls.problems = parse_frontmatter(cls.text)
        cls.body = cls.text.split("\n---\n", 1)[1]

    def test_the_file_is_the_command_named_kpi_setup_with_a_frontmatter_a_strict_reader_accepts(self):
        self.assertEqual(SETUP.name, "kpi-setup.md")
        self.assertEqual(self.problems, [])
        self.assertTrue(self.fields.get("description"), "the command needs a non-empty description")
        self.assertIn("argument-hint", self.fields)

    def test_it_never_runs_init_config_itself_nor_through_a_terminal_trick(self):
        flat = squash(self.body)
        near = [flat[max(0, m.start() - 200):m.end() + 200] for m in re.finditer(r"init-config", flat)]
        self.assertTrue(near, "the command must mention init-config")
        self.assertTrue(any(re.search(NEG + r" run", chunk) and "own terminal" in chunk for chunk in near),
                        "the command must say init-config is never run by Claude, only in the user's own terminal")
        self.assertEqual(says(self.body, r"hard rule 9", r"terminal trick"), [])

    def test_the_api_key_and_the_password_never_pass_through_the_session(self):
        self.assertEqual(says(self.body, NEG + r" ask for" + gap(60) + r"api key",
                              r"never paste the key or the password into this chat",
                              r"delete that key in odoo"), [])

    def test_the_host_name_is_typed_by_the_human_and_the_odoo_details_come_from_the_user(self):
        self.assertEqual(says(self.body, r"never fill in .?--confirm-instance", r"full host name",
                              r"never take them from a file"), [])

    def test_it_goes_one_step_at_a_time_and_waits_for_the_user(self):
        self.assertEqual(says(self.body, r"one step at a time", r"wait for the user", NEG + r" skip"), [])

    def test_the_steps_are_python_check_config_api_key_init_config_check_done_in_that_order(self):
        steps = re.findall(r"(?m)^(\d+)\. \*\*([^*]+)\*\*", self.body)
        self.assertEqual([number for number, _ in steps], ["1", "2", "3", "4", "5", "6"])
        titles = [title.lower() for _, title in steps]
        for index, word in enumerate(("python", "already set up", "api key", "configuration file", "check from here",
                                      "done")):
            self.assertIn(word, titles[index])

    def test_it_already_set_up_skips_ahead_instead_of_redoing_the_work(self):
        self.assertEqual(says(self.body, r"check-config", r"config: file, private \(permissions ok\)",
                              r"go to step 6"), [])

    def test_the_windows_store_python_is_refused_as_in_the_readme(self):
        self.assertEqual(says(self.body, r"windowsapps", r"pythonsoftwarefoundation", r"microsoft store"), [])

    def test_the_last_step_points_at_the_daily_command(self):
        self.assertIn("/odoo-kpi-skills:kpi`", self.body)

    def test_it_holds_no_address_login_or_key_of_a_real_instance(self):
        # the repository is public: the values the user needs come from their administrator
        for pattern in (r"https?://[^\s`]*\.(?:cloud|com|net|vn|kr)\b", r"\bnam_dksk\b", r"\badmin/"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.text, re.I))


class DailyCommandTests(DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(COMMAND)
        cls.fields, cls.problems = parse_frontmatter(cls.text)
        cls.body = cls.text.split("\n---\n", 1)[1]

    def test_the_arguments_are_optional_and_the_hint_shows_it(self):
        self.assertRegex(self.fields["argument-hint"], r"^\"?\[")

    def test_without_an_excel_it_runs_nothing_and_tells_the_three_things_to_do_in_odoo(self):
        self.assertEqual(says(self.body, r"no excel given yet: do not run anything", r"upload", r"file > snapshot",
                              r"copy the whole address"), [])

    def test_without_a_config_it_sends_the_user_to_the_setup_command(self):
        self.assertEqual(says(self.body, r"/odoo-kpi-skills:kpi-setup", r"no config yet"), [])

    def test_it_asks_one_question_for_the_dashboard_name_or_code(self):
        self.assertEqual(says(self.body, r"one short question", r"name or code", r"dashboard title"), [])

    def test_the_host_name_is_typed_once_per_session_by_the_user(self):
        self.assertEqual(says(self.body, r"ask them to type it", r"once per session", NEG + r" copy it from the address"), [])

    def test_it_ends_by_offering_to_delete_the_work_folder(self):
        self.assertEqual(says(self.body, r"offer to delete the work folder", r"customer's excel content"), [])

    def test_it_still_publishes_only_after_the_reconcile_dry_run_and_a_yes(self):
        self.assertEqual(says(self.body, NEG + r" publish before" + gap(80) + r"dry run" + gap(80) + r"\byes\b"), [])


class SkillTalkingRulesTests(DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)

    def test_the_talking_section_comes_before_the_workflow(self):
        talking = section(self.text, r"^talking to the user")
        workflow = section(self.text, r"^workflow")
        self.assertIsNotNone(talking)
        self.assertLess(talking[0], workflow[0])

    def test_the_talking_rules_are_plain_words_one_thing_a_time_a_recommendation_and_the_title_question(self):
        body = section(self.text, r"^talking to the user")[1]
        self.assertEqual(says(body, r"one thing at a time", r"never use a technical name", r"recommendation",
                              r"name or code the dashboard title", r"from the excel", r"ask the odoo administrator"), [])

    def test_the_clean_up_step_asks_first_and_deletes_only_what_this_session_made(self):
        body = section(self.text, r"^8\. clean up")[1]
        self.assertEqual(says(body, r"ask(?:s)? whether to delete", r"only after a yes",
                              r"only the files you made in this session", NEG + r" .{0,40}the user's own excel"), [])

    def test_the_clean_up_step_comes_after_check_together_and_before_troubleshooting(self):
        titles = [title.lower() for title, _, _, _ in sections(self.text)]
        self.assertLess(titles.index(next(t for t in titles if t.startswith("7. check together"))),
                        titles.index(next(t for t in titles if t.startswith("8. clean up"))))
        self.assertLess(titles.index(next(t for t in titles if t.startswith("8. clean up"))),
                        titles.index(next(t for t in titles if t.startswith("troubleshooting"))))


class ReadmeAndVersionTests(DocCase):
    def test_the_vietnamese_readme_opens_with_the_two_commands_before_the_detailed_sections(self):
        text = read(README_VI)
        quick = section(text, r"^bắt đầu nhanh")
        first = section(text, r"^1\. cần gì")
        self.assertIsNotNone(quick)
        self.assertLess(quick[0], first[0])
        for command in ("/plugin marketplace add", "/plugin install odoo-kpi-skills@odoo-kpi",
                        "/odoo-kpi-skills:kpi-setup", "/odoo-kpi-skills:kpi\n"):
            self.assertIn(command, quick[1])

    def test_the_english_readme_lists_the_setup_command_file(self):
        self.assertIn("`commands/kpi-setup.md`", read(README))
        self.assertIn("/odoo-kpi-skills:kpi-setup", read(README))

    def test_the_manifest_the_client_and_the_readme_say_the_same_version(self):
        manifest = json.loads(read(REPO / ".claude-plugin" / "plugin.json"))["version"]
        self.assertEqual(manifest, odoo_rpc.VERSION)
        self.assertIn("**%s," % manifest, read(README))


if __name__ == "__main__":
    unittest.main()
