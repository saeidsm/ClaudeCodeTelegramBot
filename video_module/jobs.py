"""JSON-on-disk job state for /video wizard. One file per chat_id.

Stored at: <root>/<chat_id>.json
Writes are atomic via tmp + os.replace so a crashed write never leaves a
partial file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id: int) -> Path:
        return self.root / f"{chat_id}.json"

    def save(self, chat_id: int, state: dict) -> None:
        p = self._path(chat_id)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def load(self, chat_id: int) -> Optional[dict]:
        p = self._path(chat_id)
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def delete(self, chat_id: int) -> None:
        p = self._path(chat_id)
        if p.is_file():
            p.unlink()
