from penhin.cli.prompt_queue import PromptQueue


def test_restore_all_returns_pending_messages_in_submission_order() -> None:
    queue = PromptQueue()
    queue.submit("first")
    queue.submit("second")

    assert queue.restore_all() == ["first", "second"]
    assert queue.restore_all() == []
