"""Display structured diagnostics for the most recent pipeline run."""

from __future__ import annotations

import json

from .state import StateStore


def explain_problem(project_dir=".", output_format="human"):
    state = StateStore(project_dir).load()
    if not state:
        message = "No valid deptracer state was found for this project."
        if output_format == "json":
            print(json.dumps({"status": "unknown", "message": message}))
        else:
            print(f"[EXPLAIN] {message}")
        return False

    if output_format == "json":
        print(json.dumps(state, indent=2, sort_keys=True))
        return state.get("status") == "success"

    print(f"Project: {state.get('project_dir')}")
    print(f"Status: {state.get('status')}  Stage: {state.get('stage')}  Iteration: {state.get('iteration')}")
    history = state.get("history", [])
    if not history:
        print("No iteration history was recorded.")
        return False
    print("\nIteration history:")
    for event in history:
        print(
            f"- {event.get('time')} | iteration {event.get('iteration')} | "
            f"{event.get('stage')} | {event.get('status')}: {event.get('message')}"
        )
        fix = event.get("details", {}).get("fix")
        if fix:
            print(f"  Recommended action: {fix}")
    return state.get("status") == "success"
