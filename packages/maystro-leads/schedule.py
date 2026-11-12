import fcntl
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

from leads_daily import BASE, load_config, log


def main():
    if not os.environ.get("LEADS_TWENTY_API_KEY", "").strip():
        print("Lead collection is disabled. Set LEADS_TWENTY_API_KEY to enable the daily run.", flush=True)
        Event().wait()
        return

    config = load_config()
    time_zone = ZoneInfo("Africa/Tunis")
    state_dir = Path(os.environ["LEADS_DATA_DIR"])
    last_run = state_dir / "last-scheduled-run"
    log("Collecte planifiée chaque jour à 08:00 (Africa/Tunis)", config)
    while True:
        now = datetime.now(time_zone)
        if now.hour >= 8:
            today = now.date().isoformat()
            with (state_dir / "schedule.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not last_run.exists() or last_run.read_text(encoding="utf-8") != today:
                    last_run.write_text(today, encoding="utf-8")
                    result = subprocess.run([sys.executable, str(BASE / "leads_daily.py"), "run"], check=False)
                    if result.returncode:
                        log(f"La collecte s'est terminée avec le code {result.returncode}.", config)
        time.sleep(30)


if __name__ == "__main__":
    main()
