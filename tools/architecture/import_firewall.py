"""Deterministic AST checks for the initial runtime import boundary.

This intentionally checks only static imports, parent-relative escapes, and
``sys.path`` mutation. Dynamic loading and full dependency-graph analysis are
outside this package-skeleton slice.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

LEGACY_IMPORT_ROOTS = frozenset({"autocut_core", "ac_auto_cut", "artifactbus", "artifact_bus"})
LEGACY_SYMBOLS = frozenset({"ArtifactBus", "Stage"})
_SYS_PATH_MUTATORS = frozenset({"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse"})


@dataclass(frozen=True, slots=True)
class FirewallViolation:
    """One static boundary violation with a stable location."""

    path: Path
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


class ImportFirewallError(RuntimeError):
    """Raised when source crosses a forbidden import boundary."""

    def __init__(self, violations: Sequence[FirewallViolation]) -> None:
        self.violations = tuple(violations)
        super().__init__("\n".join(violation.render() for violation in self.violations))


class _ImportFirewallVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, package_depth: int, forbidden_roots: frozenset[str]) -> None:
        self.path = path
        self.package_depth = package_depth
        self.forbidden_roots = forbidden_roots
        self.violations: list[FirewallViolation] = []
        self.sys_names = {"sys"}
        self.path_names: set[str] = set()

    def _violate(self, node: ast.AST, rule: str, detail: str) -> None:
        self.violations.append(FirewallViolation(self.path, node.lineno, rule, detail))

    def _check_module(self, node: ast.AST, module: str) -> None:
        root = module.split(".", 1)[0]
        if root in self.forbidden_roots:
            self._violate(node, "legacy-import", f"forbidden import root '{root}'")

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self._check_module(node, alias.name)
            if alias.name == "sys":
                self.sys_names.add(alias.asname or "sys")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.level > self.package_depth:
            self._violate(node, "relative-import-escape", "parent-relative import escapes package")
        if node.module is not None:
            self._check_module(node, node.module)
        module_root = node.module.split(".", 1)[0] if node.module is not None else None
        for alias in node.names:
            if alias.name in LEGACY_SYMBOLS and module_root not in self.forbidden_roots:
                self._violate(node, "legacy-import", f"forbidden legacy symbol '{alias.name}'")
            if node.module == "sys" and alias.name == "path":
                self.path_names.add(alias.asname or "path")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if any(self._is_sys_path(target) or self._is_path_alias(target) for target in node.targets):
            self._violate(node, "sys-path-mutation", "assignment mutates sys.path")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if self._is_sys_path(node.target) or self._is_path_alias(node.target):
            self._violate(node, "sys-path-mutation", "assignment mutates sys.path")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        if self._is_sys_path(node.target) or self._is_path_alias(node.target):
            self._violate(node, "sys-path-mutation", "assignment mutates sys.path")
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802
        if any(self._is_sys_path(target) or self._is_path_alias(target) for target in node.targets):
            self._violate(node, "sys-path-mutation", "deletion mutates sys.path")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr in _SYS_PATH_MUTATORS
            and (self._is_sys_path(function.value) or self._is_path_alias(function.value))
        ):
            self._violate(node, "sys-path-mutation", f"sys.path.{function.attr}() is forbidden")
        self.generic_visit(node)

    def _is_sys_path(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Subscript):
            return self._is_sys_path(node.value)
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.sys_names
        )

    def _is_path_alias(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Subscript):
            return self._is_path_alias(node.value)
        return isinstance(node, ast.Name) and node.id in self.path_names


def scan_file(
    path: Path,
    *,
    package_root: Path,
    forbidden_roots: Iterable[str] = LEGACY_IMPORT_ROOTS,
) -> tuple[FirewallViolation, ...]:
    """Return static import-boundary violations for one Python source file."""

    resolved_path = path.resolve()
    resolved_root = package_root.resolve()
    relative_parent = resolved_path.parent.relative_to(resolved_root)
    package_depth = len(relative_parent.parts) + 1
    visitor = _ImportFirewallVisitor(
        resolved_path,
        package_depth,
        frozenset(forbidden_roots),
    )
    visitor.visit(ast.parse(resolved_path.read_text(encoding="utf-8"), filename=str(resolved_path)))
    return tuple(visitor.violations)


def scan_package(
    package_root: Path,
    *,
    forbidden_roots: Iterable[str] = LEGACY_IMPORT_ROOTS,
) -> tuple[FirewallViolation, ...]:
    """Scan every Python source file below one package root."""

    root = package_root.resolve()
    violations: list[FirewallViolation] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(scan_file(path, package_root=root, forbidden_roots=forbidden_roots))
    return tuple(violations)


def assert_clean(
    package_roots: Iterable[Path],
    *,
    forbidden_roots: Iterable[str] = LEGACY_IMPORT_ROOTS,
) -> None:
    """Raise a single deterministic error if any supplied package is unclean."""

    violations: list[FirewallViolation] = []
    for package_root in package_roots:
        violations.extend(scan_package(package_root, forbidden_roots=forbidden_roots))
    if violations:
        raise ImportFirewallError(violations)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the firewall over package roots supplied on the command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        assert_clean(args.package_root)
    except ImportFirewallError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
