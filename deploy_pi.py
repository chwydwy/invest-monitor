"""Deploy an exact Git commit to the Pi after local and remote checks.

Only explicitly staged files can be committed. The script never stages files,
changes Pi configuration, touches the outbox, or enables the sender timer.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOST = "pi5"
REMOTE_ROOT = "/home/pi5dart/workspace"
SHA = re.compile(r"^[0-9a-f]{40}$")
SECRET_PATHS = {
    "dart_monitor/config.json", "news_monitor/config_news.json",
    "stock_monitor/config_price.json", "news_monitor/sent_articles.json",
    "news_monitor/daily_sent_cache.json",
    "ai-pipeline.json", "ai_pipeline_settings.json", "operations-report.json", "operations-report.snapshot.json",
    "operations_report_settings.json",
}
SECRET_SUFFIXES = (".sqlite3", ".sqlite3-shm", ".sqlite3-wal", ".db", ".pem", ".key", ".token", ".secret", ".env")


class DeployError(RuntimeError):
    pass


def command(argv, *, input_text=None, cwd=ROOT, label=None):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(argv, input=input_text, text=True, capture_output=True, cwd=cwd, env=env)
    if result.returncode:
        reason = ""
        if label and label.startswith("Pi SSH"):
            for phrase in ("Permission denied", "Pi has uncommitted changes", "Pi HEAD changed since preflight",
                           "Pi branch is not main", "a monitored service is not active",
                           "origin/main does not match requested commit", "requested commit is not a fast-forward",
                           "change requires an unmapped service", "service not active after deployment",
                           "service reports failure after deployment", "Pi working tree changed after deployment",
                           "noninteractive restart permission unavailable"):
                if phrase in result.stderr:
                    reason = " - " + ("SSH authentication unavailable" if phrase == "Permission denied" else phrase)
                    break
        raise DeployError(f"{label or argv[0]} failed (exit {result.returncode}){reason}")
    return result.stdout.strip()


def git(*args):
    return command(["git", *args], label="git " + args[0])


def is_secret(path):
    lower = path.lower().replace("\\", "/")
    name = lower.rsplit("/", 1)[-1]
    return (lower in SECRET_PATHS or name.startswith(".env") or
            any(name.endswith(suffix) for suffix in SECRET_SUFFIXES) or
            "/private/" in f"/{lower}/")


def validate_paths(paths):
    blocked = [path for path in paths if is_secret(path)]
    if blocked:
        raise DeployError("secret or runtime paths are staged/tracked; deployment stopped")


def validate_service_scope(paths):
    for path in paths:
        if path.startswith("stock_monitor/") or path.endswith("requirements.txt"):
            raise DeployError("change requires an unmapped service or dependency install")
        if (path.endswith(".py") and not path.startswith(("dart_monitor/", "news_monitor/"))
                and path not in ("dashboard_outbox.py", "dashboard_sender.py", "deploy_pi.py",
                                 "test_dashboard_outbox.py", "test_deploy_pi.py", "test_disclosure_targets.py",
                                 "ai_pipeline.py", "ai_worker.py", "ai_pipeline_cli.py", "news_scan.py", "pipeline_runtime.py",
                                 "test_ai_pipeline.py", "test_pipeline_integration.py", "test_disclosure_filters.py", "operations_metrics.py", "operations_report.py", "install_operations_token.py", "test_operations_report.py", "news_selection.py", "news_cutover.py", "test_news_selection.py")):
            raise DeployError("change requires an unmapped service")


def required_restarts(paths, pipeline=False):
    if pipeline:
        changed = [p for p in paths if not p.rsplit("/",1)[-1].startswith("test_")]
        if any(p.endswith((".py", ".txt", ".json")) and (p.startswith(("dart_monitor/", "news_monitor/")) or p in ("ai_pipeline.py", "ai_worker.py", "news_scan.py", "pipeline_runtime.py", "operations_metrics.py", "news_selection.py", "DASHBOARD_COMPANIES.json", "DASHBOARD_COMPANY_ALIASES.json")) for p in changed):
            return {"monitor-pipeline@" + role + ".service" for role in ("dart","news","preprocess","ai","deliver")}
        return {"monitor-pipeline@deliver.service"} if "dashboard_outbox.py" in paths else set()
    restart = set()
    for path in paths:
        if ((path.startswith("dart_monitor/") and path.endswith((".py", ".txt", ".json"))
             and not path.startswith("dart_monitor/test")) or path == "dashboard_outbox.py"):
            restart.add("dart.service")
        if ((path.startswith("news_monitor/") and path.endswith((".py", ".txt", ".json"))
             and not path.startswith("news_monitor/test")) or path == "dashboard_outbox.py"):
            restart.add("news.service")
    return restart


def local_check():
    if git("branch", "--show-current") != "main":
        raise DeployError("local branch must be main")
    changed = git("status", "--porcelain=v1", "--untracked-files=all")
    if changed:
        # Work must be explicitly staged before a commit. Unstaged or untracked
        # files can affect tests but cannot be reproduced from the Git commit.
        for line in changed.splitlines():
            if line.startswith("??") or line[1] != " ":
                raise DeployError("unstaged or untracked local changes; deployment stopped")
    staged = [p for p in git("diff", "--cached", "--name-only").splitlines() if p]
    validate_paths(staged)
    validate_service_scope(staged)
    tracked = [p for p in git("ls-files").splitlines() if p]
    validate_paths(tracked)
    git("diff", "--cached", "--check")
    command([sys.executable, "-m", "py_compile", "dashboard_outbox.py", "dashboard_sender.py",
             "dart_monitor/dart.py", "news_monitor/news.py"], label="Python syntax check")
    command([sys.executable, "-m", "unittest", "test_dashboard_outbox", "test_ai_pipeline", "test_pipeline_integration", "news_monitor.test_news_quality", "news_monitor.test_naver_news", "test_deploy_pi", "test_disclosure_filters", "test_operations_report", "test_news_selection", "-q"], label="local outbox tests")
    command([sys.executable, "-m", "unittest", "test_disclosure_targets", "-v"], label="local disclosure target tests")
    return staged


REMOTE_PROGRAM = r'''
import json, os, re, subprocess, sys
from pathlib import Path

root = "/home/pi5dart/workspace"
mode, expected, target = sys.argv[1:4]
restart_only = sys.argv[4].split(',') if len(sys.argv)>4 else None
if not all(re.fullmatch(r"[0-9a-f]{40}", x) for x in (expected, target)):
    raise SystemExit("invalid commit identifier")

def run(*args, check=True):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(args, cwd=root, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise SystemExit("command failed: " + args[0] + " " + args[1])
    return p.stdout.strip(), p.returncode

if run("id", "-un")[0] != "pi5dart":
    raise SystemExit("unexpected service account")
if run("git", "branch", "--show-current")[0] != "main":
    raise SystemExit("Pi branch is not main")
if run("git", "status", "--porcelain=v1", "--untracked-files=all")[0]:
    raise SystemExit("Pi has uncommitted changes")
head = run("git", "rev-parse", "HEAD")[0]
if head != expected:
    raise SystemExit("Pi HEAD changed since preflight")

services = {}
restart_authorized = {}
policy, policy_status = run("sudo", "-n", "-l", check=False)
policy_lines = policy.splitlines() if policy_status == 0 else []
broad_passwordless = any(re.search(r"\bNOPASSWD:\s*ALL\b", line) for line in policy_lines)
pipeline = run("systemctl", "is-enabled", "monitor-pipeline@ai.service", check=False)[0] == "enabled"
units = tuple("monitor-pipeline@" + role + ".service" for role in ("dart","news","preprocess","ai","deliver")) if pipeline else ("dart.service", "news.service")
for unit in units:
    state, _ = run("systemctl", "is-active", unit, check=False)
    services[unit] = state
    exact = "/usr/bin/systemctl restart " + unit
    restart_authorized[unit] = broad_passwordless or run("sudo", "-n", "-l", "/usr/bin/systemctl", "restart", unit, check=False)[1] == 0 or any("NOPASSWD:" in line and exact in line for line in policy_lines)
timer, _ = run("systemctl", "is-enabled", "dashboard-sender.timer", check=False)
if mode == "check":
    print(json.dumps({"head": head, "branch": "main", "pipeline": pipeline, "services": services,
                      "sender_timer": timer, "restart_authorized": restart_authorized,
                      "broad_passwordless_sudo": broad_passwordless}, sort_keys=True))
    raise SystemExit(0)
if mode not in ("apply", "transition"):
    raise SystemExit("unknown mode")
if mode == "transition" and any(state == "active" for state in services.values()):
    raise SystemExit("transition requires old services stopped")
if mode == "apply" and any(state != "active" for state in services.values()):
    raise SystemExit("a monitored service is not active; no code applied")

run("git", "fetch", "origin", "main")
if run("git", "rev-parse", "FETCH_HEAD")[0] != target:
    raise SystemExit("origin/main does not match requested commit")
if run("git", "merge-base", "--is-ancestor", head, target, check=False)[1]:
    raise SystemExit("requested commit is not a fast-forward")
changed = run("git", "diff", "--name-only", head, target)[0].splitlines()
if any(path.startswith("stock_monitor/") or path.endswith("requirements.txt") or
       (path.endswith(".py") and not path.startswith(("dart_monitor/", "news_monitor/"))
        and path not in ("dashboard_outbox.py", "dashboard_sender.py", "deploy_pi.py", "test_dashboard_outbox.py", "test_deploy_pi.py", "test_disclosure_targets.py",
                                 "ai_pipeline.py", "ai_worker.py", "ai_pipeline_cli.py", "news_scan.py", "pipeline_runtime.py",
                                 "test_ai_pipeline.py", "test_pipeline_integration.py", "test_disclosure_filters.py", "operations_metrics.py", "operations_report.py", "install_operations_token.py", "test_operations_report.py", "news_selection.py", "news_cutover.py", "test_news_selection.py"))
       for path in changed):
    raise SystemExit("change requires an unmapped service; no code applied")
def required_restarts(paths, pipeline=False):
    if pipeline:
        changed = [p for p in paths if not p.rsplit("/",1)[-1].startswith("test_")]
        if any(p.endswith((".py", ".txt", ".json")) and (p.startswith(("dart_monitor/", "news_monitor/")) or p in ("ai_pipeline.py", "ai_worker.py", "news_scan.py", "pipeline_runtime.py", "operations_metrics.py", "news_selection.py", "DASHBOARD_COMPANIES.json", "DASHBOARD_COMPANY_ALIASES.json")) for p in changed):
            return {"monitor-pipeline@" + role + ".service" for role in ("dart","news","preprocess","ai","deliver")}
        return {"monitor-pipeline@deliver.service"} if "dashboard_outbox.py" in paths else set()
    restart = set()
    for path in paths:
        if ((path.startswith("dart_monitor/") and path.endswith((".py", ".txt", ".json"))
             and not path.startswith("dart_monitor/test")) or path == "dashboard_outbox.py"):
            restart.add("dart.service")
        if ((path.startswith("news_monitor/") and path.endswith((".py", ".txt", ".json"))
             and not path.startswith("news_monitor/test")) or path == "dashboard_outbox.py"):
            restart.add("news.service")
    return restart
restart = set() if mode == "transition" else required_restarts(changed,pipeline)
if restart_only is not None:
    selected=set(restart_only)
    if not pipeline or not selected or not selected <= restart:
        raise SystemExit("invalid scoped restart selection")
    restart=selected

for unit in sorted(restart):
    if not restart_authorized[unit]:
        raise SystemExit("noninteractive restart permission unavailable")
run("git", "merge", "--ff-only", target)
if run("git", "rev-parse", "HEAD")[0] != target:
    raise SystemExit("applied commit mismatch")
for unit in sorted(restart):
    run("sudo", "-n", "/usr/bin/systemctl", "restart", unit)
for unit in (() if mode == "transition" else units):
    state, _ = run("systemctl", "is-active", unit, check=False)
    services[unit] = state
    if state != "active":
        raise SystemExit("service not active after deployment: " + unit)
    result = run("systemctl", "show", unit, "--property=Result", "--value")[0]
    if result != "success":
        raise SystemExit("service reports failure after deployment: " + unit)
if run("git", "status", "--porcelain=v1", "--untracked-files=all")[0]:
    raise SystemExit("Pi working tree changed after deployment")
print(json.dumps({"previous": head, "applied": target, "restarted": sorted(restart),
                  "services": services, "sender_timer": timer}, sort_keys=True))
'''


def remote(mode, expected, target, restart_only=None):
    if not SHA.fullmatch(expected) or not SHA.fullmatch(target):
        raise DeployError("invalid commit identifier")
    allowed={"monitor-pipeline@"+role+".service" for role in ('dart','news','preprocess','ai','deliver')}
    if restart_only and not set(restart_only)<=allowed:
        raise DeployError('invalid scoped restart selection')
    extra=[','.join(restart_only)] if restart_only else []
    output = command(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST,
                      "python3", "-", mode, expected, target,*extra], input_text=REMOTE_PROGRAM,
                     label="Pi SSH " + mode)
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise DeployError("Pi returned an invalid deployment report") from None


def remote_head():
    output = command(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST,
                      "git", "-C", REMOTE_ROOT, "rev-parse", "HEAD"], label="Pi SSH preflight")
    if not SHA.fullmatch(output):
        raise DeployError("Pi returned an invalid commit identifier")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check or deploy an exact Git commit to Pi")
    parser.add_argument("mode", choices=("check", "apply"))
    parser.add_argument("--transition", action="store_true", help="apply code only after old services are stopped; install roles separately")
    parser.add_argument("--restart-only",nargs='+',choices=('dart','news','preprocess','ai','deliver'),help="explicit reviewed role subset for this deployment")
    parser.add_argument("--message", help="commit message for explicitly staged files")
    args = parser.parse_args(argv)
    staged = local_check()
    if staged and args.mode == "apply" and not args.message:
        raise DeployError("staged files require --message")
    head = git("rev-parse", "HEAD")
    pi_head = remote_head()
    report = remote("check", pi_head, head)
    print("preflight:", json.dumps(report, sort_keys=True))
    changes_since_pi = [p for p in git("diff", "--name-only", pi_head, head).splitlines() if p]
    validate_service_scope(changes_since_pi)
    needed = set() if args.transition else required_restarts([*changes_since_pi, *staged],report.get("pipeline",False))
    selected=None
    if args.restart_only:
        selected={"monitor-pipeline@"+role+".service" for role in args.restart_only}
        if not report.get('pipeline') or not selected <= needed or args.transition:
            raise DeployError('invalid scoped restart selection')
        needed=selected
    missing = sorted(unit for unit in needed if not report.get("restart_authorized", {}).get(unit, False))
    if missing:
        raise DeployError("noninteractive restart permission unavailable for " + ", ".join(missing))
    if args.mode == "check":
        return 0
    git("merge-base", "--is-ancestor", pi_head, head)
    origin_line = git("ls-remote", "origin", "refs/heads/main").split()
    if not origin_line or not SHA.fullmatch(origin_line[0]):
        raise DeployError("origin/main did not return a commit")
    git("merge-base", "--is-ancestor", origin_line[0], head)
    if staged:
        git("commit", "-m", args.message)
    target = git("rev-parse", "HEAD")
    # A remote branch that advanced independently must never be overwritten.
    origin_line = git("ls-remote", "origin", "refs/heads/main").split()
    if not origin_line or not SHA.fullmatch(origin_line[0]):
        raise DeployError("origin/main did not return a commit")
    git("merge-base", "--is-ancestor", origin_line[0], target)
    git("push", "origin", "main")
    published = git("ls-remote", "origin", "refs/heads/main").split()[0]
    if published != target:
        raise DeployError("published commit differs from requested commit")
    if selected:
        applied=remote('apply',pi_head,target,sorted(selected))
    else:
        applied = remote("transition" if args.transition else "apply", pi_head, target)
    print("applied:", json.dumps(applied, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeployError as exc:
        print("deployment stopped:", exc, file=sys.stderr)
        raise SystemExit(1)
