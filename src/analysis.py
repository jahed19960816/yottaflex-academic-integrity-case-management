"""Run sql/analysis_queries.sql against the generated database and print the results.

Usage:
    python -m src.analysis --scale full
"""
from __future__ import annotations

import argparse

from .common import ROOT, connect
from .database import db_path_for, init_db


def load_queries() -> list[tuple[str, str]]:
    text = (ROOT / "sql" / "analysis_queries.sql").read_text(encoding="utf-8")
    queries = []
    for block in text.split("-- name:")[1:]:
        name, _, sql = block.partition("\n")
        queries.append((name.strip(), sql.strip().rstrip(";")))
    return queries


def print_table(cols, rows) -> None:
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c)) for i, c in enumerate(cols)]
    print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=["starter", "full"], default="starter")
    args = parser.parse_args()
    path = db_path_for(args.scale)
    if not path.exists():
        init_db(args.scale)
    conn = connect(path)
    for name, sql in load_queries():
        cur = conn.execute(sql)
        print(f"\n== {name}")
        print_table([d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()])


if __name__ == "__main__":
    main()
