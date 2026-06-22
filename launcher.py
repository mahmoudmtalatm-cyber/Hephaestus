"""
Render only runs one start command per service. This launches both
bot.py (the builder bot people /start with) and runner.py (which spawns
each admin's own bot) as subprocesses, and keeps the main process alive
as long as both are alive. Only bot.py runs the keep-alive HTTP server
that Render pings to classify this as a live Web Service; runner.py
doesn't need its own port.
"""
import subprocess
import sys
import time

PROCESSES = [
    ("bot.py", ["python", "bot.py"]),
    ("runner.py", ["python", "runner.py"]),
]


def main():
    procs = []
    for name, cmd in PROCESSES:
        print(f"🚀 Launching {name}...")
        procs.append((name, subprocess.Popen(cmd)))

    try:
        while True:
            for name, p in procs:
                ret = p.poll()
                if ret is not None:
                    print(f"⚠️  {name} exited with code {ret}. Restarting...")
                    idx = next(i for i, (n, _) in enumerate(procs) if n == name)
                    cmd = next(c for n, c in PROCESSES if n == name)
                    procs[idx] = (name, subprocess.Popen(cmd))
            time.sleep(5)
    except KeyboardInterrupt:
        for _, p in procs:
            p.terminate()
        sys.exit(0)


if __name__ == "__main__":
    main()
