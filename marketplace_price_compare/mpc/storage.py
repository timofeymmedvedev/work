"""Чекпоинт результатов в SQLite.

Полный прогон по 3 872 товарам на трёх площадках занимает часы, и падение
на середине не должно стоить всей собранной выдачи. Каждый ответ площадки
пишется сразу, повторный запуск пропускает уже собранное.
"""

import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    row_id     INTEGER NOT NULL,
    source     TEXT    NOT NULL,
    query      TEXT,
    avg_price  REAL,
    match_count INTEGER,
    min_price  REAL,
    max_price  REAL,
    status     TEXT,
    offers     TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (row_id, source)
);
"""


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def done_keys(self):
        """Пары (row_id, source), которые уже успешно собраны."""
        cur = self.conn.execute(
            "SELECT row_id, source FROM results WHERE status = 'ok'"
        )
        return {(r[0], r[1]) for r in cur.fetchall()}

    def save(self, row_id, source, query, summary, status, offers=None):
        self.conn.execute(
            """INSERT OR REPLACE INTO results
               (row_id, source, query, avg_price, match_count,
                min_price, max_price, status, offers)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                row_id,
                source,
                query,
                summary.get("avg"),
                summary.get("count", 0),
                summary.get("min"),
                summary.get("max"),
                status,
                json.dumps(offers or [], ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def load_all(self):
        """{(row_id, source): {...}} для сборки итогового файла."""
        cur = self.conn.execute(
            """SELECT row_id, source, avg_price, match_count,
                      min_price, max_price, status FROM results"""
        )
        out = {}
        for row_id, source, avg, cnt, lo, hi, status in cur.fetchall():
            out[(row_id, source)] = {
                "avg": avg,
                "count": cnt,
                "min": lo,
                "max": hi,
                "status": status,
            }
        return out

    def stats(self):
        cur = self.conn.execute(
            "SELECT source, status, COUNT(*) FROM results GROUP BY source, status"
        )
        return cur.fetchall()

    def close(self):
        self.conn.close()
