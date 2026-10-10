"""Import only the source checkout; never the operator's installed launcher."""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT/'dashboard'))
