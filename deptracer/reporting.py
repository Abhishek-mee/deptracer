"""Human and JSON-lines output for CI and interactive use."""

from __future__ import annotations

import json
import sys


class Reporter:
    COLORS = {
        "info": "\033[34m",
        "success": "\033[92m",
        "warning": "\033[93m",
        "error": "\033[91m",
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def __init__(self, output_format="human", stream=None):
        self.output_format = output_format
        self.stream = stream or sys.stdout

    @property
    def is_json(self):
        return self.output_format == "json"

    def emit(self, level, stage, message, **details):
        if self.is_json:
            payload = {"level": level, "stage": stage, "message": message}
            if details:
                payload["details"] = details
            print(json.dumps(payload, sort_keys=True), file=self.stream, flush=True)
            return
        color = self.COLORS.get(level, "")
        print(
            f"{color}{self.BOLD}[{stage}]{self.RESET} {message}",
            file=self.stream,
            flush=True,
        )

    def info(self, stage, message, **details):
        self.emit("info", stage, message, **details)

    def success(self, stage, message, **details):
        self.emit("success", stage, message, **details)

    def warning(self, stage, message, **details):
        self.emit("warning", stage, message, **details)

    def error(self, stage, message, **details):
        self.emit("error", stage, message, **details)

