import time

from retry_policy import ReasonAggregator, RetryPolicy, RetryScheduler


def test_exponential_backoff_caps_and_retry_after():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=8, max_attempts=3, jitter_fraction=0)
    assert policy.delay_for(0) == 1
    assert policy.delay_for(1) == 2
    assert policy.delay_for(8) == 8
    assert policy.delay_for(0, retry_after_seconds=20) == 8


def test_retry_budget_exhausts_explicitly():
    scheduler = RetryScheduler(RetryPolicy(base_delay_seconds=0, max_delay_seconds=1,
                                           max_attempts=2, jitter_fraction=0))
    scheduler.schedule("M", "DATA_MISSING")
    scheduler.schedule("M", "DATA_MISSING")
    final = scheduler.schedule("M", "DATA_MISSING")
    assert final.exhausted is True
    assert final.next_retry_at is None
    assert scheduler.should_wait("M") is True


def test_due_retry_claim_is_single_and_release_requeues():
    scheduler = RetryScheduler(RetryPolicy(base_delay_seconds=0, max_delay_seconds=1,
                                           max_attempts=3, jitter_fraction=0))
    scheduler.schedule("M", "TIMEOUT")
    claimed = scheduler.claim_due(now=time.time() + 2)
    assert len(claimed) == 1 and claimed[0][0] == "M"
    assert scheduler.should_wait("M")
    assert scheduler.claim_due(now=time.time() + 2) == []
    scheduler.release_after_enqueue_failure("M", 0.25)
    assert scheduler.claim_due(now=time.time() + 2)


def test_reason_aggregation_keeps_one_record_per_mint_reason():
    reasons = ReasonAggregator()
    for i in range(10):
        reasons.record("M", "MISSING_FREEZE_AUTHORITY", f"attempt {i}")
    record = reasons.for_mint("M")["MISSING_FREEZE_AUTHORITY"]
    assert record["count"] == 10
    assert record["first_seen_at"] <= record["last_seen_at"]
    assert reasons.global_counts()[0] == ("MISSING_FREEZE_AUTHORITY", 10)
