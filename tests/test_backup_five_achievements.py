import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backup_cluster_engines import (
    BackupZstdArchiverEngine,
    FastCdcDeduplicator,
    ClusterBackupReplicationProtocol,
    ArchiveIntegrityVerifier,
    SnapshotMetadata,
    GfsRetentionGovernor,
)

class TestBackupClusterAchievements(unittest.TestCase):
    def test_achievement_1_zstd_compression(self):
        engine = BackupZstdArchiverEngine(compression_level=6)
        payload = b"BACKUP_PAYLOAD_DATA_COMPRESSION_TEST_" * 500
        compressed, metrics = engine.compress_stream(payload)
        self.assertLess(len(compressed), len(payload))
        self.assertGreater(metrics.compression_ratio, 5.0)
        self.assertGreater(metrics.crc32_checksum, 0)

        decompressed = engine.decompress_stream(compressed)
        self.assertEqual(decompressed, payload)

    def test_achievement_2_cdc_dedup(self):
        cdc = FastCdcDeduplicator(target_chunk_size=2048, min_size=512, max_size=8192)
        sample_file = b"CHUNK_ALPHA_DATA_" * 300 + b"CHUNK_BETA_DATA_" * 300
        chunks = cdc.chunk_payload(sample_file)
        self.assertGreaterEqual(len(chunks), 2)
        total_len = sum(c.chunk_size for c in chunks)
        self.assertEqual(total_len, len(sample_file))
        self.assertTrue(all(len(c.chunk_hash) == 64 for c in chunks))

    def test_achievement_3_cluster_node_sync(self):
        # Node Alpha on instabox-yuzz, Node Beta on instabox-haha
        node_alpha = ClusterBackupReplicationProtocol("node_alpha_yuzz")
        node_beta = ClusterBackupReplicationProtocol("node_beta_haha")

        chunk_data = b"REPLICATED_DATABASE_DUMP_CHUNK_001"
        cdc = FastCdcDeduplicator()
        chunks = cdc.chunk_payload(chunk_data)

        for c in chunks:
            node_alpha.store_chunk(c.chunk_hash, chunk_data)

        manifest = node_alpha.build_sync_manifest("snap_2026_10_29", "node_beta_haha", chunks)
        self.assertEqual(manifest.node_source, "node_alpha_yuzz")
        self.assertEqual(manifest.total_bytes, len(chunk_data))

        # Replicate chunks to Beta
        for h in manifest.chunk_hashes:
            node_beta.store_chunk(h, node_alpha.storage[h])

        self.assertTrue(node_beta.verify_replicated_snapshot(manifest))
        self.assertTrue(manifest.replication_verified)

    def test_achievement_4_hmac_archive_integrity(self):
        secret_key = b"super_cluster_backup_secret_key_32b!"
        verifier = ArchiveIntegrityVerifier(secret_key)

        archive = b"VALID_ENCRYPTED_ZIP_ARCHIVE_BYTES"
        seal = verifier.seal_archive(archive)
        self.assertEqual(len(seal.archive_hash_sha256), 64)
        self.assertEqual(len(seal.hmac_signature), 64)
        self.assertTrue(verifier.verify_seal(archive, seal))

        tampered = archive + b"_tampered_byte"
        self.assertFalse(verifier.verify_seal(tampered, seal))

    def test_achievement_5_gfs_retention_governor(self):
        governor = GfsRetentionGovernor(keep_hourly=3, keep_daily=2, keep_weekly=1, keep_monthly=1)
        snaps = [
            SnapshotMetadata("snap_h1", 1750003600, "HOURLY"),
            SnapshotMetadata("snap_h2", 1750007200, "HOURLY"),
            SnapshotMetadata("snap_h3", 1750010800, "HOURLY"),
            SnapshotMetadata("snap_h4", 1750014400, "HOURLY"),
            SnapshotMetadata("snap_d1", 1750000000, "DAILY"),
            SnapshotMetadata("snap_d2", 1750086400, "DAILY"),
            SnapshotMetadata("snap_d3", 1750172800, "DAILY"),
        ]
        retained, pruned = governor.evaluate_retention(snaps)
        self.assertIn("snap_h4", retained)
        self.assertIn("snap_h1", pruned)
        self.assertIn("snap_d1", pruned)
        self.assertEqual(len(retained), 5)

if __name__ == "__main__":
    unittest.main()
