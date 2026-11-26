import hashlib
import json
import unittest
from pathlib import Path

from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.check_profiles import copy_check_profile
from app.persistence.connection import get_db
from app.persistence.defaults import (
    DEFAULT_CHECK_ITEMS_BY_CODE,
    _sync_document_review_prompts,
    seed_defaults,
)
from app.persistence.software_defaults import software_prompt
from app.tasks.selection import selected_check_items
from app.tasks.submission import _enabled_check_item_snapshots
from tests import test_db as fixtures

BASELINE = json.loads(
    (Path(__file__).parent / "fixtures/document_review_prompts.json").read_text(
        encoding="utf-8"
    )
)


def prompt_for(profile, code):
    return (
        software_prompt(code)
        if profile == "software"
        else DEFAULT_CHECK_ITEMS_BY_CODE[code]["prompt"]
    )


class DocumentReviewPromptTest(unittest.TestCase):
    setUp = fixtures.CheckItemDefaultsTest.setUp
    tearDown = fixtures.CheckItemDefaultsTest.tearDown

    def item(self, key):
        profile, code = key.split(":")
        return dict(
            get_db()
            .execute(
                "SELECT * FROM check_items WHERE profile_id=? AND template_code=? AND pool_item_id=id",
                (profile, code),
            )
            .fetchone()
        )

    def install_baseline(self):
        for key, prompt in BASELINE["prompts"].items():
            get_db().execute(
                "UPDATE check_items SET prompt=? WHERE id=?",
                (prompt, self.item(key)["id"]),
            )
        get_db().commit()

    def test_existing_pooled_templates_sync_once_and_shared_profiles_keep_memberships(
        self,
    ):
        db = get_db()
        shared = copy_check_profile("default", "安装交付资料")
        self.install_baseline()
        memberships = list(
            db.execute("SELECT * FROM check_profile_items ORDER BY 1, 2")
        )
        revisions = dict(db.execute("SELECT id, revision FROM check_profiles"))
        before = {key: self.item(key) for key in BASELINE["prompts"]}

        seed_defaults()

        for key, item in before.items():
            updated = self.item(key)
            self.assertEqual(updated["prompt"], prompt_for(*key.split(":")))
            self.assertNotEqual(updated["prompt"], item["prompt"])
            for field in (
                "id",
                "code",
                "name",
                "enabled",
                "sort_order",
                "check_method",
            ):
                self.assertEqual(updated[field], item[field])
        self.assertEqual(
            list(db.execute("SELECT * FROM check_profile_items ORDER BY 1, 2")),
            memberships,
        )
        after = dict(db.execute("SELECT id, revision FROM check_profiles"))
        for profile in ("default", "software", shared):
            self.assertEqual(after[profile], revisions[profile] + 2)
        seed_defaults()
        self.assertEqual(
            dict(db.execute("SELECT id, revision FROM check_profiles")), after
        )

    def test_customized_prompts_and_skill_packages_keep_their_contents(self):
        db = get_db()
        for mode in ("appended", "replaced", "skill"):
            with self.subTest(mode=mode):
                expected = {}
                for key, stock in BASELINE["prompts"].items():
                    prompt = (
                        stock + "\n企业要求：仅核对合同明确约定的术语。"
                        if mode == "appended"
                        else "企业专用审查规则"
                        if mode == "replaced"
                        else stock
                    )
                    method = "skill" if mode == "skill" else "prompt"
                    db.execute(
                        "UPDATE check_items SET prompt=?, check_method=?, skill_package_json=? WHERE id=?",
                        (
                            prompt,
                            method,
                            '{"name":"enterprise-review"}',
                            self.item(key)["id"],
                        ),
                    )
                    expected[key] = self.item(key)
                db.commit()
                seed_defaults()
                for key, item in expected.items():
                    self.assertEqual(self.item(key), item)

    def test_new_submissions_use_current_prompts_and_retries_keep_saved_prompts(self):
        db = get_db()
        self.install_baseline()
        previous = {}
        for key in BASELINE["prompts"]:
            profile = key.split(":")[0]
            item = self.item(key)
            previous[key] = _enabled_check_item_snapshots(
                db, [item["id"]], DOCUMENT_TASK_TYPE, profile
            )

        self.assertEqual(_sync_document_review_prompts(db, "2026-11-26 12:00:00"), 4)
        self.assertEqual(_sync_document_review_prompts(db, "2026-11-26 12:00:01"), 0)

        for key, snapshot in previous.items():
            profile, code = key.split(":")
            task = {
                "task_type": DOCUMENT_TASK_TYPE,
                "checks_snapshot_json": json.dumps(snapshot),
                "retry_check_codes_json": json.dumps([snapshot[0]["code"]]),
            }
            self.assertEqual(
                selected_check_items(db, task)[0]["prompt"], BASELINE["prompts"][key]
            )
            current = _enabled_check_item_snapshots(
                db, [snapshot[0]["id"]], DOCUMENT_TASK_TYPE, profile
            )
            self.assertEqual(current[0]["prompt"], prompt_for(profile, code))

    def test_completeness_and_understandability_are_unchanged(self):
        for key, digest in BASELINE["untouched_sha256"].items():
            with self.subTest(key=key):
                prompt = prompt_for(*key.split(":"))
                self.assertEqual(
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(), digest
                )
                self.assertEqual(self.item(key)["prompt"], prompt)


if __name__ == "__main__":
    unittest.main()
