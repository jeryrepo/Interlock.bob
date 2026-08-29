"""Add fixture root to sys.path so that `from app import ...` works."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
