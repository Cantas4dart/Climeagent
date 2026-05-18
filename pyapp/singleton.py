import atexit
import os
import signal
from pathlib import Path


def _is_process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_existing_pid(lock_path: Path) -> int | None:
    try:
        return int(lock_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def acquire_process_lock(lock_name: str):
    lock_dir = Path(__file__).resolve().parent.parent / "data" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in lock_name).lower()
    lock_path = lock_dir / f"{safe_name}.lock"

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            break
        except FileExistsError:
            existing_pid = _read_existing_pid(lock_path)
            if existing_pid and _is_process_alive(existing_pid):
                print(
                    f"[LOCK] {lock_name} is already running under PID {existing_pid}. "
                    "This duplicate instance will exit."
                )
                return None
            try:
                lock_path.unlink()
            except OSError:
                print(f"[LOCK] Could not remove stale lock for {lock_name} at {lock_path}.")
                return None

    released = False

    def release():
        nonlocal released
        if released:
            return
        released = True
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass

    def _handle_signal(signum, _frame):
        release()
        raise SystemExit(0)

    atexit.register(release)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    return release

