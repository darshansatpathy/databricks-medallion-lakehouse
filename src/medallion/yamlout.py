"""A tiny deterministic YAML writer for the bundle files (dicts, lists, strings, numbers, bools)."""

from __future__ import annotations

import json
import re
from typing import Any

_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]*$")


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in {
        "true",
        "false",
        "null",
        "yes",
        "no",
        "on",
        "off",
        "~",
    }:
        return s
    return json.dumps(s, ensure_ascii=False)


def dump(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict | list) and v:
                lines.append(f"{pad}{k}:")
                lines.append(dump(v, indent + 1).rstrip("\n"))
            elif isinstance(v, dict | list):
                lines.append(f"{pad}{k}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item:
                inner = dump(item, indent + 1).rstrip("\n").split("\n")
                lines.append(f"{pad}- {inner[0].lstrip()}")
                lines.extend(inner[1:])
            elif isinstance(item, list) and item:
                lines.append(f"{pad}-")
                lines.append(dump(item, indent + 1).rstrip("\n"))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(data)}")
    return "\n".join(lines) + "\n"
