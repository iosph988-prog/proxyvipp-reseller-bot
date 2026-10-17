import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from access_control import (
    approve,
    generated_key_record,
    is_allowed,
    key_deletion_denial,
    list_active,
    mark_generated_key_deleted,
    record_generated_keys,
    revoke,
    should_alert,
)


class AccessControlTests(unittest.TestCase):
    ADMIN = 111111111
    RESELLER = 222222222

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.patch_env = patch.dict(os.environ, {"ACCESS_DB": str(Path(self.temp_dir.name) / "access.sqlite3")})
        self.patch_env.start()

    def tearDown(self):
        self.patch_env.stop()
        self.temp_dir.cleanup()

    def test_admin_bypass_and_temporary_grant(self):
        admin_id, reseller_id = self.ADMIN, self.RESELLER
        self.assertTrue(is_allowed(admin_id, admin_id, now=1000))
        self.assertFalse(is_allowed(reseller_id, admin_id, now=1000))
        expires = approve(reseller_id, 7, admin_id, now=1000)
        self.assertEqual(expires, 1000 + 7 * 86400)
        self.assertTrue(is_allowed(reseller_id, admin_id, now=1001))
        self.assertFalse(is_allowed(reseller_id, admin_id, now=expires))
        self.assertEqual(list_active(now=expires), [])

    def test_revoke(self):
        approve(self.RESELLER, 2, self.ADMIN, now=100)
        revoke(self.RESELLER)
        self.assertFalse(is_allowed(self.RESELLER, self.ADMIN, now=101))

    def test_invalid_grant(self):
        with self.assertRaises(ValueError):
            approve(self.RESELLER, 0, self.ADMIN, now=100)
        with self.assertRaises(ValueError):
            approve(self.RESELLER, 31, self.ADMIN, now=100)
        with self.assertRaises(ValueError):
            approve(-1, 1, self.ADMIN, now=100)

    def test_grant_accepts_one_through_thirty_days(self):
        self.assertEqual(approve(self.RESELLER, 1, self.ADMIN, now=100), 100 + 86400)
        self.assertEqual(approve(self.RESELLER, 30, self.ADMIN, now=100), 100 + 30 * 86400)

    def test_request_alert_cooldown(self):
        self.assertTrue(should_alert(self.RESELLER, cooldown_seconds=100, now=1000))
        self.assertFalse(should_alert(self.RESELLER, cooldown_seconds=100, now=1099))
        self.assertTrue(should_alert(self.RESELLER, cooldown_seconds=100, now=1100))

    def test_generated_key_ownership_and_deletion_marker(self):
        key = "EXTERNAL-AB12-CD34-EF56"
        self.assertIsNone(generated_key_record(key))
        self.assertEqual(key_deletion_denial(self.RESELLER, self.ADMIN, key), "untracked")
        self.assertIsNone(key_deletion_denial(self.ADMIN, self.ADMIN, key))
        record_generated_keys(self.RESELLER, [key], now=1000)
        self.assertEqual(generated_key_record(key), (self.RESELLER, None))
        self.assertIsNone(key_deletion_denial(self.RESELLER, self.ADMIN, key))
        self.assertEqual(key_deletion_denial(self.ADMIN + 1, self.ADMIN, key), "not_owner")
        mark_generated_key_deleted(key, now=2000)
        self.assertEqual(generated_key_record(key), (self.RESELLER, 2000))
        self.assertEqual(key_deletion_denial(self.RESELLER, self.ADMIN, key), "already_deleted")


if __name__ == "__main__":
    unittest.main()
