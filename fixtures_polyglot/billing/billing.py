"""billing — migrated to the Python core."""
from calc import calc_py


def invoice(amount: int) -> int:
    return calc_py(amount)
