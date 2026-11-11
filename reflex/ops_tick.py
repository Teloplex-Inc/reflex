#!/usr/bin/env python3
"""Reflex ops tick — the self-debugging version of the three data steps.
Runs: capture -> hindsight -> L1 scoring. Each step:
  - catches its own errors, logs them to ~/.hermes/reflex/errors.log
  - retries once after a tunnel/server health check
  - reports per-step status so a cron can see exactly what failed
Exit code = number of failed steps (0 = all clean).
This replaces ad-hoc calls in the cron prompt with ONE robust entrypoint."""
import subprocess, sys, os, time, json

STEPS = [
    ("capture",   ["python3", "/home/hermes/.hermes/reflex/capture_tick.py"], {}),
    ("hindsight", ["python3", "/home/hermes/.hermes/reflex/hindsight_tick.py"], {}),
    ("l1_score",  ["python3", "/home/hermes/.hermes/reflex/l1_score_turns.py"], {}),
]
ERR_LOG = os.path.expanduser("~/.hermes/reflex/errors.log")

# Pod connection comes from the environment; nothing about the host is kept in the repo.
#   REFLEX_POD_SSH_HOST  user@host        REFLEX_POD_SSH_PORT  ssh port (default 22)
#   REFLEX_POD_SSH_KEY   path to the private key
# With REFLEX_POD_SSH_HOST unset, the tick still runs but cannot restart tunnels or servers.
POD_HOST = os.environ.get("REFLEX_POD_SSH_HOST")
POD_PORT = os.environ.get("REFLEX_POD_SSH_PORT", "22")
POD_KEY = os.environ.get("REFLEX_POD_SSH_KEY")

def _ssh_base():
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30", "-p", POD_PORT]
    if POD_KEY:
        cmd += ["-i", POD_KEY]
    return cmd

def health_check():
    """Verify tunnels + servers; restart if needed (tunnels are local, servers via SSH)."""
    import urllib.request, subprocess, time
    results = {}
    for port in (8001, 8002):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/v1/models", timeout=5)
            results[port] = "ok"
        except Exception:
            results[port] = "dead"
    if any(v == "dead" for v in results.values()):
        if not POD_HOST:
            return {p: ("no_pod_configured" if v == "dead" else v) for p, v in results.items()}
        # restart tunnels (one ssh, both forwards)
        subprocess.run(["pkill", "-f", "8001:localhost:8000"], capture_output=True)
        subprocess.Popen(_ssh_base() + ["-N", "-L", "8001:localhost:8000", "-L", "8002:localhost:8002", POD_HOST],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(6)
        for port in (8001, 8002):
            try:
                urllib.request.urlopen(f"http://localhost:{port}/v1/models", timeout=5)
                results[port] = "recovered"
            except Exception:
                results[port] = "STILL_DEAD_pod_server_down"
    return results

def run_step(name, cmd, env):
    e = dict(os.environ); e.update(env)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=280, env=e)
        out = (r.stdout or "").strip().splitlines()
        last = out[-1] if out else "(no output)"
        if r.returncode != 0:
            return ("WARN", f"{name}: exit {r.returncode}: {last[:120]} | {r.stderr[-150:]}")
        return ("OK", f"{name}: {last[:120]}")
    except Exception as ex:
        return ("FAIL", f"{name}: {str(ex)[:150]}")

def main():
    report = []
    failures = 0
    for name, cmd, env in STEPS:
        status, msg = run_step(name, cmd, env)
        if status != "OK":
            # self-debug: health check + one retry (unless capture/hindsight which are local-only)
            if name == "l1_score":
                hc = health_check()
                report.append(f"  [debug] health: {hc}")
                if any(v == "STILL_DEAD_pod_server_down" for v in hc.values()):
                    # servers down on pod — restart L1 then L2 via SSH (boot order!)
                    subprocess.run(_ssh_base() + [POD_HOST,
                        "cd /workspace && nohup /opt/vllm-venv/bin/python -m vllm.entrypoints.openai.api_server "
                        "--model /workspace/gemma-1b --served-model-name reflex-v05 --enable-lora "
                        "--lora-modules reflex-v05=/workspace/gemma-1b-lora --max-lora-rank 16 --port 8002 "
                        "--gpu-memory-utilization 0.25 --max-model-len 1024 --max-num-seqs 16 --enforce-eager "
                        "> /workspace/logs/vllm_l1.log 2>&1 &"], capture_output=True, timeout=30)
                    time.sleep(60)  # vllm boot time
                status2, msg2 = run_step(name, cmd, env)
                if status2 == "OK":
                    report.append(f"  [debug] {name} recovered after health check: {msg2[:100]}")
                    status, msg = "OK", msg2
                else:
                    status, msg = status2, f"{msg} | retry-failed: {msg2[:100]}"
        if status != "OK":
            failures += 1
            with open(ERR_LOG, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n")
        report.append(f"  {status}: {msg}")
    print("REFLEX TICK " + ("CLEAN" if failures == 0 else f"{failures} FAILURES"))
    for r in report: print(r)
    sys.exit(failures)

if __name__ == "__main__":
    main()