from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ols_reader
from ols_reader import main
from olsbuild import AxisSpec, FolderSpec, MapSpec, VersionSpec, build_ols

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project(tmp_path: Path) -> Path:
    maps = [
        MapSpec(name="KFMIRL", description="load", type_code=4, dim1=12, dim2=16, unit="%", factor=0.5,
                folder_id=3, axis1=AxisSpec(address=0x2000), axis2=AxisSpec(address=0x3000)),
        MapSpec(name="KLAF", description="curve", type_code=3, dim1=8, axis1=AxisSpec(address=0x5000)),
    ] + [MapSpec(name=f"CW_{i}", type_code=2) for i in range(12)]
    path = tmp_path / "golf.ols"
    path.write_bytes(build_ols(
        804, header={"make": "VW", "model": "Golf", "ecu_type": "SIMOS18", "ecu_manufacturer": "Continental"},
        maps=maps, versions=[VersionSpec("Original"), VersionSpec("../../evil/..")],
        blobs=[b"CASC2E55SC8F830\x00" + b"A" * 32, b"B" * 32],
        folders=[FolderSpec(3, "ACQ_KNK", "Knock")],
    ))
    return path


def test_summary(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(project)]) == 0
    out = capsys.readouterr().out
    assert "OLS Version: 804" in out
    assert "Vehicle: VW Golf" in out
    assert "ECU: Continental SIMOS18" in out
    assert "Parameters: 14" in out
    assert "By type: CURVE: 1, MAP: 1, VALUE: 12" in out
    assert "[xaxis@0x3000] [yaxis@0x2000] [ACQ_KNK]" in out
    assert "[xaxis@0x5000]" in out
    assert "... and 4 more" in out
    assert "Original (idx=0/2)" in out and "EPK=SC8F830" in out


def test_all_and_limit(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(project), "--all"]) == 0
    assert "CW_11" in capsys.readouterr().out
    assert main([str(project), "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "KFMIRL" in out and "KLAF" not in out


def test_json(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(project), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["make"] == "VW"
    assert len(doc["parameters"]) == 14
    assert doc["parameters"][0]["x_axis"]["address"] == 0x3000
    assert doc["cal_block"]["epk"] == "SC8F830"


def test_json_multiple_files(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(project), str(project), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_errors_set_exit_status(project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bogus = tmp_path / "bogus.ols"
    bogus.write_bytes(b"not an ols file")
    assert main([str(bogus), str(tmp_path / "missing.ols"), str(project)]) == 1
    captured = capsys.readouterr()
    assert "bogus.ols: " in captured.err and "not a WinOLS file" in captured.err
    assert "missing.ols" in captured.err
    assert "Parameters: 14" in captured.out  # remaining files are still processed


def test_extract_sanitises_names_and_refuses_overwrite(project: Path, tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "out"
    assert main([str(project), "--extract", str(out_dir)]) == 0
    files = sorted(p.name for p in out_dir.iterdir())
    assert files == ["golf.00.Original.bin", "golf.01.evil.bin"]
    assert (out_dir / "golf.00.Original.bin").read_bytes().startswith(b"CASC2E55")
    assert not (tmp_path / "evil").exists()

    assert main([str(project), "--extract", str(out_dir)]) == 1
    assert "cannot extract binaries" in capsys.readouterr().err
    assert main([str(project), "--extract", str(out_dir), "--force"]) == 0


@pytest.mark.parametrize("name, expected", [
    ("Original", "Original"),
    ("../../etc/passwd", "etc_passwd"),
    ("..", "binary"),
    ("C:\\Windows\\x", "C_Windows_x"),
    ("Stage 1 / ä", "Stage_1"),
    ("", "binary"),
])
def test_safe_filename(name: str, expected: str) -> None:
    assert ols_reader._safe_filename(name) == expected


def test_usage_errors(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main([str(project), "--limit", "-1"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main([str(project), "--json", "--all"])
    assert exc.value.code == 2


def test_module_entry_point(project: Path) -> None:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, "-m", "ols_reader", "--version"],
                            capture_output=True, text=True, env=env, check=True)
    assert result.stdout.strip() == f"pyols {ols_reader.__version__}"
    result = subprocess.run([sys.executable, str(ROOT / "ols_reader.py"), str(project)],
                            capture_output=True, text=True, env=env, check=True)
    assert "Parameters: 14" in result.stdout
