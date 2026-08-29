"""Sandbox-only reader for the semantic terminal reconciliation projection.

This file intentionally uses only the Python standard library so it can run
with ``python -I -B`` without importing repository or site-package code.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import resource
import sqlite3
import stat
from collections import Counter
from contextlib import suppress
from datetime import datetime
from pathlib import Path

_CAPABILITY = "guo.decision_guidance"
_OUTPUT_SCHEMA = "fin.semantic-terminal-reconciliation/v2"
_TIMING_OUTPUT_SCHEMA = "fin.semantic-daily-workspace-timing/v1"
_SCHEMA_MANIFEST_VERSION = "fin.semantic-snapshot-schema-manifest/v1"
# ``schema_version`` pragma deliberately excluded from the manifest: it is
# SQLite's internal schema cookie, which advances on every DDL — a migrated
# v1 owner can never match a fresh v2 database on that value.
_SCHEMA_PRAGMAS = frozenset(
    {
        "application_id",
        "user_version",
    }
)
# Read allowlist keeps ``schema_version`` for read-stability checks only.
_PRAGMA_ALLOWLIST = _SCHEMA_PRAGMAS | frozenset({"schema_version"})
_ACTIVE_STATES = frozenset({"queued", "running"})
_TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "timed_out", "cancelled"})
_PRODUCT_STATES = frozenset({"succeeded", "partial"})
_FORBIDDEN_PRODUCT_FIELDS = frozenset(
    {
        "current_advice",
        "action",
        "trade_action",
        "position",
        "position_action",
        "position_size",
        "entry",
        "entry_price",
        "entry_timing",
        "exit",
        "exit_price",
        "exit_timing",
        "order",
        "order_instruction",
        "price",
        "target_price",
        "stop_loss_price",
        "buy_price",
        "sell_price",
        "recommended_position",
        "entry_window",
        "exit_window",
    }
)
_MAX_STORE_PART_BYTES = 64 * 1024 * 1024
_MAX_MATERIALIZED_BYTES = 2 * _MAX_STORE_PART_BYTES
_MAX_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
_MAX_OPEN_FILES = 32
_MAX_TIMING_SAMPLES = 40
_TIMING_CANDIDATE_MULTIPLIER = 8
_DAILY_WORKSPACE_CHECKPOINTS = frozenset({"premarket", "morning", "close", "postmarket"})


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--database", required=True)
    parser.add_argument("--epoch")
    parser.add_argument("--schema-manifest-digest")
    parser.add_argument("--observed-at", type=float)
    parser.add_argument("--timing-principal-id")
    parser.add_argument("--timing-checkpoint")
    parser.add_argument("--timing-max-samples", type=int)
    parser.add_argument("--materialize-rollback-destination")
    args = parser.parse_args()
    if args.materialize_rollback_destination is not None:
        try:
            _confine_process_resources()
            database = Path(args.database)
            destination = Path(args.materialize_rollback_destination)
            if (
                args.epoch is not None
                or args.schema_manifest_digest is not None
                or args.observed_at is not None
                or args.timing_principal_id is not None
                or args.timing_checkpoint is not None
                or args.timing_max_samples is not None
            ):
                raise ValueError
            _materialize_rollback_snapshot(database, destination)
        except Exception:
            return 2
        return 0
    try:
        _confine_process_resources()
        database = Path(args.database)
        if (
            not database.is_absolute()
            or not isinstance(args.epoch, str)
            or not args.epoch
            or not isinstance(args.schema_manifest_digest, str)
            or not isinstance(args.observed_at, float)
            or not math.isfinite(args.observed_at)
        ):
            raise ValueError
        timing_values = (
            args.timing_principal_id,
            args.timing_checkpoint,
            args.timing_max_samples,
        )
        if any(value is not None for value in timing_values) and not all(
            value is not None for value in timing_values
        ):
            raise ValueError
        timing_requested = args.timing_principal_id is not None
        if timing_requested and (
            not isinstance(args.timing_principal_id, str)
            or not args.timing_principal_id
            or len(args.timing_principal_id) > 256
            or args.timing_checkpoint not in _DAILY_WORKSPACE_CHECKPOINTS
            or type(args.timing_max_samples) is not int
            or not 1 <= args.timing_max_samples <= _MAX_TIMING_SAMPLES
        ):
            raise ValueError
        generation_before = _store_generation(database)
        wal_present = generation_before[1] is not None
        shm_present = generation_before[2] is not None
        journal_present = generation_before[3] is not None
        if not wal_present and not shm_present and not journal_present:
            uri = f"{database.as_uri()}?mode=ro&immutable=1"
        elif wal_present and shm_present and not journal_present:
            uri = f"{database.as_uri()}?mode=ro"
        else:
            raise ValueError
        with sqlite3.connect(
            uri,
            uri=True,
            timeout=5.0,
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            schema_version_before = _pragma_int(connection, "schema_version")
            actual_digest = (
                "sha256:"
                + hashlib.sha256(_canonical_json(_schema_manifest(connection))).hexdigest()
            )
            if not hmac.compare_digest(actual_digest, args.schema_manifest_digest):
                raise ValueError
            _require_integrity(connection)
            if timing_requested:
                samples = _daily_workspace_timing_samples(
                    connection,
                    principal_id=args.timing_principal_id,
                    checkpoint=args.timing_checkpoint,
                    max_samples=args.timing_max_samples,
                )
            else:
                counts = _validated_terminal_counts(
                    connection,
                    epoch=args.epoch,
                    observed_at=args.observed_at,
                )
            if _pragma_int(connection, "schema_version") != schema_version_before:
                raise ValueError
        if _store_generation(database) != generation_before:
            raise ValueError
        payload = (
            {
                "schema_version": _TIMING_OUTPUT_SCHEMA,
                "status": "ok",
                "checkpoint": args.timing_checkpoint,
                "samples": samples,
            }
            if timing_requested
            else {
                "schema_version": _OUTPUT_SCHEMA,
                "status": "ok",
                "total_jobs": counts[0],
                "active_jobs": counts[1],
                "expired_jobs": counts[2],
                "terminal_jobs": counts[3],
                "uncoordinated_terminal_jobs": counts[4],
            }
        )
    except Exception:
        payload = {
            "schema_version": _OUTPUT_SCHEMA,
            "status": "error",
            "error": "semantic_state_snapshot_unavailable",
        }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload["status"] == "ok" else 2


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _schema_manifest(connection: sqlite3.Connection) -> dict[str, object]:
    """Recompute the snapshot schema contract from read-only SQLite catalogs."""
    table_name_query = """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
            ORDER BY name
        """
    table_name_rows = connection.execute(table_name_query)
    table_names = [str(row[0]) for row in table_name_rows]
    tables: list[dict[str, object]] = []
    for table_name in table_names:
        table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if table_sql_row is None or not isinstance(table_sql_row[0], str):
            raise ValueError
        columns = [
            {
                "cid": int(row[0]),
                "name": str(row[1]),
                "type": str(row[2]),
                "notnull": int(row[3]),
                "default": row[4],
                "pk": int(row[5]),
                "hidden": int(row[6]),
            }
            for row in connection.execute(
                """
                SELECT cid, name, type, "notnull", dflt_value, pk, hidden
                FROM pragma_table_xinfo(?)
                ORDER BY cid
                """,
                (table_name,),
            )
        ]
        indexes: list[dict[str, object]] = []
        for index_row in connection.execute(
            """
            SELECT name, "unique", origin, partial
            FROM pragma_index_list(?)
            ORDER BY name
            """,
            (table_name,),
        ):
            index_name = str(index_row[0])
            index_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            indexes.append(
                {
                    "name": index_name,
                    "unique": int(index_row[1]),
                    "origin": str(index_row[2]),
                    "partial": int(index_row[3]),
                    "sql": None if index_sql_row is None else index_sql_row[0],
                    "columns": [
                        {
                            "seqno": int(column[0]),
                            "cid": int(column[1]),
                            "name": column[2],
                            "desc": int(column[3]),
                            "collation": str(column[4]),
                            "key": int(column[5]),
                        }
                        for column in connection.execute(
                            """
                            SELECT seqno, cid, name, "desc", coll, key
                            FROM pragma_index_xinfo(?)
                            ORDER BY seqno
                            """,
                            (index_name,),
                        )
                    ],
                }
            )
        foreign_keys = [
            {
                "id": int(row[0]),
                "seq": int(row[1]),
                "table": str(row[2]),
                "from": str(row[3]),
                "to": str(row[4]),
                "on_update": str(row[5]),
                "on_delete": str(row[6]),
                "match": str(row[7]),
            }
            for row in connection.execute(
                """
                SELECT id, seq, "table", "from", "to", on_update, on_delete, match
                FROM pragma_foreign_key_list(?)
                ORDER BY id, seq
                """,
                (table_name,),
            )
        ]
        tables.append(
            {
                "name": table_name,
                "sql": table_sql_row[0],
                "columns": columns,
                "indexes": indexes,
                "foreign_keys": foreign_keys,
            }
        )
    meta_query = """
            SELECT id, schema_name, schema_version, epoch
            FROM semantic_state_meta
            ORDER BY id
        """
    meta_rows = connection.execute(meta_query)
    meta = [
        {
            "id": row[0],
            "schema_name": row[1],
            "schema_version": row[2],
            "epoch": row[3],
        }
        for row in meta_rows
    ]
    schema_object_query = """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('view', 'trigger') AND name NOT GLOB 'sqlite_*'
            ORDER BY type, name
        """
    schema_object_rows = connection.execute(schema_object_query)
    schema_objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in schema_object_rows
    ]
    return {
        "schema_version": _SCHEMA_MANIFEST_VERSION,
        "pragmas": {name: _pragma_int(connection, name) for name in sorted(_SCHEMA_PRAGMAS)},
        "meta": meta,
        "tables": tables,
        "schema_objects": schema_objects,
    }


def _confine_process_resources() -> None:
    current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = _MAX_ADDRESS_SPACE_BYTES
    if current_soft != resource.RLIM_INFINITY:
        limit = min(limit, current_soft)
    if current_hard != resource.RLIM_INFINITY:
        limit = min(limit, current_hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, current_hard))
    open_soft, open_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    open_limit = _MAX_OPEN_FILES
    if open_soft != resource.RLIM_INFINITY:
        open_limit = min(open_limit, open_soft)
    if open_hard != resource.RLIM_INFINITY:
        open_limit = min(open_limit, open_hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_limit, open_hard))


def _require_owner_store(path: Path) -> tuple[object, ...]:
    parent = path.parent.lstat()
    database = path.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
        or not stat.S_ISREG(database.st_mode)
        or database.st_uid != os.geteuid()
        or database.st_nlink != 1
        or stat.S_IMODE(database.st_mode) != 0o600
        or database.st_size > _MAX_STORE_PART_BYTES
    ):
        raise ValueError
    return _file_content_identity(path, database)


def _store_generation(
    path: Path,
) -> tuple[
    tuple[object, ...],
    tuple[object, ...] | None,
    tuple[object, ...] | None,
    tuple[object, ...] | None,
]:
    return (
        _require_owner_store(path),
        _sidecar_identity(Path(f"{path}-wal")),
        _sidecar_identity(Path(f"{path}-shm")),
        _sidecar_identity(Path(f"{path}-journal")),
    )


def _materialize_rollback_snapshot(database: Path, destination: Path) -> None:
    if (
        not database.is_absolute()
        or not destination.is_absolute()
        or destination.exists()
        or destination.parent.resolve(strict=True) != destination.parent
    ):
        raise ValueError
    destination_parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(destination_parent.st_mode)
        or destination_parent.st_uid != os.geteuid()
        or stat.S_IMODE(destination_parent.st_mode) != 0o700
    ):
        raise ValueError
    generation = _store_generation(database)
    if generation[1] is not None or generation[2] is not None:
        raise ValueError
    journal_identity = generation[3]
    if journal_identity is None:
        raise ValueError
    database_bytes = generation[0][6]
    journal_bytes = journal_identity[6]
    if type(database_bytes) is not int or type(journal_bytes) is not int:
        raise ValueError
    total_bytes = database_bytes + journal_bytes
    if total_bytes > _MAX_MATERIALIZED_BYTES:
        raise ValueError
    destination_journal = Path(f"{destination}-journal")
    _copy_store_part(database, destination, expected=generation[0])
    _copy_store_part(
        Path(f"{database}-journal"),
        destination_journal,
        expected=journal_identity,
    )
    with sqlite3.connect(
        f"{destination.as_uri()}?mode=rw",
        uri=True,
        timeout=5.0,
    ) as connection:
        if connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone() is None:
            raise ValueError
        with suppress(FileNotFoundError):
            destination_journal.unlink()
    materialized = _store_generation(destination)
    if any(part is not None for part in materialized[1:]):
        raise ValueError
    if _store_generation(database) != generation:
        raise ValueError


def _copy_store_part(
    source: Path,
    destination: Path,
    *,
    expected: tuple[object, ...],
) -> None:
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    destination_descriptor = -1
    try:
        before = os.fstat(source_descriptor)
        if _stat_identity(before) != tuple(expected[:-1]):
            raise ValueError
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_STORE_PART_BYTES:
                raise ValueError
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise ValueError
                view = view[written:]
        after = os.fstat(source_descriptor)
        expected_bytes = expected[6]
        if type(expected_bytes) is not int:
            raise ValueError
        if (
            _stat_identity(before) != _stat_identity(after)
            or total != expected_bytes
            or not hmac.compare_digest(digest.hexdigest(), str(expected[-1]))
        ):
            raise ValueError
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _sidecar_identity(path: Path) -> tuple[object, ...] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > _MAX_STORE_PART_BYTES
    ):
        raise ValueError
    return _file_content_identity(path, metadata)


def _file_content_identity(
    path: Path,
    metadata: os.stat_result,
) -> tuple[object, ...]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(metadata):
            raise ValueError
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_STORE_PART_BYTES:
                raise ValueError
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise ValueError
    finally:
        os.close(descriptor)
    return (*_stat_identity(metadata), digest.hexdigest())


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    if name not in _PRAGMA_ALLOWLIST:
        raise ValueError
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or type(row[0]) is not int:
        raise ValueError
    return int(row[0])


def _require_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError
    quick_check = connection.execute("PRAGMA quick_check").fetchall()
    if len(quick_check) != 1 or str(quick_check[0][0]) != "ok":
        raise ValueError


def _validated_terminal_counts(
    connection: sqlite3.Connection,
    *,
    epoch: str,
    observed_at: float,
) -> tuple[int, int, int, int, int]:
    chains = {
        str(row["chain_id"]): row
        for row in connection.execute(
            "SELECT chain_id, principal_id, chain_kind, status FROM chains"
        )
    }
    job_rows = connection.execute("""
            SELECT job_id, chain_id, principal_id, state, product_json,
                   artifact_hash, coordinated_version_no, contract_json,
                   input_json, contract_hash, input_hash, request_hash, deadline_at
            FROM jobs
            """)
    jobs = {str(row["job_id"]): row for row in job_rows}
    continuation_rows = connection.execute("""
            SELECT epoch, principal_id, chain_id, active_job_id
            FROM continuations
            """)
    continuations = {str(row["chain_id"]): row for row in continuation_rows}
    if set(continuations) != set(chains):
        raise ValueError
    for chain_id, continuation in continuations.items():
        chain = chains[chain_id]
        if str(continuation["epoch"]) != epoch or str(continuation["principal_id"]) != str(
            chain["principal_id"]
        ):
            raise ValueError
        active_job_id = continuation["active_job_id"]
        if active_job_id is None:
            continue
        job = jobs.get(str(active_job_id))
        if (
            job is None
            or str(job["chain_id"]) != chain_id
            or str(job["principal_id"]) != str(chain["principal_id"])
            or str(chain["status"]) == "closed"
        ):
            raise ValueError
        state = str(job["state"])
        if state not in _ACTIVE_STATES and not (
            state in _TERMINAL_STATES and job["coordinated_version_no"] is None
        ):
            raise ValueError

    versions: dict[tuple[str, int], sqlite3.Row] = {}
    for row in connection.execute("""
        SELECT chain_id, version_no, kind, job_id, product_id,
               contract_json, input_json, contract_hash, input_hash,
               payload_json
        FROM chain_versions
        """):
        version_no = _exact_positive_int(row["version_no"])
        key = (str(row["chain_id"]), version_no)
        if key in versions:
            raise ValueError
        versions[key] = row
    for row in connection.execute("""
        SELECT chain_id, COUNT(*) AS count, MIN(version_no) AS first_version,
               MAX(version_no) AS last_version
        FROM chain_versions
        GROUP BY chain_id
        """):
        if int(row["first_version"]) != 1 or int(row["last_version"]) != int(row["count"]):
            raise ValueError
    products_by_id: dict[str, sqlite3.Row] = {}
    products_by_job: dict[str, sqlite3.Row] = {}
    for row in connection.execute("""
        SELECT product_id, chain_id, job_id, product_version, status,
               product_json, artifact_hash
        FROM products
        """):
        product_row_id = str(row["product_id"])
        if product_row_id in products_by_id:
            raise ValueError
        _exact_positive_int(row["product_version"])
        _require_product_hash(
            str(row["product_json"]),
            str(row["artifact_hash"]),
        )
        products_by_id[product_row_id] = row
        if row["job_id"] is not None:
            product_job_id = str(row["job_id"])
            if product_job_id in products_by_job:
                raise ValueError
            products_by_job[product_job_id] = row

    pending_job_ids: set[str] = set()
    primary_product_counts: dict[str, int] = {}
    closed_versions_by_chain: dict[str, int] = {}
    answer_versions_by_product: dict[str, sqlite3.Row] = {}
    feedback_version_counts: Counter[tuple[str, str, int, str]] = Counter()
    snapshot_fields = ("contract_json", "input_json", "contract_hash", "input_hash")
    for (chain_id, version_no), version in versions.items():
        chain = chains.get(chain_id)
        if chain is None:
            raise ValueError
        kind = str(version["kind"])
        payload = _json_object(str(version["payload_json"]))
        job_id = str(version["job_id"]) if version["job_id"] is not None else None
        product_id = str(version["product_id"]) if version["product_id"] is not None else None
        snapshot_values = tuple(version[field] for field in snapshot_fields)
        if kind == "research_pending":
            job = jobs.get(job_id or "")
            if (
                job is None
                or job_id in pending_job_ids
                or str(job["chain_id"]) != chain_id
                or product_id is not None
                or any(value is None for value in snapshot_values)
                or snapshot_values != tuple(job[field] for field in snapshot_fields)
                or payload != {"status": "queued"}
            ):
                raise ValueError
            if job_id is None:
                raise ValueError
            pending_job_ids.add(job_id)
            continue
        if kind == "research_terminal":
            job = jobs.get(job_id or "")
            if (
                job is None
                or str(job["chain_id"]) != chain_id
                or any(value is not None for value in snapshot_values)
                or _optional_positive_int(job["coordinated_version_no"]) != version_no
            ):
                raise ValueError
            if product_id is not None:
                _increment(primary_product_counts, product_id)
            continue
        if kind == "closed":
            job = jobs.get(job_id or "") if job_id is not None else None
            if (
                str(chain["status"]) != "closed"
                or product_id is not None
                or any(value is not None for value in snapshot_values)
                or payload != {"status": "closed"}
                or (
                    job_id is not None
                    and (
                        job is None
                        or str(job["chain_id"]) != chain_id
                        or str(job["state"]) not in _TERMINAL_STATES
                    )
                )
            ):
                raise ValueError
            _increment(closed_versions_by_chain, chain_id)
            continue
        if kind == "answer":
            product = products_by_id.get(product_id or "")
            if (
                job_id is not None
                or product is None
                or any(value is None for value in snapshot_values)
                or str(product["chain_id"]) != chain_id
                or payload.get("product_version") != _exact_positive_int(product["product_version"])
                or payload.get("status") != str(product["status"])
            ):
                raise ValueError
            _increment(primary_product_counts, product_id)
            if product_id is None or product_id in answer_versions_by_product:
                raise ValueError
            answer_versions_by_product[product_id] = version
            continue
        if kind == "daily_workspace":
            product = products_by_id.get(product_id or "")
            if (
                job_id is not None
                or product is None
                or any(value is None for value in snapshot_values)
                or str(product["chain_id"]) != chain_id
                or payload.get("product_version") != _exact_positive_int(
                    product["product_version"]
                )
                or payload.get("status") != str(product["status"])
            ):
                raise ValueError
            _increment(primary_product_counts, product_id)
            if product_id is None or product_id in answer_versions_by_product:
                raise ValueError
            answer_versions_by_product[product_id] = version
            continue
        if kind == "feedback":
            product = products_by_id.get(product_id or "")
            product_version = (
                _exact_positive_int(product["product_version"]) if product is not None else None
            )
            disposition = payload.get("disposition")
            if (
                job_id is not None
                or product is None
                or any(value is not None for value in snapshot_values)
                or str(product["chain_id"]) != chain_id
                or set(payload) != {"disposition", "product_version"}
                or payload.get("product_version") != product_version
                or not isinstance(disposition, str)
                or not disposition
            ):
                raise ValueError
            if product_id is None or product_version is None:
                raise ValueError
            feedback_version_counts[(chain_id, product_id, product_version, disposition)] += 1
            continue
        raise ValueError

    if pending_job_ids != set(jobs):
        raise ValueError
    if set(primary_product_counts) != set(products_by_id) or any(
        count != 1 for count in primary_product_counts.values()
    ):
        raise ValueError
    for chain_id, chain in chains.items():
        expected_closed_versions = 1 if str(chain["status"]) == "closed" else 0
        if closed_versions_by_chain.get(chain_id, 0) != expected_closed_versions:
            raise ValueError

    _require_idempotency_relations(
        connection,
        chains=chains,
        jobs=jobs,
        products_by_id=products_by_id,
        answer_versions_by_product=answer_versions_by_product,
    )
    _require_feedback_relations(
        connection,
        products_by_id=products_by_id,
        expected=feedback_version_counts,
    )

    active = 0
    expired = 0
    terminal = 0
    pending = 0
    for job_id, job in jobs.items():
        chain_id = str(job["chain_id"])
        chain = chains.get(chain_id)
        if chain is None or str(job["principal_id"]) != str(chain["principal_id"]):
            raise ValueError
        state = str(job["state"])
        product_json = job["product_json"]
        artifact_hash = job["artifact_hash"]
        product = products_by_job.get(job_id)
        marker = job["coordinated_version_no"]
        continuation = continuations[chain_id]
        active_job_id = continuation["active_job_id"]
        if state in _ACTIVE_STATES:
            deadline_at = float(job["deadline_at"])
            if not math.isfinite(deadline_at):
                raise ValueError
            if deadline_at <= observed_at:
                expired += 1
            else:
                active += 1
            if (
                str(chain["status"]) != "active"
                or str(active_job_id) != job_id
                or marker is not None
                or product_json is not None
                or artifact_hash is not None
            ):
                raise ValueError
            if product is not None:
                raise ValueError
            continue
        if state not in _TERMINAL_STATES:
            raise ValueError
        terminal += 1
        is_product = state in _PRODUCT_STATES
        if is_product:
            if product_json is None or artifact_hash is None:
                raise ValueError
            _require_product_hash(str(product_json), str(artifact_hash))
        elif product_json is not None or artifact_hash is not None:
            raise ValueError
        if marker is None:
            pending += 1
            if (
                str(chain["status"]) != "active"
                or str(active_job_id) != job_id
                or product is not None
            ):
                raise ValueError
            continue
        if active_job_id is not None and str(active_job_id) == job_id:
            raise ValueError
        marker_no = _exact_positive_int(marker)
        marker_version = versions.get((chain_id, marker_no))
        if marker_version is None or str(marker_version["job_id"]) != job_id:
            raise ValueError
        if any(
            marker_version[field] is not None
            for field in ("contract_json", "input_json", "contract_hash", "input_hash")
        ):
            raise ValueError
        kind = str(marker_version["kind"])
        payload = _json_object(str(marker_version["payload_json"]))
        if kind == "closed":
            if (
                state != "cancelled"
                or str(chain["status"]) != "closed"
                or marker_version["product_id"] is not None
                or payload != {"status": "closed"}
                or product is not None
            ):
                raise ValueError
            continue
        if kind != "research_terminal":
            raise ValueError
        if state == "cancelled" and str(chain["status"]) != "closed":
            raise ValueError
        public_status = _public_status(state)
        if is_product:
            if product is None:
                raise ValueError
            product_version = int(product["product_version"])
            if (
                str(product["chain_id"]) != chain_id
                or str(product["status"]) != public_status
                or str(product["product_json"]) != str(product_json)
                or str(product["artifact_hash"]) != str(artifact_hash)
                or str(marker_version["product_id"]) != str(product["product_id"])
                or payload != {"product_version": product_version, "status": public_status}
            ):
                raise ValueError
            _require_product_hash(
                str(product["product_json"]),
                str(product["artifact_hash"]),
            )
        elif (
            product is not None
            or marker_version["product_id"] is not None
            or payload != {"product_version": None, "status": public_status}
        ):
            raise ValueError
    return len(jobs), active, expired, terminal, pending


def _daily_workspace_timing_samples(
    connection: sqlite3.Connection,
    *,
    principal_id: str,
    checkpoint: str,
    max_samples: int,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT c.business_key AS trading_day_id, i.key_hash,
               c.chain_id, i.job_id AS idempotency_job_id,
               p.chain_id AS product_chain_id, p.job_id AS product_job_id,
               p.product_json, p.artifact_hash
        FROM chains AS c
        JOIN idempotency AS i
          ON i.chain_id = c.chain_id AND i.product_id IS NOT NULL
        JOIN products AS p ON p.product_id = i.product_id
        WHERE c.principal_id = ?
          AND c.chain_kind = 'daily_workspace'
          AND i.principal_id = ?
          AND i.capability = 'daily_workspace'
        ORDER BY c.business_key DESC, p.product_version DESC
        LIMIT ?
        """,
        (principal_id, principal_id, max_samples * _TIMING_CANDIDATE_MULTIPLIER),
    ).fetchall()
    samples: list[dict[str, object]] = []
    seen_days: set[str] = set()
    for row in rows:
        trading_day_id = row["trading_day_id"]
        key_hash = row["key_hash"]
        chain_id = row["chain_id"]
        product_chain_id = row["product_chain_id"]
        if (
            not isinstance(trading_day_id, str)
            or not isinstance(key_hash, str)
            or not isinstance(chain_id, str)
            or product_chain_id != chain_id
            or row["idempotency_job_id"] is not None
            or row["product_job_id"] is not None
        ):
            raise ValueError
        expected_key_hash = _hash_text(f"daily:{trading_day_id}:{checkpoint}")
        if not hmac.compare_digest(key_hash.encode("utf-8"), expected_key_hash.encode("utf-8")):
            continue
        _require_product_hash(str(row["product_json"]), str(row["artifact_hash"]))
        sample = _timing_sample_payload(
            _json_object(str(row["product_json"])),
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
        )
        if sample is None:
            continue
        if trading_day_id in seen_days:
            raise ValueError
        seen_days.add(trading_day_id)
        samples.append(sample)
        if len(samples) == max_samples:
            break
    return samples


def _timing_sample_payload(
    product: dict[str, object],
    *,
    trading_day_id: str,
    checkpoint: str,
) -> dict[str, object] | None:
    if (
        product.get("schema_version") != "fin.daily_workspace_product/v1"
        or product.get("origin") != "scheduled"
        or product.get("trading_day_id") != trading_day_id
        or product.get("checkpoint") != checkpoint
    ):
        return None
    provenance = product.get("agent_provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("runtime_invoked_at_generation") is not True
    ):
        return None
    degraded = product.get("degraded")
    timing = product.get("delivery_timing")
    if (
        not isinstance(degraded, bool)
        or not isinstance(timing, dict)
        or timing.get("schema") != "fin.daily-workspace-delivery-timing/v1"
    ):
        return None
    target_at = _parse_timing(timing.get("target_at"))
    prepared_at = _parse_timing(timing.get("prepared_at"))
    generated_at = _parse_timing(timing.get("generated_at"))
    if (
        target_at is None
        or prepared_at is None
        or generated_at is None
        or generated_at < prepared_at
        or any(
            value.date().isoformat() != trading_day_id
            for value in (target_at, prepared_at, generated_at)
        )
    ):
        return None
    return {
        "trading_day_id": trading_day_id,
        "checkpoint": checkpoint,
        "target_at": target_at.isoformat(),
        "prepared_at": prepared_at.isoformat(),
        "generated_at": generated_at.isoformat(),
        "degraded": degraded,
        "agent_runtime_invoked": True,
    }


def _parse_timing(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        return None
    return parsed


def _require_idempotency_relations(
    connection: sqlite3.Connection,
    *,
    chains: dict[str, sqlite3.Row],
    jobs: dict[str, sqlite3.Row],
    products_by_id: dict[str, sqlite3.Row],
    answer_versions_by_product: dict[str, sqlite3.Row],
) -> None:
    job_counts: Counter[str] = Counter()
    answer_counts: Counter[str] = Counter()
    for row in connection.execute("""
        SELECT principal_id, capability, key_hash, request_hash,
               chain_id, job_id, product_id
        FROM idempotency
        """):
        chain_id = str(row["chain_id"])
        principal_id = str(row["principal_id"])
        chain = chains.get(chain_id)
        capability = str(row["capability"])
        if (
            chain is None
            or principal_id != str(chain["principal_id"])
            or capability not in {_CAPABILITY, "daily_workspace"}
            or not _is_sha256(str(row["key_hash"]))
            or not _is_sha256(str(row["request_hash"]))
        ):
            raise ValueError
        if capability == "daily_workspace":
            if row["job_id"] is not None or str(chain["chain_kind"]) != "daily_workspace":
                raise ValueError
            if row["product_id"] is None:
                # chain-only entry (create); no product to bind.
                continue
            product_id = str(row["product_id"])
            product = products_by_id.get(product_id)
            version = answer_versions_by_product.get(product_id)
            if (
                product is None
                or version is None
                or str(product["chain_id"]) != chain_id
                or str(row["request_hash"])
                != _hash_text(
                    f"{version['contract_json']}\x00{version['input_json']}",
                )
            ):
                raise ValueError
            answer_counts[product_id] += 1
            continue
        if row["job_id"] is not None:
            job_id = str(row["job_id"])
            job = jobs.get(job_id)
            if (
                row["product_id"] is not None
                or job is None
                or str(job["chain_id"]) != chain_id
                or str(job["principal_id"]) != principal_id
                or str(job["request_hash"]) != str(row["request_hash"])
            ):
                raise ValueError
            job_counts[job_id] += 1
            continue
        if row["product_id"] is None:
            raise ValueError
        product_id = str(row["product_id"])
        product = products_by_id.get(product_id)
        version = answer_versions_by_product.get(product_id)
        if (
            product is None
            or version is None
            or str(product["chain_id"]) != chain_id
            or str(row["request_hash"])
            != _hash_text(
                f"{version['contract_json']}\x00{version['input_json']}",
            )
        ):
            raise ValueError
        answer_counts[product_id] += 1
    if set(job_counts) != set(jobs) or any(count != 1 for count in job_counts.values()):
        raise ValueError
    if set(answer_counts) != set(answer_versions_by_product) or any(
        count != 1 for count in answer_counts.values()
    ):
        raise ValueError


def _require_feedback_relations(
    connection: sqlite3.Connection,
    *,
    products_by_id: dict[str, sqlite3.Row],
    expected: Counter[tuple[str, str, int, str]],
) -> None:
    products_by_version = {
        (str(row["chain_id"]), _exact_positive_int(row["product_version"])): product_id
        for product_id, row in products_by_id.items()
    }
    observed: Counter[tuple[str, str, int, str]] = Counter()
    for row in connection.execute("""
        SELECT feedback_id, chain_id, product_version, feedback_key_hash,
               disposition, note
        FROM feedback
        """):
        chain_id = str(row["chain_id"])
        product_version = _exact_positive_int(row["product_version"])
        product_id = products_by_version.get((chain_id, product_version))
        disposition = str(row["disposition"])
        if (
            product_id is None
            or not str(row["feedback_id"])
            or not _is_sha256(str(row["feedback_key_hash"]))
            or not disposition
            or len(str(row["note"])) > 4_000
        ):
            raise ValueError
        observed[(chain_id, product_id, product_version, disposition)] += 1
    if observed != expected:
        raise ValueError


def _json_object(raw: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _require_product_hash(raw: str, expected_hash: str) -> None:
    value = _json_object(raw)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if raw.encode() != encoded:
        raise ValueError
    _require_safe_product(value)
    if expected_hash != f"sha256:{hashlib.sha256(encoded).hexdigest()}":
        raise ValueError


def _require_safe_product(value: object) -> None:
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _FORBIDDEN_PRODUCT_FIELDS:
                raise ValueError
            _require_safe_product(nested)
    elif isinstance(value, list):
        for nested in value:
            _require_safe_product(nested)


def _exact_positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError
    return int(value)


def _optional_positive_int(value: object) -> int | None:
    return None if value is None else _exact_positive_int(value)


def _increment(counts: dict[str, int], key: str | None) -> None:
    if key is None:
        raise ValueError
    counts[key] = counts.get(key, 0) + 1


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _public_status(state: str) -> str:
    if state == "succeeded":
        return "completed"
    if state == "partial":
        return "partial"
    if state in {"failed", "timed_out"}:
        return "unavailable"
    if state == "cancelled":
        return "cancelled"
    raise ValueError


if __name__ == "__main__":
    raise SystemExit(main())
