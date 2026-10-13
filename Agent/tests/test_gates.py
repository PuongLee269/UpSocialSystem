import copy
import unittest

from ytagent.gates import CONFIRM_TEXT, can_go_public, can_upload_private
from ytagent.jobs import _new_job


def ready_job():
    job = _new_job({"video_id": "v1", "name": "n"})
    job["content"] = {"title": "T", "description": "D", "tags": ["a"], "tags_total_chars": 1}
    job["frame"]["selected_index"] = 2
    job["vidiq"]["confirmed_by_user"] = True
    job["thumbnail"]["approved_version"] = 1
    job["checklist"].update(rights=True, policy=True, metadata=True, disclosure=True, kids=True)
    return job


def uploaded_job():
    job = ready_job()
    job["status"] = "private_uploaded"
    job["upload"].update(state="done", youtube_video_id="YT123", privacy="private")
    return job


def _no_frame(j): j["frame"]["selected_index"] = None
def _no_vidiq(j): j["vidiq"]["confirmed_by_user"] = False
def _no_thumb(j): j["thumbnail"]["approved_version"] = None
def _no_check(j): j["checklist"]["kids"] = False
def _bad_title(j): j["content"]["title"] = ""
def _bad_tags(j): j["content"]["tags"] = ["a", "A"]


class UploadGateTests(unittest.TestCase):
    def test_ready_passes(self):
        self.assertEqual(can_upload_private(ready_job()), (True, []))

    def test_each_missing_requirement_blocks(self):
        for mutate in (_no_frame, _no_vidiq, _no_thumb, _no_check, _bad_title, _bad_tags):
            with self.subTest(case=mutate.__name__):
                job = ready_job()
                mutate(job)
                ok, reasons = can_upload_private(job)
                self.assertFalse(ok)
                self.assertTrue(reasons)

    def test_checklist_requires_true_not_truthy(self):
        job = ready_job()
        job["checklist"]["rights"] = "yes"
        self.assertFalse(can_upload_private(job)[0])


class PublicGateTests(unittest.TestCase):
    def test_passes_with_confirmation(self):
        self.assertEqual(can_go_public(uploaded_job(), "YT123", CONFIRM_TEXT), (True, []))

    def test_missing_or_wrong_confirmation(self):
        job = uploaded_job()
        for vid, text in ((None, CONFIRM_TEXT), ("OTHER", CONFIRM_TEXT), ("YT123", None),
                          ("YT123", "cong khai"), ("YT123", "")):
            with self.subTest(vid=vid, text=text):
                self.assertFalse(can_go_public(job, vid, text)[0])

    def test_requires_private_uploaded_state(self):
        job = uploaded_job()
        job["status"] = "thumbnail_approved"
        self.assertFalse(can_go_public(job, "YT123", CONFIRM_TEXT)[0])
        job = uploaded_job()
        job["upload"]["state"] = "in_progress"
        self.assertFalse(can_go_public(job, "YT123", CONFIRM_TEXT)[0])

    def test_no_video_id_blocks_even_if_confirm_none(self):
        job = uploaded_job()
        job["upload"]["youtube_video_id"] = None
        self.assertFalse(can_go_public(job, None, CONFIRM_TEXT)[0])

    def test_earlier_gates_must_still_hold(self):
        job = uploaded_job()
        job["checklist"]["policy"] = False
        self.assertFalse(can_go_public(job, "YT123", CONFIRM_TEXT)[0])

    def test_does_not_mutate_input(self):
        job = uploaded_job()
        before = copy.deepcopy(job)
        can_go_public(job, "YT123", CONFIRM_TEXT)
        self.assertEqual(job, before)


if __name__ == "__main__":
    unittest.main()
