import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.vanish import VanishError, clear_runtime_data, resolve_project_root, resolve_runtime_data_dir


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "VideoMind"
    data_dir = root / "data"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "frontend").mkdir()
    data_dir.mkdir()

    for name in ["videos", "audio", "transcripts", "summaries", "chroma", "users", "chats"]:
        (data_dir / name).mkdir()

    (root / "README.md").write_text("project readme", encoding="utf-8")
    (root / "PROJECT_RECAP.md").write_text("project recap", encoding="utf-8")
    (root / "backend" / "app" / "main.py").write_text("print('backend')\n", encoding="utf-8")
    (root / ".env").write_text("KEY=value\n", encoding="utf-8")
    (data_dir / "unlisted.txt").write_text("not allowlisted", encoding="utf-8")
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "cache.txt").write_text("cache stays", encoding="utf-8")
    return root


def test_runtime_files_are_removed(project_root: Path) -> None:
    data_dir = resolve_runtime_data_dir(project_root)
    for section in ["videos", "audio", "transcripts", "summaries", "chroma", "users", "chats"]:
        (data_dir / section / "nested").mkdir(parents=True)
        (data_dir / section / "nested" / "artifact.txt").write_text(f"{section} data", encoding="utf-8")
        (data_dir / section / f"{section}.bin").write_bytes(b"payload")

    clear_runtime_data(project_root)

    for section in ["videos", "audio", "transcripts", "summaries", "chroma", "users", "chats"]:
        assert (data_dir / section).exists()
        assert list((data_dir / section).iterdir()) == []


def test_directory_structure_remains(project_root: Path) -> None:
    data_dir = resolve_runtime_data_dir(project_root)
    for section in ["videos", "audio", "transcripts", "summaries", "chroma", "users", "chats"]:
        (data_dir / section).mkdir(exist_ok=True)

    clear_runtime_data(project_root)

    for section in ["videos", "audio", "transcripts", "summaries", "chroma", "users", "chats"]:
        assert (data_dir / section).is_dir()


def test_nested_files_and_directories_are_removed(project_root: Path) -> None:
    data_dir = resolve_runtime_data_dir(project_root)
    nested_dir = data_dir / "videos" / "abc123" / "nested"
    nested_dir.mkdir(parents=True)
    (nested_dir / "deep.txt").write_text("deep", encoding="utf-8")
    (data_dir / "audio" / "summaries").mkdir(parents=True)
    (data_dir / "audio" / "summaries" / "demo.wav").write_bytes(b"wav")

    clear_runtime_data(project_root)

    assert list((data_dir / "videos").iterdir()) == []
    assert list((data_dir / "audio").iterdir()) == []


def test_cache_is_untouched(project_root: Path) -> None:
    cache_path = project_root / ".pytest_cache" / "cache.txt"
    before = cache_path.read_text(encoding="utf-8")

    clear_runtime_data(project_root)

    assert cache_path.read_text(encoding="utf-8") == before
    assert cache_path.exists()


def test_project_files_are_untouched(project_root: Path) -> None:
    readme = project_root / "README.md"
    source = project_root / "backend" / "app" / "main.py"
    recap = project_root / "PROJECT_RECAP.md"
    config = project_root / ".env"

    clear_runtime_data(project_root)

    assert readme.read_text(encoding="utf-8") == "project readme"
    assert recap.read_text(encoding="utf-8") == "project recap"
    assert source.read_text(encoding="utf-8") == "print('backend')\n"
    assert config.read_text(encoding="utf-8") == "KEY=value\n"
    assert (project_root / "data" / "unlisted.txt").read_text(encoding="utf-8") == "not allowlisted"


def test_wrong_target_is_rejected(project_root: Path) -> None:
    unsafe = project_root / "backend"
    with pytest.raises(VanishError, match="Unsafe runtime data target"):
        clear_runtime_data(project_root, target=unsafe)


def test_symlinked_runtime_directory_is_rejected_before_cleanup(project_root: Path, tmp_path: Path) -> None:
    data_dir = resolve_runtime_data_dir(project_root)
    video_file = data_dir / "videos" / "keep.mp4"
    video_file.write_bytes(b"media")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "keep.txt"
    outside_file.write_text("outside", encoding="utf-8")
    chats_dir = data_dir / "chats"
    chats_dir.rmdir()
    try:
        chats_dir.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symlinks are unavailable in this environment")

    with pytest.raises(VanishError, match="symlink"):
        clear_runtime_data(project_root)

    assert video_file.read_bytes() == b"media"
    assert outside_file.read_text(encoding="utf-8") == "outside"


def test_standalone_script_execution(project_root: Path) -> None:
    data_dir = resolve_runtime_data_dir(project_root)
    (data_dir / "videos" / "example.mp4").write_bytes(b"media")
    script_dir = project_root / "scripts"
    script_dir.mkdir()
    script_path = script_dir / "vanish.py"
    source_script = Path(__file__).resolve().parents[1] / "scripts" / "vanish.py"
    shutil.copyfile(source_script, script_path)

    result = subprocess.run(
        [sys.executable, "scripts/vanish.py"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "data/users/" in result.stdout
    assert "data/chats/" in result.stdout
    for section in ["videos", "audio", "transcripts", "summaries", "chroma", "users", "chats"]:
        assert list((data_dir / section).iterdir()) == []


def test_resolve_project_root_from_script_location() -> None:
    assert resolve_project_root().name == "VideoMind"
