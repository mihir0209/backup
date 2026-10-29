import hashlib
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backup_cluster_engines_part5 import (
    AsyncioSshP2pStreamPipeline,
    ZstdDictionaryCompressionTuner,
    MmrSnapshotAppendLog,
    VssVolumeShadowCopyProvider,
    run_bare_metal_recovery_smoke_test,
)

class TestBackupPart5ClusterAchievements(unittest.TestCase):
    def test_achievement_1_ssh_p2p_stream(self):
        pipeline = AsyncioSshP2pStreamPipeline(target_node="node_beta_haha")
        chunk = b"PEER_TO_PEER_SSH_STREAMED_CHUNK_DATA_001"
        transfer = pipeline.send_chunk_stream(seq_id=1, chunk_bytes=chunk, channel_id=3)
        self.assertEqual(transfer.sequence_id, 1)
        self.assertEqual(transfer.bytes_streamed, len(chunk))
        self.assertTrue(transfer.flow_control_ack_received)
        self.assertEqual(len(transfer.chunk_digest), 64)

    def test_achievement_2_zstd_dict_compression(self):
        samples = [b"const express = require('express');\n", b"import { NextRequest } from 'next/server';\n"]
        tuner = ZstdDictionaryCompressionTuner(samples)
        payload = b"const express = require('express');\napp.get('/api', (req, res) => res.json({ status: 'ok' }));"
        result = tuner.compress_with_custom_dict(payload)
        self.assertGreater(result.dictionary_trained_bytes, 0)
        self.assertGreater(result.compression_ratio_boost_pct, 0.0)

    def test_achievement_3_mmr_append_log(self):
        mmr = MmrSnapshotAppendLog()
        idx0, root0 = mmr.append_snapshot_commitment({"id": "snap_0", "ts": 1000})
        self.assertEqual(idx0, 0)
        self.assertEqual(len(root0), 64)

        idx1, root1 = mmr.append_snapshot_commitment({"id": "snap_1", "ts": 2000})
        self.assertEqual(idx1, 1)
        self.assertNotEqual(root0, root1)

    def test_achievement_4_vss_provider(self):
        vss = VssVolumeShadowCopyProvider()
        snap = vss.create_atomic_volume_snapshot("C:")
        self.assertTrue(snap.is_consistent_point_in_time)
        self.assertEqual(snap.original_volume, "C:")
        self.assertIn("HarddiskVolumeShadowCopy", snap.shadow_device_path)

    def test_achievement_5_bare_metal_smoke_test(self):
        checksum = "0x" + "a" * 62 # length 64
        # Valid bare metal restore with 20480 sectors and 5000 inodes
        res = run_bare_metal_recovery_smoke_test(20480, 5000, checksum)
        self.assertTrue(res.is_bare_metal_bootable)
        self.assertTrue(res.uefi_gpt_recreated)
        self.assertTrue(res.fsck_clean_pass)

        # Truncated sectors
        fail_res = run_bare_metal_recovery_smoke_test(512, 5000, checksum)
        self.assertFalse(fail_res.is_bare_metal_bootable)

if __name__ == "__main__":
    unittest.main()
