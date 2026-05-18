import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def _spawn(module_name: str, run_once: bool):
    env = os.environ.copy()
    command = [sys.executable, "-m", module_name]
    if run_once:
        command.append("--once")
    return subprocess.Popen(
        command,
        cwd=str(ROOT_DIR),
        env=env,
    )


def run_selected(run_bot: bool, run_executor: bool, run_settlement: bool, run_once: bool = False):
    processes = []
    try:
        if run_bot:
            processes.append(_spawn("pyapp.bot", run_once))
        if run_executor:
            processes.append(_spawn("pyapp.executor", run_once))
        if run_settlement:
            processes.append(_spawn("pyapp.settlement", run_once))

        if not processes:
            print("No Python app components selected. Use --executor, --settlement, or --all.")
            return 1

        exit_code = 0
        for process in processes:
            code = process.wait()
            if code != 0 and exit_code == 0:
                exit_code = code
        return exit_code
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except Exception:
                if process.poll() is None:
                    process.kill()


def main():
    parser = argparse.ArgumentParser(description="Run Python app-side Blocky components.")
    parser.add_argument("--bot", action="store_true", help="Run the Python Telegram bot")
    parser.add_argument("--executor", action="store_true", help="Run the Python executor")
    parser.add_argument("--settlement", action="store_true", help="Run the Python settlement monitor")
    parser.add_argument("--all", action="store_true", help="Run all available Python app components")
    parser.add_argument("--once", action="store_true", help="Run each selected component once and exit")
    args = parser.parse_args()

    run_bot = args.all or args.bot
    run_executor = args.all or args.executor
    run_settlement = args.all or args.settlement
    raise SystemExit(run_selected(run_bot, run_executor, run_settlement, run_once=args.once))


if __name__ == "__main__":
    main()
