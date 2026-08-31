import json
import os
import sqlite3
from datetime import datetime, UTC


class HarnessWarehouse:
    """Manages storage and historical analysis of model evaluation runs."""

    def __init__(self, db_path: str = "scorecards/evaluation_history.db"):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            # sqlite3.connect fails with OperationalError when the parent
            # directory does not exist (e.g. a fresh checkout without
            # scorecards/) — create it so the first run never crashes on init.
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    model_name TEXT,
                    agent_role TEXT,
                    raw_output TEXT,
                    scores TEXT
                )
            """)
            conn.commit()

    def log_run(self, model_name: str, agent_role: str, raw_output: str, scores: dict):
        # Guard against empty artifacts: a dropped connection or failed run
        # must not write a blank row into the history DB, where it would
        # silently skew scorecard averages and trend queries.
        if not raw_output or not raw_output.strip():
            print(
                "   ⚠️  Warehouse refused to log empty raw_output "
                f"(role={agent_role!r}); skipping insert."
            )
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO evaluation_runs (timestamp, model_name, agent_role, raw_output, scores) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    model_name,
                    agent_role,
                    raw_output,
                    json.dumps(scores),
                ),
            )
            conn.commit()
