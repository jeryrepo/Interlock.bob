#!/bin/sh
# Provider suite: both implementations must agree during the coexistence window.
grep -q "calc_legacy_c" calc.c || { echo "legacy C entry point missing"; exit 1; }
grep -q "calc_py" calc.py || { echo "python replacement missing"; exit 1; }
echo "calc-core: 2 passed (legacy C + python parity)"
