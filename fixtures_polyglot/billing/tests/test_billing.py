import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from billing import invoice


def test_invoice_uses_python_core():
    assert invoice(21) == 42
