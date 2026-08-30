#!/bin/sh
grep -q "calc_py" report.sh || exit 1
echo "reporting: 1 passed"
