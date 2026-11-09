"""Create the SQLite database from sql/schema.sql and load the exported CSV files.

Loading from the CSV exports (not from the generator's memory) proves the exports are
complete and keep every foreign key intact.

Usage:
    python -m src.database --init --scale full
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .common import DATA_DIR, TABLE_ORDER, connect, create_schema


def db_path_for(scale: str, data_root: Path = DATA_DIR) -> Path:
    return Path(data_root) / f"academic_integrity_{scale}.db"


def init_db(scale: str, data_root: Path = DATA_DIR, db_path: Path | None = None) -> Path:
    data_root = Path(data_root)
    csv_dir = data_root / "generated" / scale / "csv"
    if not csv_dir.exists():
        raise SystemExit(f"No generated data in {csv_dir}. Run: python -m src.generate_data --scale {scale}")
    db_path = Path(db_path or db_path_for(scale, data_root))
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    create_schema(conn)
    for table in TABLE_ORDER:
        with open(csv_dir / f"{table}.csv", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            cols = next(reader)
            rows = [[None if v == "" else v for v in row] for row in reader]
        placeholders = ",".join("?" * len(cols))
        conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--init", action="store_true", help="(re)create the database and load the CSVs")
    parser.add_argument("--scale", choices=["starter", "full"], default="starter")
    args = parser.parse_args()
    if not args.init:
        parser.error("nothing to do; pass --init")
    path = init_db(args.scale)
    conn = connect(path)
    print(f"Created {path.relative_to(DATA_DIR.parent)}")
    for table in TABLE_ORDER:
        print(f"  {table:<20} {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>6}")


if __name__ == "__main__":
    main()
