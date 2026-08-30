#!/bin/sh
# Proves the provider serves BOTH implementations during the port.
# Interlock passes the symbols in the environment; it knows nothing about C.
old="${INTERLOCK_OLD_SYMBOL:-calc_legacy_c}"
new="${INTERLOCK_NEW_SYMBOL:-calc_py}"

grep -q "$old" calc.c || { echo "FAIL: legacy $old gone; un-ported callers would break"; exit 1; }
grep -q "$new" calc.py || { echo "FAIL: replacement $new absent; nothing to port to"; exit 1; }
echo "OK: coexistence holds - $old (C) and $new (Python) both live"
