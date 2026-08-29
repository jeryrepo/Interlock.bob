"""Tests for fraud service (pre-migration baseline).

These tests operate on the PRE-migration state: account-service responses
carry account_id. After migration, account_id will be replaced by account_id.
"""
from fraud import check_fraud, get_risk_score


def test_clean_customer_not_flagged():
    assert check_fraud({"account_id": "cust-ok"}) is False


def test_high_risk_customer_flagged():
    assert check_fraud({"account_id": "cust-bad"}) is True


def test_risk_score_clean():
    assert get_risk_score({"account_id": "cust-clean"}) == 0.0


def test_risk_score_high():
    assert get_risk_score({"account_id": "cust-bad"}) == 1.0
