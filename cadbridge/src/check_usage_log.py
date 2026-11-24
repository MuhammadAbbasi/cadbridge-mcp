"""Offline check of the usage log's shared-folder backlog (no CAD needed): python check_usage_log.py

Lines written while the shared folder is unreachable must reach it, in order, at the next successful write.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["LOCALAPPDATA"] = str(tmp / "local")
blocker = tmp / "not_a_folder"
blocker.write_text("x")
os.environ["CADBRIDGE_LOG_DIR"] = str(blocker / "logs")  # under a file: every write fails
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cadbridge_mcp as m  # noqa: E402  (logs server_start: during the outage)

m.log_event("during_outage")
assert m._shared_down and m._PENDING.exists(), "the outage was not noticed"

shared_dir = tmp / "share"
os.environ["CADBRIDGE_LOG_DIR"] = str(shared_dir)  # the share is back
m.log_event("after_outage")

(shared_file,) = shared_dir.rglob("*.jsonl")
events = [json.loads(x)["event"] for x in shared_file.read_text(encoding="utf-8").splitlines()]
assert events == ["server_start", "shared_log_unreachable", "during_outage", "after_outage"], events
assert not m._PENDING.exists(), "the backlog was not cleared"
assert not m._shared_down
local = [json.loads(x)["event"] for x in m._LOCAL_LOG.read_text(encoding="utf-8").splitlines()]
assert local == events, local  # the local log has everything, once
print("usage log backlog: ok")
