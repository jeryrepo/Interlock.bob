"""Add fixture root to sys.path so that `from worker import ...` works."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
