"""Select a release without executing source from the version file."""

import ast
import json
import os
import re
import subprocess
from pathlib import Path

VERSION_PATH = "src/git_ots/__init__.py"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def version_at(ref: str) -> str:
    tree = ast.parse(git("show", f"{ref}:{VERSION_PATH}"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            version = ast.literal_eval(statement.value)
            if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
                return version
    raise ValueError("Release version must be a literal MAJOR.MINOR.PATCH string")


def select(event: dict, event_name: str) -> tuple[str, str] | None:
    if event_name == "workflow_dispatch":
        tag = event["inputs"]["release_tag"]
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise ValueError("Recovery requires an existing vMAJOR.MINOR.PATCH tag")
        target = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
        if tag != f"v{version_at(target)}":
            raise ValueError("Tag and package version disagree")
    else:
        if event["ref"] != "refs/heads/master" or event.get("deleted"):
            return None
        before, target = event["before"], event["after"]
        if set(before) == {"0"}:
            raise ValueError("Initial branch creation is not a version bump")
        previous, current = version_at(before), version_at(target)
        if previous == current:
            return None
        if tuple(map(int, current.split("."))) <= tuple(map(int, previous.split("."))):
            raise ValueError("Release version must increase")
        tag = f"v{current}"
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", target, "origin/master"], check=True
    )
    existing = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0 and existing.stdout.strip() != target:
        raise ValueError(f"{tag} already points to another commit")
    return target, tag


if __name__ == "__main__":
    release = select(
        json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
        os.environ["GITHUB_EVENT_NAME"],
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"needed={'true' if release else 'false'}\n")
        if release:
            output.write(f"target={release[0]}\ntag={release[1]}\n")
