import hashlib
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backup_cluster_engines_part4 import (
    S3AsyncMultipartDispatcher,
    split_passphrase_into_escrow_shares,
    reconstruct_passphrase_from_escrow_shares,
    CowBlockDeltaEngine,
    find_rabin_chunk_boundaries,
    execute_dr_restoration_smoke_test,
)

class TestBackupPart4ClusterAchievements(unittest.TestCase):
    def test_achievement_1_s3_multipart_dispatcher(self):
        dispatcher = S3AsyncMultipartDispatcher(upload_id="mp_upload_12345", total_parts=3)
        etag1 = dispatcher.upload_part(1, b"PART_1_DATA" * 50)
        etag2 = dispatcher.upload_part(2, b"PART_2_DATA" * 50)
        self.assertFalse(dispatcher.complete_multipart_upload().is_completed)

        etag3 = dispatcher.upload_part(3, b"PART_3_DATA" * 50)
        completed = dispatcher.complete_multipart_upload()
        self.assertTrue(completed.is_completed)
        self.assertEqual(len(completed.completed_etags), 3)

    def test_achievement_2_shamir_passphrase_escrow(self):
        secret = "MasterPassphrase2026!"
        shares = split_passphrase_into_escrow_shares(secret, threshold_k=3, num_shares_n=5)
        self.assertEqual(len(shares), 5)

        # Collect any 3 holders: holder_1, holder_3, holder_5
        k_shares = [shares["holder_1"], shares["holder_3"], shares["holder_5"]]
        recovered = reconstruct_passphrase_from_escrow_shares(k_shares)
        self.assertEqual(recovered, secret)

    def test_achievement_3_cow_block_delta(self):
        engine = CowBlockDeltaEngine(block_size_bytes=1024)
        # Exactly 10 KB virtual device (10 blocks of 1024 bytes)
        baseline = b"A" * 1024 * 10
        engine.capture_initial_baseline(baseline)

        # Modify only block 2 and block 7
        modified = bytearray(baseline)
        modified[2048] ^= 0xFF # block 2
        modified[7168] ^= 0xFF # block 7

        delta = engine.compute_incremental_delta(bytes(modified))
        self.assertEqual(delta.total_blocks_monitored, 10)
        self.assertEqual(delta.modified_blocks_count, 2)
        self.assertIn(2, delta.dirty_block_indices)
        self.assertIn(7, delta.dirty_block_indices)
        self.assertEqual(delta.delta_compression_ratio, 5.0)

    def test_achievement_4_rabin_chunk_boundaries(self):
        data = b"RANDOM_STREAM_DATA_BUFFER_" * 500
        boundaries = find_rabin_chunk_boundaries(data, min_chunk=512, max_chunk=4096)
        self.assertGreaterEqual(len(boundaries), 2)
        # Boundaries must be strictly monotonically increasing and end at len(data)
        self.assertEqual(boundaries[-1], len(data))
        for i in range(len(boundaries) - 1):
            self.assertLess(boundaries[i], boundaries[i + 1])

    def test_achievement_5_dr_smoke_test(self):
        manifest = {
            "file_1.txt": b"CONTENT_FILE_1",
            "file_2.json": b'{"status": "ok"}',
        }
        hasher = hashlib.sha256()
        for k in sorted(manifest.keys()):
            hasher.update(manifest[k])
        root_sum = hasher.hexdigest()

        report = execute_dr_restoration_smoke_test(manifest, root_sum)
        self.assertTrue(report.is_restoration_verified)
        self.assertTrue(report.integrity_checksum_match)
        self.assertEqual(report.rebuilt_file_count, 2)

if __name__ == "__main__":
    unittest.main()
