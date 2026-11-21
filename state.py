import json
import sqlite3
from pathlib import Path

from scanning import COOLDOWN, DAY, QUARTER, Candidate, select


SCHEMA = '''
CREATE TABLE IF NOT EXISTS cursor (
    symbol TEXT PRIMARY KEY,
    bar INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alert (
    symbol TEXT NOT NULL,
    bar INTEGER NOT NULL,
    category TEXT NOT NULL,
    direction TEXT NOT NULL,
    family TEXT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (symbol, bar)
);
CREATE INDEX IF NOT EXISTS alert_time ON alert (bar);
CREATE TABLE IF NOT EXISTS digest (
    id INTEGER PRIMARY KEY,
    created INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery (
    digest_id INTEGER NOT NULL REFERENCES digest (id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    receipt TEXT,
    PRIMARY KEY (digest_id, channel)
);
CREATE TABLE IF NOT EXISTS daily_ai (
    day INTEGER PRIMARY KEY,
    attempts INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scan_log (
    started INTEGER PRIMARY KEY,
    result TEXT NOT NULL
);
'''


class State:
    def __init__(self, path: str):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def cursors(self) -> dict[str, int]:
        return {row['symbol']: row['bar'] for row in self.db.execute('SELECT symbol, bar FROM cursor')}

    def record(self, cursors, candidates, threshold, channels, now, render) -> tuple[list[Candidate], int | None]:
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            previous = self.cursors()
            new_candidates = [item for item in candidates if item.signal.t > previous.get(item.signal.symbol, -1)]
            accepted = []
            if new_candidates:
                earliest = min(item.signal.t for item in new_candidates)
                recent = list(self.db.execute('SELECT * FROM alert WHERE bar > ?', (earliest - COOLDOWN,)))
                last = {}
                cohorts = {}
                for row in recent:
                    last[row['symbol']] = max(last.get(row['symbol'], -1), row['bar'])
                    key = (row['bar'], row['category'], row['direction'])
                    cohorts[key] = cohorts.get(key, 0) + 1
                accepted = select(new_candidates, threshold, last, cohorts)

            for item in accepted:
                signal = item.signal
                self.db.execute('INSERT INTO alert VALUES (?, ?, ?, ?, ?, ?)', (
                    signal.symbol, signal.t, item.market.category, signal.direction, signal.family, signal.score,
                ))
            self.db.executemany('''INSERT INTO cursor VALUES (?, ?)
                ON CONFLICT(symbol) DO UPDATE SET bar = max(bar, excluded.bar)''', cursors.items())

            digest_id = None
            if accepted and channels:
                title, body = render(accepted)
                digest_id = self.db.execute('INSERT INTO digest(created, title, body) VALUES (?, ?, ?)',
                                            (now, title, body)).lastrowid
                self.db.executemany('INSERT INTO delivery (digest_id, channel) VALUES (?, ?)',
                                    ((digest_id, channel) for channel in channels))
        return accepted, digest_id

    def note_scan(self, now: int, summary: dict):
        self.db.execute('INSERT OR REPLACE INTO scan_log VALUES (?, ?)', (now, json.dumps(summary, ensure_ascii=False)))
        self.db.execute('DELETE FROM scan_log WHERE started < ?', (now - 7 * DAY,))
        self.db.execute('DELETE FROM alert WHERE bar < ?', (now - 14 * DAY,))
        self.db.execute('DELETE FROM cursor WHERE bar < ?', (now - 14 * DAY,))
        self.db.execute('DELETE FROM digest WHERE created < ?', (now - 7 * DAY,))
        self.db.execute('DELETE FROM daily_ai WHERE day < ?', (now // DAY - 30,))

    def reserve_ai(self, now: int, cap: int) -> bool:
        if cap <= 0:
            return False
        result = self.db.execute('''INSERT INTO daily_ai (day, attempts) VALUES (?, 1)
            ON CONFLICT(day) DO UPDATE SET attempts = attempts + 1 WHERE attempts < ?''', (now // DAY, cap))
        return result.rowcount == 1

    def note_ai_usage(self, now: int, usage: dict[str, int]):
        self.db.execute('''UPDATE daily_ai SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?
            WHERE day = ?''', (usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0), now // DAY))

    def append_summary(self, digest_id: int, summary: str):
        self.db.execute('''UPDATE digest SET body = body || ? WHERE id = ? AND NOT EXISTS (
            SELECT 1 FROM delivery WHERE digest_id = ? AND status <> 'pending')''',
            ('\n\nDeepSeek 摘要\n' + summary, digest_id, digest_id))

    def pending(self, now: int):
        self.db.execute('''UPDATE delivery SET status = 'expired' WHERE status = 'pending' AND digest_id IN (
            SELECT id FROM digest WHERE created < ?)''', (now - 4 * QUARTER,))
        return list(self.db.execute('''SELECT id, channel, title, body FROM delivery
            JOIN digest ON id = digest_id WHERE status = 'pending' ORDER BY id, channel'''))

    def claim(self, digest_id: int, channel: str) -> bool:
        return self.db.execute('''UPDATE delivery SET status = 'attempting'
            WHERE digest_id = ? AND channel = ? AND status = 'pending' ''', (digest_id, channel)).rowcount == 1

    def finish(self, digest_id: int, channel: str, status: str, receipt: str):
        self.db.execute('''UPDATE delivery SET status = ?, receipt = ?
            WHERE digest_id = ? AND channel = ? AND status = 'attempting' ''', (status, receipt, digest_id, channel))

    def report(self) -> dict:
        latest = self.db.execute('SELECT started, result FROM scan_log ORDER BY started DESC LIMIT 1').fetchone()
        return {
            'last_scan': None if not latest else {'at': latest['started'], **json.loads(latest['result'])},
            'delivery_statuses': {row[0]: row[1] for row in self.db.execute('SELECT status, count(*) FROM delivery GROUP BY status')},
            'delivery_receipts': [dict(row) for row in self.db.execute('''SELECT digest_id, channel, status, receipt
                FROM delivery ORDER BY digest_id DESC, channel LIMIT 12''')],
            'recent_ai_usage': [dict(row) for row in self.db.execute('SELECT * FROM daily_ai ORDER BY day DESC LIMIT 7')],
        }
