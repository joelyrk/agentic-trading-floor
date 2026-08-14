"""Safe backup, restore, integrity, and retention commands for the SQLite store."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import database
from backend.migrations import current_version, migrate


def integrity_check(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"database does not exist: {path}")
    with sqlite3.connect(path) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise ValueError(f"database integrity check failed: {result}")


def backup(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("backup destination must differ from the source")
    integrity_check(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"backup already exists: {destination}")
    with (
        sqlite3.connect(source) as source_conn,
        sqlite3.connect(destination) as backup_conn,
    ):
        source_conn.backup(backup_conn)
    os.chmod(destination, 0o600)
    integrity_check(destination)


def restore(source: Path, destination: Path, *, overwrite: bool = False) -> None:
    integrity_check(source)
    if destination.exists() and not overwrite:
        raise FileExistsError("restore target exists; pass --overwrite after taking a backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restore.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with (
            sqlite3.connect(source) as source_conn,
            sqlite3.connect(temporary) as target_conn,
        ):
            source_conn.backup(target_conn)
        integrity_check(temporary)
        migrate(str(temporary))
        temporary.replace(destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def prune(path: Path, retention_days: int, *, now: datetime | None = None) -> dict[str, int]:
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    cutoff_text = cutoff.isoformat()
    deleted: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        queries = {
            "logs": "DELETE FROM logs WHERE datetime < ?",
            "valuations": (
                "DELETE FROM market_observations WHERE usage_kind='valuation' AND recorded_at < ?"
            ),
            "cycles": (
                "DELETE FROM cycle_metrics WHERE completed_at IS NOT NULL AND completed_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM decision_telemetry dt "
                "WHERE dt.cycle_id=cycle_metrics.cycle_id)"
            ),
            "replays": "DELETE FROM replay_sessions WHERE revealed_at IS NOT NULL AND revealed_at < ?",
        }
        for name, query in queries.items():
            deleted[name] = conn.execute(query, (cutoff_text,)).rowcount
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Administer the paper-trading SQLite database")
    parser.add_argument("--database", type=Path, default=Path(database.DB))
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("destination", type=Path)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("source", type=Path)
    restore_parser.add_argument("--overwrite", action="store_true")
    commands.add_parser("check")
    prune_parser = commands.add_parser("prune")
    prune_parser.add_argument("--retention-days", type=int, default=90)
    args = parser.parse_args()
    if args.command == "backup":
        backup(args.database, args.destination)
        print(f"backup verified: {args.destination}")
    elif args.command == "restore":
        restore(args.source, args.database, overwrite=args.overwrite)
        print(f"restore verified at schema version {current_version(str(args.database))}")
    elif args.command == "check":
        integrity_check(args.database)
        print(f"integrity ok; schema version {current_version(str(args.database))}")
    else:
        print(prune(args.database, args.retention_days))


if __name__ == "__main__":
    main()
