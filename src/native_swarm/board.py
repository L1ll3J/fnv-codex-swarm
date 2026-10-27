"""Append-only collaboration board, including atomic experiment reservations."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from .operator_store import now


class Board:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, member TEXT NOT NULL,
                kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS experiments (
                digest TEXT PRIMARY KEY, member TEXT NOT NULL, at TEXT NOT NULL);""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def post(self, member, kind, body):
        if kind not in (
            "observation",
            "hypothesis",
            "question",
            "reservation",
            "result",
            "handoff",
        ):
            raise ValueError("Unknown board entry kind.")
        text = json.dumps(body)
        if len(text.encode()) > 16384:
            raise ValueError(
                "Keep board entries below 16KB; reference candidate files by hash."
            )
        with self.connect() as db:
            db.execute(
                "INSERT INTO entries(at,member,kind,body) VALUES(?,?,?,?)",
                (now(), member, kind, text),
            )

    def reserve(self, member, digest):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO experiments VALUES(?,?,?)",
                (digest, member, now()),
            )
            return (
                db.execute(
                    "SELECT member FROM experiments WHERE digest=?", (digest,)
                ).fetchone()[0]
                == member
            )

    def entries(self, after=0, latest=False):
        with self.connect() as db:
            order = "DESC" if latest else "ASC"
            rows = list(
                db.execute(
                    f"SELECT * FROM entries WHERE id>? ORDER BY id {order} LIMIT 200",
                    (after,),
                )
            )
            if latest:
                rows.reverse()
            return [
                dict(zip(("id", "at", "member", "kind", "body"), row)) for row in rows
            ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assignment", type=Path, required=True)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument(
        "--kind", choices=["observation", "hypothesis", "question", "handoff"]
    )
    parser.add_argument("--text")
    args = parser.parse_args()
    assignment = json.loads(args.assignment.read_text())
    group = assignment["collaboration"]
    board = Board(group["board"])
    if args.kind:
        board.post(group["member"], args.kind, args.text or "")
    from .shared_status import status

    canonical = json.loads(Path(group["canonical_assignment"]).read_text())
    print(
        json.dumps(
            {
                "status": status(canonical["claim_dir"]),
                "entries": board.entries(args.after, latest=args.after == 0),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
