"""Local-only operations (run on the PC, never remotely):
  python ops.py stop      kill switch ON  (creates STOP; blocks every order)
  python ops.py resume    kill switch OFF (removes STOP). Deliberately NOT available from Telegram.
  python ops.py status    STOP / live flag / latest plan state
"""
import os
import sys

import execution as ex

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "stop":
        open(ex.STOP_PATH, "w").close()
    elif cmd == "resume":
        if os.path.exists(ex.STOP_PATH):
            os.remove(ex.STOP_PATH)
    elif cmd != "status":
        sys.exit(__doc__)
    p = ex.latest_plan(ex.db())
    print(f"STOP: {'ON' if os.path.exists(ex.STOP_PATH) else 'off'} | LIVE_TRADING_ENABLED: {ex.live_enabled()} | "
          f"latest plan: {dict(p)['id'] if p else '-'} {dict(p)['state'] if p else ''}")
