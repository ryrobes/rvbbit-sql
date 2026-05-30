from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import traceback
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> str:
    return str(value)


def main() -> int:
    if len(sys.argv) != 3:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "usage: runner.py <handler_path> <entrypoint>",
                }
            )
        )
        return 2

    handler_path = Path(sys.argv[1])
    entrypoint = sys.argv[2]
    try:
        inputs = json.load(sys.stdin)
        spec = importlib.util.spec_from_file_location("rvbbit_handler", handler_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot import handler at {handler_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, entrypoint)
        if not callable(fn):
            raise RuntimeError(f"entrypoint {entrypoint!r} is not callable")

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            output = fn(inputs)

        print(
            json.dumps(
                {
                    "ok": True,
                    "output": output,
                    "stdout": stdout_buf.getvalue(),
                },
                default=_json_default,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - preserve user traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
