"""Optional one-off jobs started at boot, in the background, chosen by environment variables.

Render's free plan has no shell or cron, so START_PY runs this after migrations:

    EVAL_JOB=factory-ads   push new agent decisions to this LangSmith dataset (needs LANGSMITH_API_KEY)
    EVAL_RUN=1             also score the current model and prompt on it (uses model quota)

The web server starts straight away; a job's output goes to the service log.
"""
import os
import subprocess
import sys


def main() -> None:
    dataset = os.environ.get("EVAL_JOB", "").strip()
    if not dataset:
        return
    if not os.environ.get("LANGSMITH_API_KEY"):
        print("EVAL_JOB is set but LANGSMITH_API_KEY is not; skipping the eval job", flush=True)
        return
    cmd = [sys.executable, "-m", "scripts.eval_ads", "job", dataset, "--confirm-privacy"]
    if os.environ.get("EVAL_RUN") == "1":
        cmd.append("--run")
    subprocess.Popen(cmd)  # detached: boot carries on to the web server
    print(f"eval job started: {' '.join(cmd[2:])}", flush=True)


if __name__ == "__main__":
    main()
