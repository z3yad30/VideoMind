from __future__ import annotations

import shutil
import sys
from pathlib import Path

RUNTIME_DATA_DIRECTORIES = ("audio", "chroma", "summaries", "transcripts", "videos")


class VanishError(RuntimeError):
    """Raised when the cleanup target is not the expected VideoMind runtime data directory."""


def resolve_project_root(start: Path | str | None = None) -> Path:
    """Resolve the project root without depending on the current working directory."""
    candidate = Path(start).resolve() if start is not None else Path(__file__).resolve().parent.parent

    for location in [candidate, *candidate.parents]:
        if (location / "backend").exists() and (location / "README.md").exists():
            return location.resolve()

    raise VanishError(f"Unable to locate the VideoMind project root from {candidate}")


def resolve_runtime_data_dir(project_root: Path | str | None = None) -> Path:
    """Resolve the repository-bound runtime data directory while rejecting unsafe targets."""
    root = resolve_project_root(project_root)
    target = (root / "data").resolve(strict=False)

    if target == root:
        raise VanishError("Unsafe runtime data target: the project root is not allowed as the cleanup target.")
    if target.parent == root:
        # This is the expected directory under the repo root; allow it.
        pass
    if target.is_symlink():
        raise VanishError(f"Unsafe runtime data target: {target} is a symlink and cannot be used for cleanup.")
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise VanishError(f"Unsafe runtime data target: {target} is not a directory.")
    return target


def _validate_cleanup_target(target: Path | str, expected: Path) -> Path:
    resolved_target = Path(target).resolve(strict=False)
    if resolved_target != expected:
        raise VanishError(
            f"Unsafe runtime data target: {resolved_target} does not match the expected VideoMind data directory {expected}."
        )
    if resolved_target == resolved_target.anchor:
        raise VanishError("Unsafe runtime data target: cannot use a filesystem root as the cleanup target.")
    if resolved_target.is_symlink():
        raise VanishError(f"Unsafe runtime data target: {resolved_target} is a symlink and cannot be used for cleanup.")
    return resolved_target


def _clear_directory_contents(directory: Path) -> int:
    cleared = 0
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or child.is_file():
            child.unlink()
            cleared += 1
            continue
        if child.is_dir():
            shutil.rmtree(child)
            cleared += 1
    return cleared


def clear_runtime_data(project_root: Path | str | None = None, target: Path | str | None = None) -> Path:
    """Remove the runtime-generated contents from the VideoMind data directory while preserving its structure."""
    root = resolve_project_root(project_root)
    expected_data_dir = resolve_runtime_data_dir(root)

    if target is not None:
        expected_data_dir = _validate_cleanup_target(target, expected_data_dir)

    print(f"Vanish: starting cleanup for VideoMind runtime data at {expected_data_dir}")
    print(f"Vanish: clearing runtime directories: {', '.join(RUNTIME_DATA_DIRECTORIES)}")

    for directory_name in RUNTIME_DATA_DIRECTORIES:
        runtime_dir = expected_data_dir / directory_name
        runtime_dir.mkdir(parents=True, exist_ok=True)
        cleared = _clear_directory_contents(runtime_dir)
        print(f"Vanish: cleared {cleared} item(s) from {runtime_dir}")

    print(f"Vanish: completed; preserved runtime directory structure in {expected_data_dir}")
    return expected_data_dir


def main() -> int:
    try:
        clear_runtime_data()
        return 0
    except VanishError as exc:
        print(f"Vanish: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI guard
        print(f"Vanish: unexpected error while clearing runtime data: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
