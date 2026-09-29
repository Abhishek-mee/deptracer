"""Project-local structured pipeline state and iteration history."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir).resolve()
        self.directory = self.project_dir / ".deptracer"
        self.path = self.directory / "state.json"

    def load(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("schema_version") != SCHEMA_VERSION:
            return None
        return data

    def new(self, binary_name, spec_name, options):
        return {
            "schema_version": SCHEMA_VERSION,
            "project_dir": str(self.project_dir),
            "binary_name": binary_name,
            "spec_name": spec_name,
            "status": "running",
            "stage": "SETUP",
            "started_at": _timestamp(),
            "updated_at": _timestamp(),
            "iteration": 1,
            "injected": [],
            "system_noise": [],
            "options": options,
            "history": [],
        }

    def save(self, state):
        self.directory.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _timestamp()
        fd, temporary = tempfile.mkstemp(
            prefix="state-",
            suffix=".json.tmp",
            dir=str(self.directory),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def event(self, state, stage, status, message, **details):
        event = {
            "time": _timestamp(),
            "iteration": state.get("iteration"),
            "stage": stage,
            "status": status,
            "message": message,
        }
        if details:
            event["details"] = details
        state["stage"] = stage
        state["status"] = status
        state["history"].append(event)
        self.save(state)
        return event

