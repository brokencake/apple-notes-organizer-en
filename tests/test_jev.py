"""Tests for the Jev suggestion layer.

No test reaches the network: call_jev is replaced, so the decision logic, the
secret filter and the keyword layer are all exercised without an API key.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jev_classify as jev


def bodies_from(mapping):
    def fetch(ids):
        return [mapping.get(i, "") for i in ids], None
    return fetch


class ConfigTests(unittest.TestCase):
    def test_defaults_fill_in_and_key_is_not_erased_by_blank(self):
        with tempfile.TemporaryDirectory() as lib:
            conf = jev.load_conf(lib)
            self.assertEqual(conf["autoThreshold"], 0.75)
            self.assertEqual(conf["descriptions"], {})

            jev.save_conf(lib, {"apiKey": "apikey_live", "autoThreshold": 0.9})
            jev.save_conf(lib, {"apiKey": "", "descriptions": {"Research": "papers"}})

            conf = jev.load_conf(lib)
            self.assertEqual(conf["apiKey"], "apikey_live")
            self.assertEqual(conf["autoThreshold"], 0.9)
            self.assertEqual(conf["descriptions"], {"Research": "papers"})

    def test_stored_key_is_private_to_this_account(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live"})
            mode = os.stat(jev.config_path(lib)).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_environment_key_is_a_fallback_not_an_override(self):
        with tempfile.TemporaryDirectory() as lib:
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "from_env"}):
                self.assertEqual(jev.get_key(lib), "from_env")
                jev.save_conf(lib, {"apiKey": "from_file"})
                self.assertEqual(jev.get_key(lib), "from_file")


class SecretTests(unittest.TestCase):
    def test_credentials_and_identity_numbers_are_never_sent(self):
        samples = ["sk-abcdefghijklmnopqrstuvwx",
                   "apikey_0000000000000000000000000000example",
                   "ntn_1234567890abcdefghijkl",
                   "ghp_1234567890abcdefghijkl",
                   "AKIAIOSFODNN7EXAMPLE",
                   "password: hunter2",
                   "密码：hunter2",
                   "123-45-6789"]
        for text in samples:
            self.assertTrue(jev.looks_secret(text)[0], text)

    def test_ordinary_notes_are_not_treated_as_secret(self):
        for text in ["Reading notes on attention and memory.",
                     "买牛奶、鸡蛋、咖啡豆",
                     ""]:
            self.assertFalse(jev.looks_secret(text)[0], text)

    def test_named_folders_are_skipped_without_reading_the_text(self):
        conf = {"neverSend": ["Passwords"]}
        self.assertTrue(jev.looks_secret("harmless", "Passwords", conf)[0])
        self.assertFalse(jev.looks_secret("harmless", "Recipes", conf)[0])

    def test_a_secret_note_reaches_no_network_call(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live"})
            with mock.patch.object(jev, "call_jev", side_effect=AssertionError("sent")) as call:
                rows, err = jev.classify(
                    lib, [{"id": "n1", "name": "AWS", "folder": "Work"}],
                    [("Work", "work notes")],
                    bodies_from({"n1": "AKIAIOSFODNN7EXAMPLE"}))
            self.assertIsNone(err)
            self.assertEqual(rows[0]["verdict"], jev.VERDICT_SKIPPED)
            call.assert_not_called()

    def test_the_pattern_check_is_on_until_you_turn_it_off(self):
        self.assertFalse(jev.DEFAULTS["sendSensitive"])
        with tempfile.TemporaryDirectory() as lib:
            self.assertFalse(jev.load_conf(lib)["sendSensitive"])

    def test_opting_in_lets_a_flagged_note_through(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live", "sendSensitive": True})
            with mock.patch.object(jev, "call_jev", return_value={}) as call:
                rows, err = jev.classify(
                    lib, [{"id": "n1", "name": "AWS", "folder": "Work"}],
                    [("Work", "work notes")],
                    bodies_from({"n1": "AKIAIOSFODNN7EXAMPLE"}))
            self.assertIsNone(err)
            self.assertNotEqual(rows[0]["verdict"], jev.VERDICT_SKIPPED)
            call.assert_called_once()

    def test_a_never_send_folder_is_excluded_even_when_opted_in(self):
        """The folder list is a decision already made; the toggle only lifts
        the pattern check, never an explicit exclusion."""
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live", "sendSensitive": True,
                                "neverSend": ["Passwords"]})
            with mock.patch.object(jev, "call_jev",
                                   side_effect=AssertionError("sent")) as call:
                rows, err = jev.classify(
                    lib, [{"id": "n1", "name": "Bank", "folder": "Passwords"}],
                    [("Work", "work notes")],
                    bodies_from({"n1": "nothing unusual here"}))
            self.assertIsNone(err)
            self.assertEqual(rows[0]["verdict"], jev.VERDICT_SKIPPED)
            self.assertIn("never-send", rows[0]["error"])
            call.assert_not_called()


class KeywordTests(unittest.TestCase):
    def test_exactly_one_match_decides_locally(self):
        self.assertEqual(jev.keyword_hit("about Lenormand", {"Tarot": ["lenormand"]}), "Tarot")

    def test_matching_several_projects_is_not_a_decision(self):
        hit = jev.keyword_hit("lenormand and bazi",
                              {"Tarot": ["lenormand"], "Astrology": ["bazi"]})
        self.assertIsNone(hit)

    def test_keyword_hit_costs_no_request_and_needs_no_key(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"keywords": {"Recipes": ["sourdough"]}})
            with mock.patch.object(jev, "call_jev", side_effect=AssertionError("sent")):
                rows, err = jev.classify(
                    lib, [{"id": "n1", "name": "Sourdough starter", "folder": ""}],
                    [("Recipes", "baking")], bodies_from({"n1": "feed it daily"}))
            self.assertIsNone(err)
            self.assertEqual(rows[0]["verdict"], jev.VERDICT_AUTO)
            self.assertEqual(rows[0]["project"], "Recipes")


class QuestionTests(unittest.TestCase):
    def test_one_question_per_project_in_a_single_request(self):
        qs = jev.build_questions([("Writing", "drafts"), ("Research", "")])
        self.assertEqual(len(qs), 2)
        self.assertEqual({q["type"] for q in qs.values()}, {"noul"})
        self.assertIn("drafts", qs["c0"]["instructions"])
        self.assertIn("Writing", qs["c0"]["criteria"]["true"])

    def test_non_ascii_project_names_do_not_become_question_keys(self):
        qs = jev.build_questions([("算命玄学", "命理")])
        self.assertEqual(list(qs), ["c0"])

    def test_questions_follow_the_interface_language(self):
        self.assertIn("Does this note belong",
                      jev.build_questions([("A", "")], "en")["c0"]["instructions"])
        self.assertIn("这条备忘录",
                      jev.build_questions([("A", "")], "zh")["c0"]["instructions"])


class ScoreShapeTests(unittest.TestCase):
    def test_known_response_shapes_all_flatten(self):
        projects = [("A", ""), ("B", "")]
        for payload in ({"nouls": {"c0": {"noul": 0.9}, "c1": {"noul": 0.1}}},
                        {"answers": {"c0": {"value": 0.9}, "c1": {"probability": 0.1}}},
                        {"c0": 0.9, "c1": 0.1}):
            self.assertEqual(jev.extract_scores(payload, projects), {"A": 0.9, "B": 0.1})

    def test_an_unreadable_response_yields_no_scores_rather_than_zeros(self):
        self.assertEqual(jev.extract_scores({"unexpected": 1}, [("A", "")]), {})


class JudgeTests(unittest.TestCase):
    conf = {"autoThreshold": 0.75, "doubtThreshold": 0.45, "minGap": 0.15}

    def test_confident_and_clear_is_filed_automatically(self):
        r = jev.judge({"A": 0.92, "B": 0.20}, self.conf)
        self.assertEqual(r["verdict"], jev.VERDICT_AUTO)
        self.assertEqual(r["project"], "A")
        self.assertEqual(r["runnerUp"], "B")

    def test_belonging_nowhere_stays_unfiled_instead_of_being_forced(self):
        self.assertEqual(jev.judge({"A": 0.2, "B": 0.1}, self.conf)["verdict"],
                         jev.VERDICT_UNFILED)

    def test_two_close_projects_are_handed_back_to_the_human(self):
        self.assertEqual(jev.judge({"A": 0.88, "B": 0.80}, self.conf)["verdict"],
                         jev.VERDICT_CONFIRM)

    def test_high_but_below_threshold_asks_rather_than_files(self):
        self.assertEqual(jev.judge({"A": 0.60, "B": 0.10}, self.conf)["verdict"],
                         jev.VERDICT_CONFIRM)

    def test_no_scores_is_reported_as_an_error_not_as_unfiled(self):
        self.assertEqual(jev.judge({}, self.conf)["verdict"], jev.VERDICT_ERROR)


class ClassifyTests(unittest.TestCase):
    def test_a_full_run_reports_coverage_and_keeps_the_body_local(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live"})
            long_body = "Notes on attention. " * 474
            sent = []

            def fake(key, state, questions, timeout=60):
                sent.append(state)
                return {"nouls": {"c0": {"noul": 0.95}, "c1": {"noul": 0.10}}}

            with mock.patch.object(jev, "call_jev", fake):
                rows, err = jev.classify(
                    lib, [{"id": "n1", "name": "Draft", "folder": "Notes"}],
                    [("Writing", "drafts"), ("Research", "papers")],
                    bodies_from({"n1": long_body}))

            self.assertIsNone(err)
            row = rows[0]
            self.assertEqual(row["verdict"], jev.VERDICT_AUTO)
            self.assertEqual(row["project"], "Writing")
            self.assertEqual((row["asked"], row["answered"]), (2, 2))
            # Only the opening is sent; the preview kept for the browser is
            # shorter still, and neither is the whole note.
            self.assertEqual(len(sent[0]["body"]), jev.BODY_LIMIT)
            self.assertEqual(len(row["body"]), jev.PREVIEW_LIMIT)
            self.assertTrue(row["bodyTruncated"])
            self.assertEqual(row["bodyLength"], len(long_body))

    def test_partial_coverage_is_visible_rather_than_looking_like_low_scores(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live"})
            with mock.patch.object(jev, "call_jev",
                                   return_value={"nouls": {"c0": {"noul": 0.9}}}):
                rows, _ = jev.classify(
                    lib, [{"id": "n1", "name": "Draft", "folder": ""}],
                    [("Writing", "drafts"), ("Research", "papers")],
                    bodies_from({"n1": "text"}))
            self.assertEqual((rows[0]["asked"], rows[0]["answered"]), (2, 1))

    def test_missing_key_is_reported_per_row_not_as_a_failed_run(self):
        with tempfile.TemporaryDirectory() as lib:
            with mock.patch.dict(os.environ, {}, clear=True):
                rows, err = jev.classify(
                    lib, [{"id": "n1", "name": "Draft", "folder": ""}],
                    [("Writing", "drafts")], bodies_from({"n1": "text"}))
            self.assertIsNone(err)
            self.assertEqual(rows[0]["verdict"], jev.VERDICT_NO_KEY)

    def test_http_errors_surface_the_response_detail(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live"})
            error = urllib.error.HTTPError(jev.API_URL, 401, "Unauthorized", {}, None)
            error.read = lambda: b'{"error":"invalid key"}'
            with mock.patch.object(jev, "call_jev", side_effect=error):
                rows, _ = jev.classify(
                    lib, [{"id": "n1", "name": "Draft", "folder": ""}],
                    [("Writing", "drafts")], bodies_from({"n1": "text"}))
            self.assertEqual(rows[0]["verdict"], jev.VERDICT_ERROR)
            self.assertIn("401", rows[0]["error"])
            self.assertIn("invalid key", rows[0]["error"])

    def test_no_projects_and_oversized_batches_are_refused_up_front(self):
        with tempfile.TemporaryDirectory() as lib:
            _, err = jev.classify(lib, [{"id": "n1", "name": "x", "folder": ""}], [],
                                  bodies_from({}))
            self.assertIsNotNone(err)
            items = [{"id": f"n{i}", "name": "x", "folder": ""}
                     for i in range(jev.MAX_ITEMS + 1)]
            _, err = jev.classify(lib, items, [("A", "a")], bodies_from({}))
            self.assertIn(str(jev.MAX_ITEMS), err)

    def test_a_notes_failure_stops_the_run_instead_of_guessing_from_titles(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {"apiKey": "apikey_live"})
            rows, err = jev.classify(
                lib, [{"id": "n1", "name": "Draft", "folder": ""}],
                [("Writing", "drafts")], lambda ids: (None, "Permission denied"))
            self.assertIsNone(rows)
            self.assertEqual(err, "Permission denied")


if __name__ == "__main__":
    unittest.main()
