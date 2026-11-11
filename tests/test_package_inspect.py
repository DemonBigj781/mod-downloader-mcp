import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import zipfile

import pytest


INSPECTOR = Path(__file__).resolve().parents[1] / "scripts/tcg-package-inspect.py"


def run_inspection(monkeypatch, root, entries):
    archives = root / "archives"
    archives.mkdir()
    with zipfile.ZipFile(archives / "fixture.zip", "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    monkeypatch.setattr(sys, "argv", [str(INSPECTOR), str(root)])
    runpy.run_path(str(INSPECTOR))["main"]()


def test_windows_directory_entries_are_not_treated_as_files(monkeypatch, tmp_path):
    run_inspection(monkeypatch, tmp_path, [
        ("BepInEx\\plugins\\", b""),
        ("BepInEx\\plugins\\Fixture\\readme.txt", b"fixture"),
    ])
    assert (tmp_path / "expanded/fixture/BepInEx/plugins/Fixture/readme.txt").read_bytes() == b"fixture"
    report = json.loads((tmp_path / "package-inspection.json").read_text())
    assert len(report[0]["files"]) == 1


@pytest.mark.parametrize("name", ["../outside.txt", "BepInEx\\..\\outside.txt", "/outside.txt", "C:\\outside.txt"])
def test_path_escape_is_still_rejected(monkeypatch, tmp_path, name):
    with pytest.raises(ValueError, match="Unsafe archive path"):
        run_inspection(monkeypatch, tmp_path, [(name, b"no")])


def test_directory_with_payload_is_rejected(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        run_inspection(monkeypatch, tmp_path, [("BepInEx\\plugins\\", b"not a directory")])


def test_assembly_with_no_external_references_is_valid(monkeypatch, tmp_path):
    module = runpy.run_path(str(INSPECTOR))
    metadata = SimpleNamespace(
        Assembly=SimpleNamespace(rows=[SimpleNamespace(Name="fixture")]),
        AssemblyRef=None, CustomAttribute=None,
    )
    pe = SimpleNamespace(net=SimpleNamespace(mdtables=metadata), close=lambda: None)
    monkeypatch.setattr(module["dnfile"], "dnPE", lambda path: pe)
    path = tmp_path / "fixture.dll"
    path.write_bytes(b"fixture")
    assert module["inspect"](path)["references"] == []
