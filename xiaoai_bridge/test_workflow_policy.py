"""Static policy tests for the GitHub Actions workflows.

The repository deliberately separates "build & test" from "publish":

* ``ci.yml`` runs on pull requests and on pushes to ``main`` and must never publish.
* ``release.yml`` is manual-dispatch only, may only run from ``main``, pushes just the
  immutable ``<version>`` tag and must never move ``:latest``.

Those promises are easy to break while editing YAML, so they are asserted here on the
*parsed* workflow documents (not on raw text), which keeps the test from matching
itself. Run it from the repository root:

    python xiaoai_bridge/test_workflow_policy.py
"""

from pathlib import Path
import re
import sys

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

failures = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


def load(name):
    path = WORKFLOW_DIR / name
    check(path.exists(), f"{name} must exist")
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def triggers(document):
    # PyYAML resolves the unquoted `on:` key to boolean True.
    return document.get("on") or document.get(True) or {}


def steps(document):
    for job_name, job in (document.get("jobs") or {}).items():
        for step in (job.get("steps") or []):
            yield job_name, job, step


def perm(document, name):
    return ((document.get("permissions") or {}).get(name) or "").lower()


def run_scripts(document):
    return "\n".join(
        step.get("run") or "" for _, _, step in steps(document)
    )


# ───────────────────────── ci.yml: test only, never publishes ─────────────────────────
ci = load("ci.yml")
ci_triggers = triggers(ci)
check("pull_request" in ci_triggers, "ci.yml must run on pull_request")
check("workflow_dispatch" in ci_triggers, "ci.yml should also be manually runnable")
push_trigger = ci_triggers.get("push") or {}
check("main" in (push_trigger.get("branches") or []), "ci.yml should run on pushes to main")

check(perm(ci, "packages") != "write", "ci.yml must not ask for packages: write")
check(perm(ci, "contents") == "read", "ci.yml should be contents: read only")
for job_name, job in (ci.get("jobs") or {}).items():
    job_permissions = job.get("permissions") or {}
    check(
        (job_permissions.get("packages") or "").lower() != "write",
        f"ci.yml job {job_name} must not grant packages: write",
    )

ci_uses = "\n".join(
    step.get("uses") or "" for _, _, step in steps(ci)
)
check("docker/login-action" not in ci_uses, "ci.yml must not log in to a registry")

build_steps = [
    step for _, _, step in steps(ci)
    if "docker/build-push-action" in (step.get("uses") or "")
]
check(bool(build_steps), "ci.yml must build the image")
for step in build_steps:
    with_block = step.get("with") or {}
    check(with_block.get("push") is False, "the CI image build must set push: false")
    check(with_block.get("load") is True, "the CI image build must load the image locally")
    check(
        str(with_block.get("push", "")).lower() != "true",
        "the CI image build must never push",
    )

ci_runs = run_scripts(ci)
check("docker login" not in ci_runs, "ci.yml must not run docker login")
check("docker push" not in ci_runs, "ci.yml must not run docker push")

# Every suite that CI is supposed to run must actually be invoked.
for suite in (
    "test_startup_patch.py",
    "test_api_auth.py",
    "test_web_patch.py",
    "test_ingress_contract.py",
    "test_upgrade_data.py",
):
    check(suite in ci_runs, f"ci.yml must run {suite}")

# ─────────────────── release.yml: manual, main-only, version tag only ───────────────────
release = load("release.yml")
release_triggers = triggers(release)
check(
    set(release_triggers) == {"workflow_dispatch"},
    f"release.yml must be workflow_dispatch only, got {sorted(release_triggers)}",
)

release_jobs = release.get("jobs") or {}
check(bool(release_jobs), "release.yml must define a job")
release_job = release_jobs.get("release") or next(iter(release_jobs.values()), {})
check(
    "refs/heads/main" in (release_job.get("if") or ""),
    "the release job must be gated on refs/heads/main",
)

release_steps = list(steps(release))
release_names = " ".join(step.get("name") or "" for _, _, step in release_steps)
release_runs = run_scripts(release)

check("main" in release_runs, "release.yml must assert it runs from main")
check(
    "allow_latest" in release_runs and ":latest" in release_runs,
    "release.yml must block moving :latest",
)
check(
    "overwrite_existing" in release_runs,
    "release.yml must refuse to overwrite published version tags by default",
)
check("docker buildx imagetools inspect" in release_runs,
      "release.yml must check whether the version tag already exists")

release_pushes = [
    line for line in release_runs.splitlines() if "docker push" in line
]
check(bool(release_pushes), "release.yml must push the version tag")
for line in release_pushes:
    check(":latest" not in line, f"release.yml must never push :latest: {line.strip()}")

check(perm(release, "packages") == "write", "release.yml needs packages: write to publish")
check(
    ":latest" not in " ".join(
        str(value) for _, _, step in steps(release)
        for value in ((step.get("with") or {}).get("tags") or [])
    ),
    "release.yml build tags must not include :latest",
)

# The version-tag-only promise depends on the tag coming from config.yaml.
check(
    "config.yaml" in release_runs,
    "release.yml must derive the version from xiaoai_bridge/config.yaml",
)

for workflow, document in (("ci.yml", ci), ("release.yml", release)):
    scripts = run_scripts(document)
    check("python xiaoai_bridge/test_entrypoint.py /tmp/app/bridge.py" in scripts,
          workflow + " must test the complete script entrypoint on the host")
    check('--entrypoint python "$IMAGE" /tmp/test_entrypoint.py /app/bridge.py' in scripts,
          workflow + " must test the complete script entrypoint inside the image")
dockerfile = (REPO_ROOT / "xiaoai_bridge" / "Dockerfile").read_text(encoding="utf-8")
check("python /tmp/test_entrypoint.py /app/bridge.py" in dockerfile,
      "Docker builds must reject broken script entrypoints")

# ───────────────────────────────────── report ─────────────────────────────────────
if failures:
    print(f"workflow policy: {checks} checks, {len(failures)} FAILED")
    for failure in failures:
        print(f"  FAIL {failure}")
    sys.exit(1)

print(f"workflow policy tests passed ({checks} checks)")
