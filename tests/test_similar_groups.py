"""Similarity and reversible merge checks using synthetic notes only."""
import copy
import json
from pathlib import Path
import random
import string
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import similar_groups as similar


def note(identifier, body, title="Draft", date="2026-01-01", **fields):
    return {"id": identifier, "title": title, "project": "Writing",
            "noteDate": date, "updated": date, "srcNoteId": "source-" + identifier,
            "active": 1, "versions": [{"v": 1, "content": body, "time": date}],
            **fields}


def prose(seed, length):
    rng = random.Random(seed)
    return "".join(rng.choices(string.ascii_letters + string.digits, k=length))


class SimilarGroupsTests(unittest.TestCase):
    def test_body_requires_bidirectional_overlap(self):
        body = prose(7, 1200)
        data = {"prompts": [note("component", body),
                            note("larger", body + prose(9, 400))]}
        # The smaller note is fully contained and the length ratio is allowed.
        # A one-way score would incorrectly be 1.0 and group these notes.
        result = similar.scan(data)
        self.assertEqual(result["compared"], 2)
        self.assertEqual(result["groups"], [])
        self.assertEqual(len(similar.scan(data, {"threshold": .70})["groups"]), 1)

    def test_near_versions_are_sorted_and_scan_is_read_only(self):
        body = prose(3, 1600)
        data = {"prompts": [note("latest", body[:-40] + prose(6, 40), date="2026-03-01"),
                            note("original", body, date="2026-01-01")]}
        original = copy.deepcopy(data)
        result = similar.scan(data)
        self.assertEqual(data, original)
        self.assertEqual(result["config"], similar.DEFAULTS)
        self.assertEqual(result["tooShort"], 0)
        group, = result["groups"]
        self.assertEqual(group["reason"], "body")
        self.assertEqual([m["id"] for m in group["members"]], ["original", "latest"])
        self.assertEqual(group["members"][0]["body"], body)
        self.assertEqual(group["members"][0]["preview"], body[:400])
        self.assertEqual(group["members"][0]["versions"], 1)
        score, = group["scores"]
        self.assertEqual({score["a"], score["b"]}, {0, 1})
        self.assertGreaterEqual(score["score"], .90)

    def test_length_ratio_is_an_independent_limit(self):
        body = prose(11, 1000)
        data = {"prompts": [note("a", body), note("b", body + prose(12, 600))]}
        self.assertEqual(similar.scan(data, {"threshold": .50})["groups"], [])
        result = similar.scan(data, {"threshold": .50, "maxRatio": 2})
        self.assertEqual(len(result["groups"]), 1)

    def test_short_notes_require_matching_title_and_body(self):
        data = {"prompts": [note("a", "buy milk", title="Shopping"),
                            note("b", "buy bread", title="Shopping"),
                            note("c", "buy milk", title="Reminder"),
                            note("d", "buy milk", title="Shopping")]}
        result = similar.scan(data)
        self.assertEqual(result["compared"], 0)
        self.assertEqual(result["tooShort"], 4)
        group, = result["groups"]
        self.assertEqual(group["reason"], "title")
        self.assertEqual({m["id"] for m in group["members"]}, {"a", "d"})

    def test_automatic_titles_and_empty_bodies_are_excluded(self):
        entries = []
        for title in [*similar.GENERIC_TITLES, "NEW NOTE", "Untitled", "", "  note  "]:
            entries.extend([note(str(len(entries)), "same body", title=title),
                            note(str(len(entries) + 1), "same body", title=title)])
        entries.extend([note("empty-1", "", title="Named"),
                        note("empty-2", " \n!?", title="Named")])
        self.assertEqual(similar.scan({"prompts": entries})["groups"], [])

    def test_active_body_and_normalization(self):
        entry = note("a", "old")
        entry["versions"].append({"v": 2, "content": "new"})
        self.assertEqual(similar.active_body(entry), "old")
        entry["active"] = 99
        self.assertEqual(similar.active_body(entry), "new")
        self.assertEqual(similar.active_body({}), "")
        self.assertEqual(similar.normalize("你，好！\r\n New note — text"), "你好Newnotetext")

    def test_merge_preserves_keeper_imported_ids_and_undo_restores_full_entries(self):
        entries = [note("middle", "middle body", date="2026-02-01"),
                   note("newest", "newest body", title="Latest title", date="2026-03-01",
                        project="Research", extra={"nested": [1, 2]}),
                   note("oldest", "oldest body", date="2026-01-01"),
                   note("unrelated", "keep me")]
        entries[2]["versions"].append({"v": 2, "content": "older inactive revision"})
        data = {"prompts": entries, "projects": ["Writing", "Research"],
                "importedNoteIds": [e["srcNoteId"] for e in entries]}
        before = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as directory:
            merged, stat = similar.merge(data, [["newest", "oldest", "middle"]], directory)
            self.assertEqual(stat, {"groups": 1, "removed": 2})
            self.assertEqual(len(merged["prompts"]), len(before["prompts"]) - 3 + 1)
            self.assertEqual(merged["importedNoteIds"], before["importedNoteIds"])
            keeper = next(e for e in merged["prompts"] if e["id"] == "newest")
            for key in ("id", "title", "project", "srcNoteId", "extra"):
                self.assertEqual(keeper[key], before["prompts"][1][key])
            self.assertEqual(keeper["active"], 3)
            self.assertEqual([v["v"] for v in keeper["versions"]], [1, 2, 3])
            self.assertEqual([v["content"] for v in keeper["versions"]],
                             ["oldest body", "middle body", "newest body"])
            record_file = Path(directory) / "merge-log.json"
            record = json.loads(record_file.read_text())
            self.assertEqual(record["groups"][0]["keeperId"], "newest")
            self.assertEqual(record["groups"][0]["before"][0], before["prompts"][2])
            keeper["extra"]["nested"].append(3)
            restored, stat = similar.undo(merged, directory)
            self.assertEqual(stat, {"restored": 3, "groups": 1})
            self.assertEqual({e["id"]: e for e in restored["prompts"]},
                             {e["id"]: e for e in before["prompts"]})
            self.assertEqual(restored["importedNoteIds"], before["importedNoteIds"])
            self.assertFalse(record_file.exists())
            untouched = copy.deepcopy(restored)
            _, stat = similar.undo(restored, directory)
            self.assertIn("error", stat)
            self.assertEqual(restored, untouched)

    def test_missing_members_do_not_create_a_merge_record(self):
        data = {"prompts": [note("remaining", "body")]}
        before = copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as directory:
            _, stat = similar.merge(data, [["missing", "remaining"]], directory)
            self.assertEqual(stat, {"groups": 0, "removed": 0})
            self.assertEqual(data, before)
            self.assertFalse(Path(similar.record_path(directory)).exists())


if __name__ == "__main__":
    unittest.main()
