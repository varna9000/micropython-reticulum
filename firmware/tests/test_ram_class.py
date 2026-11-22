"""const._BIG_RAM: which boards get the large tables (decided by free heap at import)."""
import importlib.util
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (micropython shims)

CONST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "urns", "const.py")


def _load_with_free_heap(free):
    fake_gc = types.ModuleType("gc")
    fake_gc.mem_free = lambda: free
    real = sys.modules.get("gc")
    sys.modules["gc"] = fake_gc
    try:
        spec = importlib.util.spec_from_file_location("const_probe", CONST)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if real is not None:
            sys.modules["gc"] = real
        else:
            del sys.modules["gc"]


def test_2mb_psram_board_gets_the_large_tables():
    c = _load_with_free_heap(2_060_896)          # measured on the 2 MB-PSRAM ESP32-S3
    assert c._BIG_RAM and c.MAX_KNOWN_DESTINATIONS == 768 and c.MAX_PATH_TABLE == 256


def test_board_with_1_8_mb_free_gets_the_large_tables():
    assert _load_with_free_heap(1_800_000)._BIG_RAM


def test_no_psram_boards_keep_the_small_tables():
    for free in (95_000, 200_000, 300_000, 900_000):   # Wireless Paper, RP2040, plain S3
        c = _load_with_free_heap(free)
        assert not c._BIG_RAM and c.MAX_KNOWN_DESTINATIONS == 160 and c.MAX_PATH_TABLE == 32


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok " + t.__name__)
    print("\n%d/%d passed" % (len(tests), len(tests)))
