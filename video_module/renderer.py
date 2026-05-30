"""Run `npx remotion render` as an asyncio subprocess.

Responsible for:
  • copying brand-specific assets into <project>/public/ (logos/, products/,
    music/, narration mp3)
  • writing a temporary props.json
  • invoking the CLI
  • collecting the mp4 + a sibling .log.json

Public/ is **wiped of per-job content** before each render so leftover files
from a previous job never leak in. The fonts/ folder is preserved.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RenderJob:
    composition_id: str
    props:          dict
    logo_src:       Path
    product_srcs:   list[Path]
    music_src:      Optional[Path] = None
    narration_src:  Optional[Path] = None
    output_path:    Path = field(default_factory=lambda: Path("render.mp4"))


@dataclass
class RenderResult:
    output_path:     Path
    duration_seconds: float
    stderr_tail:     str
    exit_code:       int


class RenderError(RuntimeError):
    pass


async def render(
    project_dir: Path,
    job: RenderJob,
    *,
    timeout_seconds: int = 600,
) -> RenderResult:
    """Stage assets, write props.json, run `npx remotion render`."""
    project_dir = Path(project_dir)
    public = project_dir / "public"
    _stage_public(public, job)

    props_file = public.parent / ".video-props.json"
    props_file.write_text(json.dumps(job.props, ensure_ascii=False), encoding="utf-8")

    cmd = [
        "npx", "remotion", "render",
        "src/index.ts", job.composition_id, str(job.output_path),
        "--props", str(props_file),
        "--overwrite",
    ]
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RenderError(f"render timeout after {timeout_seconds}s for {job.composition_id}")
    duration = time.monotonic() - start
    stderr_text = (stderr or b"").decode("utf-8", errors="replace")
    tail = "\n".join(stderr_text.splitlines()[-30:])

    if proc.returncode != 0:
        raise RenderError(f"remotion render exited {proc.returncode}\n{tail}")

    if not job.output_path.is_file():
        raise RenderError(f"remotion exited 0 but output missing: {job.output_path}")

    # Sibling log file
    log_path = job.output_path.with_suffix(".log.json")
    log_path.write_text(json.dumps({
        "composition_id": job.composition_id,
        "props":           job.props,
        "duration_s":      round(duration, 2),
        "exit_code":       proc.returncode,
        "stderr_tail":     tail,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return RenderResult(
        output_path=job.output_path,
        duration_seconds=duration,
        stderr_tail=tail,
        exit_code=proc.returncode or 0,
    )


def _stage_public(public: Path, job: RenderJob) -> None:
    """Reset per-job folders under public/ and copy fresh assets in.

    The fonts/ folder is preserved (committed to git in the Remotion repo).
    """
    for sub in ("logos", "products", "music"):
        target = public / sub
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    # Drop stale narration mp3 if any (it lives at public root)
    for f in public.glob("narration*.mp3"):
        f.unlink()

    shutil.copy(job.logo_src,    public / "logos"    / job.logo_src.name)
    for p in job.product_srcs:
        shutil.copy(p, public / "products" / p.name)
    if job.music_src:
        shutil.copy(job.music_src, public / "music" / job.music_src.name)
    if job.narration_src:
        shutil.copy(job.narration_src, public / job.narration_src.name)
