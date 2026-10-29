import hashlib
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backup_cluster_engines_part3 import (
    P2pDeltaBlockReplicator,
    MerkleMountainRange,
    EbpfWriteJournalEngine,
    SparseChunkFingerprintIndex,
    RansomwareCanaryTripwireGuard,
)

class TestBackupPart3ClusterAchievements(unittest.TestCase):
    def test_achievement_1_p2p_delta_block(self):
        piece1 = b"PIECE_DATA_BLOCK_001"
        piece2 = b"PIECE_DATA_BLOCK_002"
        h1 = hashlib.sha256(piece1).hexdigest()
        h2 = hashlib.sha256(piece2).hexdigest()

        replicator = P2pDeltaBlockReplicator([h1, h2])
        t1 = replicator.download_piece(0, piece1, "peer_node_alpha")
        self.assertIsNotNone(t1)
        self.assertFalse(t1.is_bitfield_complete)

        # Download second piece completes the bitfield
        t2 = replicator.download_piece(1, piece2, "peer_node_beta")
        self.assertIsNotNone(t2)
        self.assertTrue(t2.is_bitfield_complete)

    def test_achievement_2_merkle_mountain_range(self):
        mmr = MerkleMountainRange()
        idx0 = mmr.append_leaf("0x" + "1" * 64)
        idx1 = mmr.append_leaf("0x" + "2" * 64)
        root = mmr.compute_bagged_peaks_root()
        self.assertEqual(len(root), 64)

        proof = mmr.generate_inclusion_proof(idx0)
        self.assertEqual(proof["leaf_index"], 0)
        self.assertEqual(proof["total_leaves"], 2)

    def test_achievement_3_ebpf_journal(self):
        journal = EbpfWriteJournalEngine()
        rec = journal.log_vfs_write_intercept(inode=14092, offset=4096, length=1024)
        self.assertEqual(rec.inode_number, 14092)
        self.assertEqual(rec.byte_count, 1024)
        self.assertEqual(len(journal.journal), 1)

    def test_achievement_4_sparse_fingerprint(self):
        index = SparseChunkFingerprintIndex()
        full_hash = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0a1b2c3d4e5f6g7h8i9j0k1l2"
        prefix = index.insert_fingerprint(full_hash)
        self.assertEqual(prefix, "a1b2")

        matched = index.candidate_lookup("a1b2ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
        self.assertEqual(matched, full_hash)

    def test_achievement_5_canary_tripwire_guard(self):
        baseline = {
            "/data/canary_01.docx": "0x1111111111111111111111111111111111111111111111111111111111111111",
            "/data/canary_02.xlsx": "0x2222222222222222222222222222222222222222222222222222222222222222",
        }
        guard = RansomwareCanaryTripwireGuard(baseline)
        # Normal uncompromised check
        clean = guard.verify_canary_integrity(baseline)
        self.assertFalse(clean.is_tripwire_tampered)
        self.assertFalse(clean.immutable_lock_enforced)

        # Ransomware modifies canary file (encrypts / changes hash)
        compromised = {
            "/data/canary_01.docx": "0x9999999999999999999999999999999999999999999999999999999999999999",
            "/data/canary_02.xlsx": baseline["/data/canary_02.xlsx"],
        }
        alert = guard.verify_canary_integrity(compromised)
        self.assertTrue(alert.is_tripwire_tampered)
        self.assertTrue(alert.immutable_lock_enforced)
        self.assertIn("WORM_LOCK", alert.incident_alert_action)

if __name__ == "__main__":
    unittest.main()
