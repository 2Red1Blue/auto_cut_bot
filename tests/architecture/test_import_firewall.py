from __future__ import annotations

from pathlib import Path

import pytest

from tools.architecture.import_firewall import ImportFirewallError, assert_clean, scan_package


def _write_source(root: Path, source: str) -> Path:
    package = root / "sample_runtime"
    package.mkdir()
    (package / "module.py").write_text(source, encoding="utf-8")
    return package


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("import autocut_core\n", "legacy-import"),
        ("from autocut_core.stages import Stage\n", "legacy-import"),
        ("from ..outside import value\n", "relative-import-escape"),
        ("import sys\nsys.path.append('/legacy')\n", "sys-path-mutation"),
        ("from sys import path\npath.insert(0, '/legacy')\n", "sys-path-mutation"),
        ("import sys\nsys.path[0] = '/legacy'\n", "sys-path-mutation"),
        ("import sys\nsys.path[:] = ['/legacy']\n", "sys-path-mutation"),
        ("from sys import path\ndel path[0]\n", "sys-path-mutation"),
        ("from sys import path as paths\npaths[0] = '/legacy'\n", "sys-path-mutation"),
    ],
)
def test_firewall_rejects_boundary_escapes(tmp_path: Path, source: str, rule: str) -> None:
    violations = scan_package(_write_source(tmp_path, source))

    assert [violation.rule for violation in violations] == [rule]


def test_firewall_accepts_runtime_composition_importing_kernel(tmp_path: Path) -> None:
    package = _write_source(
        tmp_path,
        "from autocut_kernel import KernelIdentity\nidentity = KernelIdentity('kernel', '0.1')\n",
    )

    assert scan_package(package) == ()
    assert_clean([package])


def test_assert_clean_reports_all_violations(tmp_path: Path) -> None:
    package = _write_source(tmp_path, "import autocut_core\nimport sys\nsys.path.append('x')\n")

    with pytest.raises(ImportFirewallError) as error:
        assert_clean([package])

    assert "legacy-import" in str(error.value)
    assert "sys-path-mutation" in str(error.value)
