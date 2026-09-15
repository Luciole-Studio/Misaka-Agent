"""Ignored build-tree symlinks must not hide the real package from Hatchling."""

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile


def test_sdist_and_wheel_keep_package_with_ignored_source_symlink(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", ".gitignore", "README.md", "LICENSE"):
        shutil.copyfile(root / name, project / name)

    expected = {
        "misaka/__init__.py": b'__version__ = "fixture"\n',
        "misaka/__main__.py": b'def main():\n    print("fixture")\n',
        "misaka/core/skills/assets/fixture/SKILL.md": b"# Packaged skill\n",
    }
    for name, content in expected.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # Hatchling visits build/ before misaka/. Its inode deduplication used to
    # skip the real package after following this excluded directory's symlink.
    shadow = project / "build" / "old-acceptance"
    shadow.mkdir(parents=True)
    (shadow / "misaka").symlink_to(project / "misaka", target_is_directory=True)
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--offline", "--python", sys.executable,
         "--out-dir", str(dist), str(project)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # uv's default path builds the wheel FROM the sdist, not directly from source.
    [sdist] = dist.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        packaged = {}
        for member in archive.getmembers():
            name = member.name.partition("/")[2]
            assert not name.startswith("build/")
            if member.isfile() and name.startswith("misaka/"):
                with archive.extractfile(member) as source:
                    packaged[name] = source.read()
        assert packaged == expected

    [wheel] = dist.glob("*.whl")
    with ZipFile(wheel) as archive:
        assert {name: archive.read(name) for name in archive.namelist()
                if name.startswith("misaka/")} == expected
