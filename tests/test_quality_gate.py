"""Quality gate tests ensuring the verification workflow runs lint and tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "verify.yml"
RELEASE_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
TESTS_DIR = PROJECT_ROOT / "tests"


@pytest.fixture
def workflow_text() -> str:
    if not WORKFLOW_PATH.exists():
        pytest.fail(f"Verification workflow not found: {WORKFLOW_PATH}")
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_verification_workflow_runs_lint(workflow_text: str) -> None:
    assert "uv run --frozen ruff check src tests" in workflow_text, (
        "Verification workflow must run the project lint command"
    )


def test_verification_workflow_runs_tests(workflow_text: str) -> None:
    assert "uv run --frozen pytest" in workflow_text, (
        "Verification workflow must run the project test command"
    )


def test_verification_workflow_checks_formatting(workflow_text: str) -> None:
    assert "uv run --frozen ruff format --check src tests" in workflow_text


def test_integration_workflow_installs_client_in_the_test_environment(
    workflow_text: str,
) -> None:
    assert "uv pip install" not in workflow_text
    assert (
        "uv run --frozen --with opentimestamps-client==0.7.2 pytest -m integration"
        in workflow_text
    )


def test_verification_workflow_targets_master(workflow_text: str) -> None:
    assert "branches: [master]" in workflow_text
    assert "branches: [main" not in workflow_text


def test_workflow_exercises_complete_supported_matrix(workflow_text: str) -> None:
    assert "os: [ubuntu-latest, macos-latest]" in workflow_text
    assert 'python-version: ["3.12", "3.13", "3.14"]' in workflow_text
    assert "runs-on: ${{ matrix.os }}" in workflow_text
    assert "python-version: ${{ matrix.python-version }}" in workflow_text


def test_workflow_actions_are_pinned_to_full_commit_ids(workflow_text: str) -> None:
    uses = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", workflow_text, re.MULTILINE)
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", use) for use in uses), uses


def test_uv_is_pinned_and_lockfile_use_is_frozen(workflow_text: str) -> None:
    assert 'version: "0.11.29"' in workflow_text
    assert workflow_text.count("--frozen") >= 3


def test_workflow_declares_read_only_permissions(workflow_text: str) -> None:
    assert "permissions:\n  contents: read" in workflow_text


def test_workflow_cancels_superseded_runs(workflow_text: str) -> None:
    assert "concurrency:" in workflow_text
    assert "cancel-in-progress: true" in workflow_text


def test_release_workflow_builds_once_and_uses_trusted_publishing() -> None:
    text = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert text.count("uv build") == 1
    assert "id-token: write" in text
    assert "gh-action-pypi-publish@" in text
    assert "actions/attest@" in text
    assert "SHA256SUMS" in text
    assert "git cat-file -e" in text
    uses = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", text, re.MULTILINE)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", use) for use in uses)


def _is_dict_like(node: ast.AST) -> bool:
    """True for a bare `{...}` dict literal or a call to the `dict` builtin."""
    if isinstance(node, ast.Dict):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    )


def _references_os_environ(node: ast.AST) -> bool:
    """True if `node`'s subtree references `os.environ`, in any spelling.

    Covers `os.environ` directly and the `__import__("os").environ` idiom
    used in this suite when `os` has not been imported at module scope
    (see `tests/test_git.py`'s committer-time helpers). Both forms
    genuinely extend, rather than replace, the ambient environment when
    they appear inside a `dict(...)` call (e.g. `dict(os.environ)`) or a
    dict-unpacking literal (e.g. `{**os.environ, ...}`); `os.environ.copy()`
    is a distinct `Call` shape that this helper also matches, since its
    subtree still contains an `os.environ` attribute access.
    """
    for candidate in ast.walk(node):
        if not (isinstance(candidate, ast.Attribute) and candidate.attr == "environ"):
            continue
        value = candidate.value
        if isinstance(value, ast.Name) and value.id == "os":
            return True
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "__import__"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and value.args[0].value == "os"
        ):
            return True
    return False


class _EnvAssignmentCollector(ast.NodeVisitor):
    """Scope-aware collector for environment-dict assignments and call sites.

    Tracks a stack of enclosing function scopes (module scope is
    represented by `None`) so that alias resolution -- finding the
    assignment a `env=<name>` keyword argument refers to -- only looks
    within the same function, never across function boundaries.
    """

    def __init__(self) -> None:
        self._scope_stack: list[ast.AST | None] = [None]
        # scope -> ordered list of (name, lineno, value) for simple
        # `name = <expr>` / `name: T = <expr>` assignments in that scope.
        self.name_assigns: dict[ast.AST | None, list[tuple[str, int, ast.AST]]] = {}
        # (lineno, value) for every direct `env = <expr>` / `env: T = <expr>`.
        self.direct_env_assigns: list[tuple[int, ast.AST]] = []
        # (scope, lineno, value) for every `env=<expr>` call keyword.
        self.env_keywords: list[tuple[ast.AST | None, int, ast.AST]] = []

    def _record(self, name: str, lineno: int, value: ast.AST) -> None:
        self.name_assigns.setdefault(self._scope_stack[-1], []).append(
            (name, lineno, value)
        )
        if name == "env":
            self.direct_env_assigns.append((lineno, value))

    def _visit_function(self, node: ast.AST) -> None:
        self._scope_stack.append(node)
        self.generic_visit(node)
        self._scope_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._record(target.id, node.lineno, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._record(node.target.id, node.lineno, node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg == "env":
                self.env_keywords.append(
                    (self._scope_stack[-1], keyword.value.lineno, keyword.value)
                )
        self.generic_visit(node)


def _find_offenders(tree: ast.AST) -> list[int]:
    """Return the 1-based line numbers of unsafe `env` sites in `tree`."""
    collector = _EnvAssignmentCollector()
    collector.visit(tree)

    offending_lines: list[int] = []

    for lineno, value in collector.direct_env_assigns:
        if _is_dict_like(value) and not _references_os_environ(value):
            offending_lines.append(lineno)

    for scope, lineno, value in collector.env_keywords:
        if _is_dict_like(value):
            if not _references_os_environ(value):
                offending_lines.append(lineno)
        elif isinstance(value, ast.Name):
            # Alias resolution: `env=commit_env` resolves back to the
            # nearest preceding `commit_env = <expr>` in the same scope.
            preceding = [
                (a_lineno, a_value)
                for (a_name, a_lineno, a_value) in collector.name_assigns.get(scope, [])
                if a_name == value.id and a_lineno < lineno
            ]
            if not preceding:
                continue
            _, resolved_value = max(preceding, key=lambda pair: pair[0])
            if _is_dict_like(resolved_value) and not _references_os_environ(
                resolved_value
            ):
                offending_lines.append(lineno)

    return offending_lines


def test_subprocess_env_dict_literals_extend_the_ambient_environment() -> None:
    """AC-STRUCT-2's env-extension rule, enforced structurally via the AST.

    A subprocess call whose environment keyword argument is built as a
    bare dict literal, or via the `dict` builtin, and does not reference
    `os.environ` REPLACES the child environment rather than extending it
    -- silently dropping the autouse git-config isolation fixture's
    variables (and PATH, and everything else). This test parses every
    `.py` file under `tests/` and walks its AST for two shapes:

    1. A direct assignment or call keyword whose value is a bare `{...}`
       dict literal or a `dict(...)` call, checked structurally for an
       `os.environ` reference anywhere in that value's subtree. This
       covers `env={...}` inline, a multi-line `env={` block, `env =
       dict(...)`, and `subprocess.Popen(env={...})` alike -- line
       layout and the surrounding call's name don't matter to an AST
       walk.
    2. Aliasing: an `env=<name>` call keyword whose value is a bare
       identifier is resolved back to the nearest *preceding* simple
       assignment of that identifier *in the same function scope*
       (module scope counts as one scope of its own), and that
       resolved assignment's value is checked the same way. Resolution
       does not cross function boundaries, and gives up (does not flag)
       if no such preceding assignment exists in scope, or if the
       resolved value is not itself a dict literal or `dict(...)` call
       (e.g. `os.environ.copy()` -- itself safe and never flagged,
       whether assigned directly or reached through an alias).

    Because this is a real parse rather than a text scan, it does not
    match inside string literals, docstrings, or comments -- rephrasing
    prose that happens to contain `env = {...}` no longer trips this
    gate. Coverage is still deliberately partial: it does not evaluate
    dict-construction hidden behind a function call other than `dict`
    (e.g. a project-local `build_env()` helper), a comprehension, a
    conditional expression, or an import-time alias of `dict` itself.
    Extending the pattern to those idioms is welcome future work; until
    then, this is a partial structural gate, not a total one.

    A file that fails to parse fails the gate immediately, naming the
    file and the underlying `SyntaxError`, rather than surfacing an
    opaque traceback from deep inside this test.
    """
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue  # this file's own docstring describes the pattern
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            pytest.fail(f"could not parse {path.relative_to(PROJECT_ROOT)}: {exc}")
        for lineno in _find_offenders(tree):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}")

    assert not offenders, (
        "found a subprocess environment dict literal or dict(...) call "
        "that does not reference os.environ (it replaces, rather than "
        f"extends, the child environment, silently dropping git-config "
        f"isolation): {offenders}"
    )
