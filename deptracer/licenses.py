"""Best-effort redistribution risk detection for bundled dependencies."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path


SPDX_COPYLEFT = re.compile(
    rb"SPDX-License-Identifier:\s*((?:A?GPL|LGPL)(?:-[0-9.]+)?(?:-only|-or-later)?)",
    re.IGNORECASE,
)
COPYLEFT_NOTICE = re.compile(
    rb"(?:This (?:program|library) is free software.{0,300})?(GNU\s+(LESSER\s+)?GENERAL\s+PUBLIC\s+LICENSE)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class LicenseFinding:
    path: str
    risk: str
    evidence: str

    def as_dict(self):
        return asdict(self)


def inspect_binary(path):
    path = Path(path).resolve()
    candidates = [
        path,
        path.with_name(path.name + ".LICENSE"),
        path.with_name(path.name + ".license"),
        path.with_name("LICENSE." + path.name),
        path.with_name("COPYING." + path.name),
    ]
    for candidate in candidates:
        try:
            content = candidate.read_bytes()[:2_000_000]
        except OSError:
            continue
        match = SPDX_COPYLEFT.search(content)
        if not match and candidate != path:
            match = COPYLEFT_NOTICE.search(content[:4096])
        elif not match and candidate == path:
            notice = COPYLEFT_NOTICE.search(content)
            if notice and b"free software" in notice.group(0).lower():
                match = notice
        if match:
            token = match.group(0).decode("ascii", errors="replace")
            risk = "lgpl" if "LESSER" in token.upper() or token.upper().startswith("LGPL") else "gpl"
            return LicenseFinding(str(path), risk, f"Matched {token!r} in {candidate}")
    return None


def scan_dependencies(paths):
    return [finding for path in paths if (finding := inspect_binary(path))]

