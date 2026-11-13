from __future__ import annotations

import json
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"


def _val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise TypeError(type(v))


def _is_table_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _key(k: str) -> str:
    return k if k.replace("_", "a").replace("-", "a").isalnum() else json.dumps(k)


def dumps(data: dict[str, Any]) -> str:
    """A small TOML writer for spec-shaped data."""
    out: list[str] = []

    def emit(path: str, table: dict[str, Any], array: bool) -> None:
        out.append(f"[[{path}]]" if array else f"[{path}]")
        for k, v in table.items():
            if not isinstance(v, dict) and not _is_table_list(v):
                out.append(f"{_key(k)} = {_val(v)}")
        for k, v in table.items():
            if isinstance(v, dict):
                emit(f"{path}.{_key(k)}", v, False)
            elif _is_table_list(v):
                for item in v:
                    emit(f"{path}.{_key(k)}", item, True)
        out.append("")

    for k, v in data.items():
        if isinstance(v, dict):
            emit(_key(k), v, False)
        elif _is_table_list(v):
            for item in v:
                emit(_key(k), item, True)
    return "\n".join(out)


class Draft:
    """The shipped specification as plain data, for tests to edit before writing it out."""

    def __init__(self) -> None:
        self.platform: dict[str, Any] = tomllib.loads((SPEC / "platform.toml").read_text())
        self.files: dict[str, dict[str, Any]] = {
            f.name: tomllib.loads(f.read_text()) for f in sorted((SPEC / "tables").glob("*.toml"))
        }
        self.extra: dict[str, str] = {}

    def table(self, fqn: str) -> dict[str, Any]:
        layer, name = fqn.split(".")
        for d in self.files.values():
            if d["table"]["layer"] == layer and d["table"]["name"] == name:
                return d
        raise KeyError(fqn)

    def col(self, fqn: str, name: str) -> dict[str, Any]:
        return next(c for c in self.table(fqn)["columns"] if c["name"] == name)

    def exp(self, fqn: str, name: str) -> dict[str, Any]:
        return next(e for e in self.table(fqn)["expectations"] if e["name"] == name)

    def grant(self, group: str, schema: str) -> dict[str, Any]:
        return next(
            g for g in self.platform["grants"] if g["group"] == group and g["schema"] == schema
        )

    def write(self, root: Path) -> Path:
        spec_dir = root / "spec"
        (spec_dir / "tables").mkdir(parents=True)
        (spec_dir / "platform.toml").write_text(dumps(self.platform), encoding="utf-8")
        for name, data in self.files.items():
            (spec_dir / "tables" / name).write_text(dumps(data), encoding="utf-8")
        shutil.copytree(SPEC / "data", spec_dir / "data")
        for rel, text in self.extra.items():
            (spec_dir / rel).write_text(text, encoding="utf-8")
        return spec_dir


@pytest.fixture
def make_spec(tmp_path: Path) -> Callable[..., Path]:
    """Write a mutated copy of the shipped spec and return the spec directory."""
    counter = iter(range(1000))

    def _make(mutate: Callable[[Draft], None] | None = None) -> Path:
        d = Draft()
        if mutate:
            mutate(d)
        return d.write(tmp_path / f"s{next(counter)}")

    return _make


@pytest.fixture(scope="session")
def lakehouse():  # type: ignore[no-untyped-def]
    from medallion import spec

    lh, problems = spec.load(SPEC)
    assert problems == [] and lh is not None
    return lh


@pytest.fixture(scope="session")
def result(lakehouse):  # type: ignore[no-untyped-def]
    from medallion import oracle

    return oracle.run(lakehouse, SPEC / "data")


def write_jsonl(folder: Path, name: str, rows: list[dict[str, Any] | str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    (folder / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
