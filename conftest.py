import os
import sys
from pathlib import Path

# Pin the data anchor for reproducible tests — same knob the deployment uses.
os.environ.setdefault("LEDGERPILOT_DATA_DATE", "2026-10-18")

sys.path.insert(0, str(Path(__file__).parent))
