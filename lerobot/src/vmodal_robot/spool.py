from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire
from src.utils.util_log import log_info, log_error, log_trace, log_warning

import json
import sqlite3
import time
import uuid

from .contracts import (
    CONTRACT_VERSION,
    Artifact,
    DatasetRevision,
    Receipt,
    RevisionInput,
    SpoolFull,
)
from .utils import os_atomic_copy, os_remove_empty_parents, str_sha256_file, str_stable_id


@dataclass(frozen=True)
class SpoolConfig:
    root: str
    max_bytes: int = 10 * 1024 * 1024 * 1024
    aux_reserve_bytes: int = 512 * 1024 * 1024
    max_records: int = 10000
    max_video_bytes: int = 100 * 1024 * 1024


class Spool:
    def __init__(self, cfg: SpoolConfig):
        if cfg.max_bytes <= 0 or cfg.max_records <= 0:
            raise ValueError("max_bytes and max_records must be positive")
        if cfg.aux_reserve_bytes < 0 or cfg.aux_reserve_bytes >= cfg.max_bytes:
            raise ValueError("aux_reserve_bytes must be non-negative and smaller than max_bytes")
        if cfg.max_video_bytes <= 0:
            raise ValueError("max_video_bytes must be positive")
        self.cfg = cfg
        self.root = os.path.abspath(os.path.expanduser(cfg.root))
        self.obj_dir = os.path.join(self.root, "objects")
        os.makedirs(self.obj_dir, exist_ok=True)
        self.db_path = os.path.join(self.root, "state.sqlite3")
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self._schema()
        self.os_recover()

    def _schema(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                dataset_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                dataset_key TEXT NOT NULL,
                destination TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(source_id, dataset_key)
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                lane TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                byte_length INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                local_ref TEXT NOT NULL,
                local_present INTEGER NOT NULL DEFAULT 1,
                source_clock TEXT NOT NULL,
                time_start REAL,
                time_end REAL,
                source_refs_json TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                accepted_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS artifacts_lane_state
                ON artifacts(lane, state, next_attempt_at, accepted_at);
            CREATE TABLE IF NOT EXISTS revisions (
                revision_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_format TEXT NOT NULL,
                source_version TEXT NOT NULL,
                destination TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                complete INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'READY',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revision_artifacts (
                revision_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY(revision_id, artifact_id)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                artifact_id TEXT PRIMARY KEY,
                remote_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                checksum TEXT NOT NULL,
                detail TEXT NOT NULL,
                acknowledged_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revision_receipts (
                revision_id TEXT PRIMARY KEY,
                remote_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                acknowledged_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manifest_path TEXT NOT NULL,
                reason TEXT NOT NULL,
                rejected_at REAL NOT NULL
            );
            """
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _dataset_id(self, item: RevisionInput) -> str:
        row = self.db.execute(
            "SELECT dataset_id, destination FROM datasets WHERE source_id=? AND dataset_key=?",
            (item.source_id, item.dataset_key),
        ).fetchone()
        if row:
            if row["destination"] != item.destination:
                raise ValueError("a persisted dataset cannot change destination")
            return str(row["dataset_id"])
        value = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO datasets VALUES (?, ?, ?, ?, ?)",
            (value, item.source_id, item.dataset_key, item.destination, time.time()),
        )
        self.db.commit()
        return value

    def _usage(self) -> Tuple[int, int, int]:
        row = self.db.execute(
            """
            SELECT COALESCE(SUM(byte_length), 0) AS total,
                   COALESCE(SUM(CASE WHEN lane='video' THEN byte_length ELSE 0 END), 0) AS video,
                   COUNT(*) AS records
            FROM artifacts WHERE local_present=1
            """
        ).fetchone()
        return int(row["total"]), int(row["video"]), int(row["records"])

    def reject(self, manifest_path: str, reason: str):
        self.db.execute(
            "INSERT INTO rejections(manifest_path, reason, rejected_at) VALUES (?, ?, ?)",
            (manifest_path, reason, time.time()),
        )
        self.db.commit()

    def accept(self, item: RevisionInput) -> DatasetRevision:
        try:
            return self._accept(item)
        except Exception as exc:
            self.reject(item.manifest_path, str(exc))
            raise

    def _accept(self, item: RevisionInput) -> DatasetRevision:
        if not item.artifacts:
            raise ValueError("a revision must reference at least one artifact")
        dataset_id = self._dataset_id(item)
        rows = []
        for source in item.artifacts:
            if not source.checksum or len(source.checksum) != 64:
                raise ValueError(f"sha256 checksum required for {source.rel_path}")
            size = os.path.getsize(source.source_path)
            if size != source.byte_length:
                raise ValueError(f"manifest size differs from source for {source.rel_path}")
            if source.kind == "video" and size > self.cfg.max_video_bytes:
                raise SpoolFull(
                    f"video exceeds deployment limit: {size} > {self.cfg.max_video_bytes} bytes"
                )
            artifact_id = str_stable_id(dataset_id, source.rel_path, source.checksum)
            lane = "video" if source.kind == "video" else "aux"
            rows.append((source, artifact_id, lane))

        artifact_ids = tuple(row[1] for row in rows)
        revision_id = str_stable_id(dataset_id, item.source_revision, *artifact_ids)
        old = self.db.execute(
            "SELECT revision_id FROM revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if old:
            return self.get_revision(revision_id)

        missing = [row for row in rows if not self.db.execute(
            "SELECT 1 FROM artifacts WHERE artifact_id=?", (row[1],)
        ).fetchone()]
        total, video, records = self._usage()
        add_total = sum(row[0].byte_length for row in missing)
        add_video = sum(row[0].byte_length for row in missing if row[2] == "video")
        if records + len(missing) > self.cfg.max_records:
            raise SpoolFull(f"spool record quota exceeded: {self.cfg.max_records}")
        if total + add_total > self.cfg.max_bytes:
            raise SpoolFull(f"spool byte quota exceeded: {self.cfg.max_bytes}")
        video_limit = max(0, self.cfg.max_bytes - self.cfg.aux_reserve_bytes)
        if video + add_video > video_limit:
            raise SpoolFull(f"video lane quota exceeded: {video_limit}")

        copied = []
        now = time.time()
        try:
            for source, artifact_id, lane in missing:
                target = os.path.join(self.obj_dir, artifact_id[:2], artifact_id, "payload")
                if os.path.exists(target):
                    if str_sha256_file(target) != source.checksum:
                        raise ValueError(f"spool object checksum conflict: {artifact_id}")
                else:
                    size, checksum = os_atomic_copy(source.source_path, target)
                    copied.append(target)
                    if size != source.byte_length or checksum != source.checksum:
                        raise ValueError(f"source checksum differs from manifest for {source.rel_path}")
                self.db.execute(
                    """
                    INSERT INTO artifacts(
                        artifact_id, dataset_id, source_id, kind, lane, rel_path,
                        content_type, byte_length, checksum, local_ref, source_clock,
                        time_start, time_end, source_refs_json, state, accepted_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?)
                    """,
                    (
                        artifact_id, dataset_id, item.source_id, source.kind, lane,
                        source.rel_path, source.content_type, source.byte_length,
                        source.checksum, target, source.source_clock, source.time_start,
                        source.time_end, json.dumps(source.source_refs or {}, sort_keys=True),
                        now, now,
                    ),
                )
            self.db.execute(
                """
                INSERT INTO revisions(
                    revision_id, dataset_id, source_id, source_format, source_version,
                    destination, source_revision, complete, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id, dataset_id, item.source_id, item.source_format,
                    item.source_version, item.destination, item.source_revision,
                    int(item.complete), now, now,
                ),
            )
            for position, artifact_id in enumerate(artifact_ids):
                self.db.execute(
                    "INSERT INTO revision_artifacts VALUES (?, ?, ?)",
                    (revision_id, artifact_id, position),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            for target in copied:
                if os.path.exists(target):
                    os.unlink(target)
                    os_remove_empty_parents(target, self.obj_dir)
            raise
        return self.get_revision(revision_id)

    def _artifact(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            contract_version=CONTRACT_VERSION,
            artifact_id=str(row["artifact_id"]),
            dataset_id=str(row["dataset_id"]),
            source_id=str(row["source_id"]),
            kind=str(row["kind"]),
            rel_path=str(row["rel_path"]),
            content_type=str(row["content_type"]),
            byte_length=int(row["byte_length"]),
            checksum=str(row["checksum"]),
            local_ref=str(row["local_ref"]),
            source_clock=str(row["source_clock"]),
            time_start=row["time_start"],
            time_end=row["time_end"],
            source_refs=json.loads(row["source_refs_json"]),
        )

    def get_artifact(self, artifact_id: str) -> Artifact:
        row = self.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not row:
            raise KeyError(artifact_id)
        return self._artifact(row)

    def artifact_state(self, artifact_id: str) -> str:
        row = self.db.execute("SELECT state FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not row:
            raise KeyError(artifact_id)
        return str(row["state"])

    def artifact_attempts(self, artifact_id: str) -> int:
        row = self.db.execute("SELECT attempts FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not row:
            raise KeyError(artifact_id)
        return int(row["attempts"])

    def get_revision(self, revision_id: str) -> DatasetRevision:
        row = self.db.execute("SELECT * FROM revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if not row:
            raise KeyError(revision_id)
        ids = tuple(
            str(value["artifact_id"])
            for value in self.db.execute(
                "SELECT artifact_id FROM revision_artifacts WHERE revision_id=? ORDER BY position",
                (revision_id,),
            )
        )
        return DatasetRevision(
            contract_version=CONTRACT_VERSION,
            revision_id=str(row["revision_id"]),
            dataset_id=str(row["dataset_id"]),
            source_id=str(row["source_id"]),
            source_format=str(row["source_format"]),
            source_version=str(row["source_version"]),
            destination=str(row["destination"]),
            source_revision=str(row["source_revision"]),
            complete=bool(row["complete"]),
            artifact_ids=ids,
        )

    def _artifact_destination(self, artifact_id: str) -> str:
        row = self.db.execute(
            """
            SELECT r.destination FROM revisions r
            JOIN revision_artifacts ra ON ra.revision_id=r.revision_id
            WHERE ra.artifact_id=? ORDER BY r.created_at LIMIT 1
            """,
            (artifact_id,),
        ).fetchone()
        if not row:
            raise KeyError(artifact_id)
        return str(row["destination"])

    def sending(self, lane: str) -> List[Tuple[Artifact, str]]:
        rows = self.db.execute(
            "SELECT * FROM artifacts WHERE lane=? AND state='SENDING' ORDER BY accepted_at",
            (lane,),
        ).fetchall()
        return [(self._artifact(row), self._artifact_destination(row["artifact_id"])) for row in rows]

    def claim(self, lane: str) -> Optional[Tuple[Artifact, str]]:
        now = time.time()
        row = self.db.execute(
            """
            SELECT * FROM artifacts
            WHERE lane=? AND local_present=1
              AND (state='READY' OR (state='RETRY_WAIT' AND next_attempt_at<=?))
            ORDER BY accepted_at LIMIT 1
            """,
            (lane, now),
        ).fetchone()
        if not row:
            return None
        self.db.execute(
            "UPDATE artifacts SET state='SENDING', attempts=attempts+1, updated_at=? WHERE artifact_id=?",
            (now, row["artifact_id"]),
        )
        self.db.commit()
        return self.get_artifact(row["artifact_id"]), self._artifact_destination(row["artifact_id"])

    def mark_acknowledged(self, receipt: Receipt):
        now = time.time()
        self.db.execute(
            "UPDATE artifacts SET state='ACKNOWLEDGED', last_error='', updated_at=? WHERE artifact_id=?",
            (now, receipt.artifact_id),
        )
        self.db.execute(
            """
            INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              remote_ref=excluded.remote_ref, status=excluded.status,
              checksum=excluded.checksum, detail=excluded.detail,
              acknowledged_at=excluded.acknowledged_at
            """,
            (
                receipt.artifact_id, receipt.remote_ref, receipt.status,
                receipt.checksum, receipt.detail, now,
            ),
        )
        self.db.commit()

    def mark_retry(self, artifact_id: str, reason: str, delay: float):
        now = time.time()
        self.db.execute(
            """
            UPDATE artifacts SET state='RETRY_WAIT', next_attempt_at=?, last_error=?, updated_at=?
            WHERE artifact_id=?
            """,
            (now + max(0.0, delay), reason, now, artifact_id),
        )
        self.db.commit()

    def mark_ready(self, artifact_id: str, reason: str = ""):
        self.db.execute(
            "UPDATE artifacts SET state='READY', next_attempt_at=0, last_error=?, updated_at=? WHERE artifact_id=?",
            (reason, time.time(), artifact_id),
        )
        self.db.commit()

    def mark_blocked(self, artifact_id: str, reason: str):
        self.db.execute(
            "UPDATE artifacts SET state='BLOCKED', last_error=?, updated_at=? WHERE artifact_id=?",
            (reason, time.time(), artifact_id),
        )
        self.db.commit()

    def claim_revision(self) -> Optional[DatasetRevision]:
        now = time.time()
        row = self.db.execute(
            """
            SELECT r.revision_id FROM revisions r
            WHERE (r.state='READY' OR (r.state='RETRY_WAIT' AND r.next_attempt_at<=?))
              AND NOT EXISTS (
                SELECT 1 FROM revision_artifacts ra
                JOIN artifacts a ON a.artifact_id=ra.artifact_id
                WHERE ra.revision_id=r.revision_id AND a.state!='ACKNOWLEDGED'
              )
            ORDER BY r.created_at LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return None
        self.db.execute(
            "UPDATE revisions SET state='SENDING', attempts=attempts+1, updated_at=? WHERE revision_id=?",
            (now, row["revision_id"]),
        )
        self.db.commit()
        return self.get_revision(row["revision_id"])

    def sending_revisions(self) -> List[DatasetRevision]:
        rows = self.db.execute(
            "SELECT revision_id FROM revisions WHERE state='SENDING' ORDER BY created_at"
        ).fetchall()
        return [self.get_revision(row["revision_id"]) for row in rows]

    def revision_attempts(self, revision_id: str) -> int:
        row = self.db.execute("SELECT attempts FROM revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if not row:
            raise KeyError(revision_id)
        return int(row["attempts"])

    def revision_state(self, revision_id: str) -> str:
        row = self.db.execute("SELECT state FROM revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if not row:
            raise KeyError(revision_id)
        return str(row["state"])

    def blocked_count(self) -> int:
        row = self.db.execute("SELECT COUNT(*) AS count FROM artifacts WHERE state='BLOCKED'").fetchone()
        return int(row["count"])

    def mark_revision_ready(self, revision_id: str, reason: str = ""):
        self.db.execute(
            "UPDATE revisions SET state='READY', next_attempt_at=0, last_error=?, updated_at=? WHERE revision_id=?",
            (reason, time.time(), revision_id),
        )
        self.db.commit()

    def mark_revision_acknowledged(self, revision_id: str, receipt: Receipt):
        now = time.time()
        self.db.execute(
            "UPDATE revisions SET state='ACKNOWLEDGED', last_error='', updated_at=? WHERE revision_id=?",
            (now, revision_id),
        )
        self.db.execute(
            """
            INSERT INTO revision_receipts VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(revision_id) DO UPDATE SET
              remote_ref=excluded.remote_ref, status=excluded.status,
              detail=excluded.detail, acknowledged_at=excluded.acknowledged_at
            """,
            (revision_id, receipt.remote_ref, receipt.status, receipt.detail, now),
        )
        self.db.commit()

    def mark_revision_retry(self, revision_id: str, reason: str, delay: float):
        now = time.time()
        self.db.execute(
            """
            UPDATE revisions SET state='RETRY_WAIT', next_attempt_at=?, last_error=?, updated_at=?
            WHERE revision_id=?
            """,
            (now + max(0.0, delay), reason, now, revision_id),
        )
        self.db.commit()

    def mark_revision_blocked(self, revision_id: str, reason: str):
        self.db.execute(
            "UPDATE revisions SET state='BLOCKED', last_error=?, updated_at=? WHERE revision_id=?",
            (reason, time.time(), revision_id),
        )
        self.db.commit()

    def pending_count(self) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS count FROM artifacts WHERE state NOT IN ('ACKNOWLEDGED', 'BLOCKED')"
        ).fetchone()
        return int(row["count"])

    def status(self) -> Dict[str, Any]:
        total, video, records = self._usage()
        values = os.statvfs(self.root)
        disk_free = int(values.f_bavail * values.f_frsize)
        lanes = {}
        for lane in ("video", "aux"):
            row = self.db.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(byte_length), 0) AS bytes,
                       MIN(accepted_at) AS oldest
                FROM artifacts WHERE lane=? AND state!='ACKNOWLEDGED'
                """,
                (lane,),
            ).fetchone()
            lanes[lane] = {
                "pending_count": int(row["count"]),
                "pending_bytes": int(row["bytes"]),
                "oldest_age_seconds": max(0.0, time.time() - float(row["oldest"])) if row["oldest"] else 0.0,
            }
        blocked = self.db.execute(
            "SELECT COUNT(*) AS count FROM artifacts WHERE state='BLOCKED'"
        ).fetchone()
        retries = self.db.execute(
            "SELECT COALESCE(SUM(CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END), 0) AS count FROM artifacts"
        ).fetchone()
        rejected = self.db.execute("SELECT COUNT(*) AS count FROM rejections").fetchone()
        receipt = self.db.execute(
            "SELECT remote_ref, acknowledged_at FROM receipts ORDER BY acknowledged_at DESC LIMIT 1"
        ).fetchone()
        return {
            "root": self.root,
            "spool_bytes": total,
            "record_count": records,
            "disk_free_bytes": disk_free,
            "disk_headroom_bytes": min(disk_free, max(0, self.cfg.max_bytes - total)),
            "video_bytes": video,
            "lanes": lanes,
            "blocked_count": int(blocked["count"]),
            "retry_count": int(retries["count"]),
            "rejected_handoffs": int(rejected["count"]),
            "last_receipt": dict(receipt) if receipt else None,
        }

    def cleanup(self, retention_seconds: float) -> int:
        cutoff = time.time() - max(0.0, retention_seconds)
        rows = self.db.execute(
            """
            SELECT a.artifact_id, a.local_ref FROM artifacts a
            WHERE a.state='ACKNOWLEDGED' AND a.local_present=1
              AND NOT EXISTS (
                SELECT 1 FROM revision_artifacts ra JOIN revisions r ON r.revision_id=ra.revision_id
                WHERE ra.artifact_id=a.artifact_id
                  AND (r.state!='ACKNOWLEDGED' OR r.updated_at>?)
              )
            """,
            (cutoff,),
        ).fetchall()
        count = 0
        for row in rows:
            path = str(row["local_ref"])
            if os.path.isfile(path):
                os.unlink(path)
                os_remove_empty_parents(path, self.obj_dir)
            self.db.execute(
                "UPDATE artifacts SET local_present=0, updated_at=? WHERE artifact_id=?",
                (time.time(), row["artifact_id"]),
            )
            count += 1
        self.db.commit()
        return count

    def os_recover(self):
        known = {
            os.path.abspath(str(row["local_ref"]))
            for row in self.db.execute("SELECT local_ref FROM artifacts WHERE local_present=1")
        }
        for root, _, files in os.walk(self.obj_dir):
            for name in files:
                path = os.path.abspath(os.path.join(root, name))
                if name.startswith(".accept-") or (name == "payload" and path not in known):
                    os.unlink(path)
                    os_remove_empty_parents(path, self.obj_dir)
