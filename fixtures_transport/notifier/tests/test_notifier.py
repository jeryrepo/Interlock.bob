from notifier import handle_account_event


def test_event_is_handled():
    assert handle_account_event({"id": "evt-1"})["notified"] == "evt-1"
