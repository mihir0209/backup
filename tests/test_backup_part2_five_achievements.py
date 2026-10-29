import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backup_cluster_engines_part2 import (
    GlacierLifecycleTieringEngine,
    MultiRecipientEnvelopeEncryption,
    FsSnapshotLockGovernor,
    ParallelResumableUploadManager,
    verify_bare_metal_disaster_recovery_plan,
)

class TestBackupPart2ClusterAchievements(unittest.TestCase):
    def test_achievement_1_glacier_lifecycle_tiering(self):
        engine = GlacierLifecycleTieringEngine(glacier_after_days=90, deep_archive_after_days=180)
        # 30-day old snapshot stays in standard
        t30 = engine.evaluate_snapshot_tier("snap_30d", 30, "STANDARD")
        self.assertEqual(t30.recommended_tier, "STANDARD")
        self.assertFalse(t30.is_actionable)

        # 120-day old moves to Glacier Flexible
        t120 = engine.evaluate_snapshot_tier("snap_120d", 120, "STANDARD")
        self.assertEqual(t120.recommended_tier, "GLACIER_FLEXIBLE")
        self.assertTrue(t120.is_actionable)
        self.assertEqual(t120.transition_cost_usd, 0.02)

        # 200-day old moves to Deep Archive
        t200 = engine.evaluate_snapshot_tier("snap_200d", 200, "GLACIER_FLEXIBLE")
        self.assertEqual(t200.recommended_tier, "DEEP_ARCHIVE")

    def test_achievement_2_envelope_encryption_escrow(self):
        encryptor = MultiRecipientEnvelopeEncryption()
        payload = b"CRITICAL_FINANCIAL_BACKUP_PAYLOAD_DATA"
        recipients = {
            "backup_admin": "pubkey_rsa4096_admin_alpha",
            "security_officer": "pubkey_rsa4096_officer_beta",
        }
        encrypted = encryptor.encrypt_with_escrow(payload, recipients)
        self.assertNotEqual(encrypted.ciphertext_bytes, payload)
        self.assertEqual(len(encrypted.wrapped_keys), 2)
        recipient_ids = [k.recipient_id for k in encrypted.wrapped_keys]
        self.assertIn("backup_admin", recipient_ids)
        self.assertIn("security_officer", recipient_ids)
        self.assertEqual(len(encrypted.nonce_hex), 24) # 12 bytes = 24 hex

    def test_achievement_3_fs_snapshot_lock_governor(self):
        governor = FsSnapshotLockGovernor()
        freeze = governor.acquire_quiescence_freeze()
        self.assertTrue(freeze.is_filesystem_quiesced)
        self.assertEqual(freeze.vss_writer_status, "VSS_WRITER_STATE_STABLE")

        thaw = governor.release_thaw()
        self.assertFalse(thaw.is_filesystem_quiesced)
        self.assertEqual(thaw.vss_writer_status, "VSS_WRITER_STATE_THAWED")

    def test_achievement_4_parallel_resumable_upload(self):
        uploader = ParallelResumableUploadManager(total_parts=3)
        self.assertFalse(uploader.is_upload_complete())

        self.assertTrue(uploader.record_uploaded_part(1, "hash_part_1", 1024 * 1024))
        self.assertTrue(uploader.record_uploaded_part(2, "hash_part_2", 1024 * 1024))
        self.assertFalse(uploader.is_upload_complete())

        # Complete final part
        self.assertTrue(uploader.record_uploaded_part(3, "hash_part_3", 512 * 1024))
        self.assertTrue(uploader.is_upload_complete())

    def test_achievement_5_bare_metal_disaster_recovery_plan(self):
        # 100 GB backup manifest with 150 GB available disk -> feasible
        report = verify_bare_metal_disaster_recovery_plan(
            manifest_files_count=50000,
            manifest_total_bytes=100 * 1024 * 1024 * 1024,
            available_disk_capacity_bytes=150 * 1024 * 1024 * 1024,
            gpt_partition_spec_present=True
        )
        self.assertTrue(report.is_restoration_feasible)
        self.assertEqual(report.readiness_tier, "BARE_METAL_RESTORATION_READY")

        # Insufficient disk space (only 80 GB available for 100 GB backup)
        fail_report = verify_bare_metal_disaster_recovery_plan(
            manifest_files_count=50000,
            manifest_total_bytes=100 * 1024 * 1024 * 1024,
            available_disk_capacity_bytes=80 * 1024 * 1024 * 1024,
            gpt_partition_spec_present=True
        )
        self.assertFalse(fail_report.is_restoration_feasible)

if __name__ == "__main__":
    unittest.main()
