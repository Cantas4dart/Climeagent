"""
Climeagent - Brain (Signal Engine)
Runs on a loop, scanning weather markets and generating trade signals.
"""
import time
import sys
import os

# Ensure we can import sibling modules when run directly
sys.path.insert(0, os.path.dirname(__file__))

from signals import SignalGenerator
from console import safe_print
from singleton import acquire_process_lock

SCAN_INTERVAL = 300  # 5 minutes


def main():
    release_lock = acquire_process_lock("signal-brain")
    if not release_lock:
        return

    safe_print("=" * 50)
    safe_print("  Climeagent Brain - Signal Engine")
    safe_print("=" * 50)

    gen = SignalGenerator()

    while True:
        try:
            safe_print(f"\n[BRAIN] Starting market scan...")
            gen.run()
            safe_print(f"[BRAIN] Scan complete. Sleeping {SCAN_INTERVAL}s...")
        except KeyboardInterrupt:
            safe_print("\n[BRAIN] Shutting down gracefully.")
            break
        except Exception as e:
            safe_print(f"[BRAIN ERROR] {e}")
            import traceback
            traceback.print_exc()

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
