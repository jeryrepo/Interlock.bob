from sink import on_account_event


def test_event_is_audited():
    assert on_account_event({"id": "evt-9"})["audited"] == "evt-9"
