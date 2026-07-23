"""Harness chạy test bằng stdlib (môi trường sandbox không cài được pytest).

Trên máy bạn (đủ mạng) chỉ cần: pip install pytest && pytest -q
"""
import inspect
import importlib.util
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

# Console Windows mặc định cp1252 không encode được tiếng Việt -> ép UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---- stub các module không có trong sandbox để import module-level không chết ----
for name in ("feedparser", "requests", "apscheduler", "apscheduler.schedulers",
             "apscheduler.schedulers.background"):
    if name not in sys.modules:
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = types.ModuleType(name)
# BackgroundScheduler chỉ cần tồn tại để import; không dùng trong test.
if not hasattr(sys.modules["apscheduler.schedulers.background"], "BackgroundScheduler"):
    sys.modules["apscheduler.schedulers.background"].BackgroundScheduler = object

# ---- pytest shim tối thiểu ----
pytest_stub = types.ModuleType("pytest")

@contextmanager
def _raises(exc):
    try:
        yield
    except exc:
        return
    except Exception as e:  # sai loại exception
        raise AssertionError(f"Kỳ vọng {exc}, nhận {type(e)}: {e}")
    raise AssertionError(f"Kỳ vọng ném {exc} nhưng không có exception")

pytest_stub.raises = _raises
sys.modules["pytest"] = pytest_stub

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def _load_test(name: str):
    """Nạp test theo đường dẫn để không bị package ``tests`` bên thứ ba che khuất."""
    path = ROOT / "tests" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"vrs_tests.{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Không nạp được {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


t1 = _load_test("test_video_ops")
t2 = _load_test("test_store")
t3 = _load_test("test_misc")
t4 = _load_test("test_config")
t5 = _load_test("test_audio")
t6 = _load_test("test_edit_service")
t7 = _load_test("test_local_source")
t8 = _load_test("test_subtitles")
t9 = _load_test("test_features")
t10 = _load_test("test_stages")
t11 = _load_test("test_queue")
t12 = _load_test("test_sqlite_store")
t13 = _load_test("test_smart_crop")


def run_module(mod) -> tuple[int, int]:
    passed = failed = 0
    for fname, fn in inspect.getmembers(mod, inspect.isfunction):
        if not fname.startswith("test_"):
            continue
        kwargs = {}
        if "tmp_path" in inspect.signature(fn).parameters:
            kwargs["tmp_path"] = Path(tempfile.mkdtemp())
        try:
            fn(**kwargs)
            print(f"  PASS {mod.__name__}.{fname}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {mod.__name__}.{fname}: {e}")
            failed += 1
    return passed, failed


def main() -> None:
    total_p = total_f = 0
    for mod in (t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, t11, t12, t13):
        print(f"== {mod.__name__} ==")
        p, f = run_module(mod)
        total_p += p
        total_f += f
    print(f"\nKẾT QUẢ: {total_p} pass, {total_f} fail")
    sys.exit(1 if total_f else 0)


if __name__ == "__main__":
    main()
