# Two more tables that only ever grew (host-side). Run:
#   python3 firmware/tests/test_table_bounds.py
#
# Found by a growth hunt (synthetic announce flood + LXMF + path requests):
#   - Transport._path_request_times: a request sent without a waiter (LXMF retries) was never
#     popped, one entry per destination ever asked for.
#   - LXMRouter.delivered_ids: expiry after 30 days only, and an inbound message is recorded even
#     when its source is unknown.
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import Transport, reset_transport

lxmf = importlib.import_module("urns.lxmf")


def test_path_request_times_are_culled_after_the_keep_window():
    reset_transport()
    Transport._path_request_times.clear()
    now = time.time()
    old, fresh = b"o" * 16, b"f" * 16
    Transport._path_request_times[old] = now - 3600
    Transport._path_request_times[fresh] = now - 5
    Transport._cull_tables()
    assert old not in Transport._path_request_times
    assert fresh in Transport._path_request_times
    for i in range(500):                       # a long-running node asking for many peers
        Transport._path_request_times[i.to_bytes(2, "big") * 8] = now - 3600 - i
    Transport._cull_tables()
    assert list(Transport._path_request_times) == [fresh]


def test_delivered_ids_are_capped_keeping_the_newest():
    r = lxmf.LXMRouter(storagepath=None)
    cap = lxmf.LXMRouter.MAX_DELIVERED_IDS
    base = int(time.time())
    for i in range(cap * 3):
        r.delivered_ids[i.to_bytes(4, "big") * 4] = base + i
        r._clean_delivered_ids()
        assert len(r.delivered_ids) <= cap
    assert len(r.delivered_ids) == cap
    assert (cap * 3 - 1).to_bytes(4, "big") * 4 in r.delivered_ids
    assert (0).to_bytes(4, "big") * 4 not in r.delivered_ids


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for k, v in tests:
        try:
            v()
            print("PASS", k)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("FAIL", k, repr(e))
            failed += 1
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
