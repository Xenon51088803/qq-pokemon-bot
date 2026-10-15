"""Real v1/v2 schema fixtures; never downgrade a current schema using a PRAGMA."""
import sqlite3
from collections import defaultdict


def make_legacy(path, version=1, rows=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = rows or [("g", "u", f"2026-01-0{i + 1}", species) for i, species in enumerate((1, 4, 7, 7))]
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE draws (group_id TEXT NOT NULL, member_id TEXT NOT NULL, day TEXT NOT NULL,
                pokemon_id INTEGER NOT NULL CHECK(pokemon_id BETWEEN 1 AND 151), PRIMARY KEY(group_id,member_id,day));
            CREATE TABLE collection (group_id TEXT NOT NULL, member_id TEXT NOT NULL,
                pokemon_id INTEGER NOT NULL CHECK(pokemon_id BETWEEN 1 AND 151),
                first_day TEXT NOT NULL, last_day TEXT NOT NULL, count INTEGER NOT NULL CHECK(count>0),
                PRIMARY KEY(group_id,member_id,pokemon_id));
            CREATE TABLE processed_events (group_id TEXT NOT NULL, message_id TEXT NOT NULL, processed_at REAL NOT NULL,
                PRIMARY KEY(group_id,message_id));
        """)
        conn.executemany("INSERT INTO draws VALUES (?,?,?,?)", rows)
        grouped = defaultdict(list)
        for group, member, day, species in rows:
            grouped[group, member, species].append(day)
        conn.executemany("INSERT INTO collection VALUES (?,?,?,?,?,?)",
                         [(g, m, p, min(days), max(days), len(days)) for (g, m, p), days in grouped.items()])
        conn.execute("INSERT INTO processed_events VALUES ('g','old-event',1)")
        if version == 2:
            conn.execute("""CREATE TABLE achievements (group_id TEXT NOT NULL, member_id TEXT NOT NULL, achievement_id TEXT NOT NULL,
                          unlocked_day TEXT NOT NULL, PRIMARY KEY(group_id,member_id,achievement_id))""")
            conn.execute("INSERT INTO achievements VALUES ('g','u','first_capture','2026-01-01')")
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
    finally:
        conn.close()
