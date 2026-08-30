"""Add fixture root to sys.path so `from publisher import ...` works."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
