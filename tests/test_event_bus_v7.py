import threading
import time

from event_bus import BusEvent, LaunchEventBus, Priority


def _event(mint, ident, kind="NEW_TOKEN"):
    return BusEvent(mint=mint, kind=kind, source="test", event_id=ident,
                    payload={"mint": mint}, received_at=time.time())


def test_priority_ordering_when_queued_before_workers_start():
    seen = []
    bus = LaunchEventBus(lambda event: seen.append(event.mint), workers=1)
    assert bus.enqueue(_event("LOW", "low"), Priority.LOW)
    assert bus.enqueue(_event("HIGH", "high"), Priority.HIGH)
    bus.start()
    deadline = time.time() + 2
    while len(seen) < 2 and time.time() < deadline:
        time.sleep(0.01)
    bus.stop()
    assert seen[:2] == ["HIGH", "LOW"]


def test_duplicate_in_flight_event_is_rejected():
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def handler(event):
        entered.set()
        release.wait(1)
        seen.append(event.event_id)

    bus = LaunchEventBus(handler, workers=1)
    bus.start()
    ev = _event("MINT", "same-id")
    assert bus.enqueue(ev, Priority.HIGH)
    assert entered.wait(1)
    assert not bus.enqueue(_event("MINT", "same-id"), Priority.HIGH)
    release.set()
    deadline = time.time() + 2
    while bus.health()["processed"] < 1 and time.time() < deadline:
        time.sleep(0.01)
    bus.stop()
    assert seen == ["same-id"]
    assert bus.health()["duplicates_dropped"] == 1


def test_events_for_same_mint_are_serialized():
    state = {"now": 0, "maximum": 0}
    state_lock = threading.Lock()

    def handler(event):
        with state_lock:
            state["now"] += 1
            state["maximum"] = max(state["maximum"], state["now"])
        time.sleep(0.03)
        with state_lock:
            state["now"] -= 1

    bus = LaunchEventBus(handler, workers=4)
    bus.start()
    for i in range(4):
        assert bus.enqueue(_event("ONE_MINT", f"ev-{i}"), Priority.NORMAL)
    deadline = time.time() + 3
    while bus.health()["processed"] < 4 and time.time() < deadline:
        time.sleep(0.01)
    bus.stop()
    assert state["maximum"] == 1
    assert bus.health()["processed"] == 4


def test_queue_saturation_is_counted_not_silently_ignored():
    bus = LaunchEventBus(lambda event: None, max_queue_size=1, workers=1)
    assert bus.enqueue(_event("A", "a"), Priority.HIGH)
    assert not bus.enqueue(_event("B", "b"), Priority.HIGH)
    assert bus.health()["queue_depth"] == 1
    assert bus.health()["dropped_on_full"] == 1
    bus.stop(drain=False)
