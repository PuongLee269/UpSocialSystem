import json
import tempfile
import threading
import unittest
from pathlib import Path

from ytagent.jobs import ConflictError, InvalidError, JobStore, NotFoundError

SRC = {"video_id": "abc123", "name": "clip", "duration": 12, "rel_path": "0/clip.mp4"}


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outbox = Path(self.tmp.name)
        self.store = JobStore(self.outbox)

    def test_create_get_list(self):
        job = self.store.create(SRC)
        self.assertEqual(job["schema_version"], 2)
        self.assertEqual(job["status"], "source_selected")
        self.assertEqual(job["revision"], 1)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertEqual([j["id"] for j in self.store.list()], [job["id"]])

    def test_create_idempotent_until_public(self):
        a = self.store.create(SRC)
        self.assertEqual(self.store.create(SRC)["id"], a["id"])
        self.assertEqual(len(self.store.list()), 1)
        for st in ("content_ready", "vidiq_reviewed", "thumbnail_approved", "private_uploaded", "public"):
            self.store.transition(a["id"], st)
        self.assertNotEqual(self.store.create(SRC)["id"], a["id"])

    def test_create_requires_video_id(self):
        with self.assertRaises(InvalidError):
            self.store.create({})

    def test_get_missing_and_bad_id(self):
        for bad in ("nope12345", "../x", "", None):
            with self.subTest(bad=bad), self.assertRaises(NotFoundError):
                self.store.get(bad)

    def test_update_section_bumps_revision_and_merges(self):
        job = self.store.create(SRC)
        new = self.store.update_section(job["id"], "content", {"title": "Hi", "tags": ["a b", "c"]}, 1)
        self.assertEqual(new["revision"], 2)
        self.assertEqual(new["content"]["title"], "Hi")
        self.assertEqual(new["content"]["description"], "")
        self.assertEqual(new["content"]["tags_total_chars"], 3 + 2 + 1)
        self.assertEqual(self.store.get(job["id"])["revision"], 2)

    def test_revision_conflict(self):
        job = self.store.create(SRC)
        self.store.update_section(job["id"], "frame", {"selected_index": 1}, 1)
        with self.assertRaises(ConflictError):
            self.store.update_section(job["id"], "frame", {"selected_index": 2}, 1)
        self.assertEqual(self.store.get(job["id"])["frame"]["selected_index"], 1)

    def test_invalid_section_or_keys(self):
        job = self.store.create(SRC)
        for section, data, rev in (("upload", {}, 1), ("frame", {"bogus": 1}, 1),
                                   ("frame", [], 1), ("frame", {}, None)):
            with self.subTest(section=section, data=data), self.assertRaises(InvalidError):
                self.store.update_section(job["id"], section, data, rev)

    def test_returned_job_is_a_copy(self):
        job = self.store.create(SRC)
        job["content"]["title"] = "mutated"
        self.assertEqual(self.store.get(job["id"])["content"]["title"], "")

    def test_transition_forward_only(self):
        job = self.store.create(SRC)
        job = self.store.transition(job["id"], "content_ready")
        self.assertEqual(job["status"], "content_ready")
        for bad in ("source_selected", "content_ready"):
            with self.subTest(bad=bad), self.assertRaises(ConflictError):
                self.store.transition(job["id"], bad)
        with self.assertRaises(InvalidError):
            self.store.transition(job["id"], "weird")

    def test_record_error_keeps_status(self):
        job = self.store.transition(self.store.create(SRC)["id"], "content_ready")
        job = self.store.record_error(job["id"], "upload", "Mất mạng", retryable=True)
        self.assertEqual(job["status"], "content_ready")
        self.assertEqual(job["error"]["stage"], "upload")
        self.assertTrue(job["error"]["retryable"])
        self.assertEqual(job["history"][-1]["event"], "error")

    def test_update_cannot_change_id(self):
        job = self.store.create(SRC)
        with self.assertRaises(InvalidError):
            self.store.update(job["id"], lambda j: {**j, "id": "other"})

    def test_update_generic(self):
        job = self.store.create(SRC)
        new = self.store.update(job["id"], lambda j: {**j, "upload": {**j["upload"], "state": "in_progress"}},
                                expected_revision=1, event="upload_start")
        self.assertEqual(new["upload"]["state"], "in_progress")
        self.assertEqual(new["history"][-1]["event"], "upload_start")

    def test_atomic_write_leaves_no_tmp_and_valid_json(self):
        job = self.store.create(SRC)
        self.store.update_section(job["id"], "content", {"title": "Tiêu đề"}, 1)
        folder = self.outbox / "jobs" / job["id"]
        self.assertEqual([p.name for p in folder.iterdir()], ["job.json"])
        self.assertEqual(json.loads((folder / "job.json").read_text("utf-8"))["content"]["title"], "Tiêu đề")

    def test_concurrent_updates_one_wins_per_revision(self):
        job = self.store.create(SRC)
        results = []

        def worker(i):
            try:
                self.store.update_section(job["id"], "frame", {"selected_index": i}, 1)
                results.append("ok")
            except ConflictError:
                results.append("conflict")
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(self.store.get(job["id"])["revision"], 2)

    def test_import_legacy(self):
        legacy_dir = self.outbox / "legacy-job-0001"
        legacy_dir.mkdir()
        raw = {"id": "legacy-job-0001", "video": {"id": "vid9", "name": "Cũ", "duration": 30},
               "platforms": {"youtube": {"title": "T cũ", "description": "D", "tags": ["x", "y z"]}}}
        path = legacy_dir / "review.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = path.read_text("utf-8")
        job = self.store.import_legacy("legacy-job-0001")
        self.assertEqual(job["schema_version"], 2)
        self.assertEqual(job["source"]["video_id"], "vid9")
        self.assertEqual(job["content"]["title"], "T cũ")
        self.assertEqual(job["content"]["tags_total_chars"], 1 + 3 + 2)
        self.assertEqual(path.read_text("utf-8"), before)
        self.assertEqual(self.store.import_legacy("legacy-job-0001")["id"], job["id"])

    def test_import_legacy_missing(self):
        with self.assertRaises(NotFoundError):
            self.store.import_legacy("does-not-exist")


if __name__ == "__main__":
    unittest.main()
