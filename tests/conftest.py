import sys
from pathlib import Path

# Make `scripts` and `src` importable from tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
