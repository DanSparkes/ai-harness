import sqlite3
import json
from datetime import datetime

class HarnessWarehouse:
    """Manages storage and historical analysis of model evaluation runs."""
    def __init__(self, db_path: str = "scorecards/evaluation_history.db"):
        self.db_path = db_path
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
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO evaluation_runs (timestamp, model_name, agent_role, raw_output, scores) VALUES (?, ?, ?, ?, ?)",
                (datetime.utcnow().isoformat(), model_name, agent_role, raw_output, json.dumps(scores))
            )
            conn.commit()
