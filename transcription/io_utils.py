from __future__ import annotations

import shutil
from pathlib import Path


def is_supported_input(path: Path, supported_extensions: set[str]) -> bool:
    return path.is_file() and path.suffix.lower() in supported_extensions


def find_source_files(source_dir: Path, supported_extensions: set[str]) -> list[Path]:
    if not source_dir.exists():
        return []
    return sorted(
        [path for path in source_dir.iterdir() if is_supported_input(path, supported_extensions)],
        key=lambda item: item.name.lower(),
    )


def unique_output_dir(base_processed_dir: Path, stem: str) -> Path:
    candidate = base_processed_dir / stem
    suffix = 1
    while candidate.exists():
        candidate = base_processed_dir / f"{stem}_{suffix}"
        suffix += 1
    return candidate


def move_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
