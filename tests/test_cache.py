"""Reproduce deterministic-mtime stale caches and verify content invalidation."""
import importlib.util
import os
import py_compile
import tempfile
import unittest
from pathlib import Path


class CacheChecks(unittest.TestCase):
    def test_same_size_same_mtime_change_is_content_checked(self):
        root = Path(__file__).resolve().parents[1]
        base = root / "state/test-cache"
        if not base.resolve().is_relative_to(root):
            raise RuntimeError("Test cache escaped the project")
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            if not Path(directory).resolve().is_relative_to(base.resolve()):
                raise RuntimeError("Test temporary directory escaped its checked base")
            source = Path(directory) / "fixture.py"
            def write(value):
                source.write_text(f"value = {value}\n", encoding="utf-8")
                os.utime(source, (0, 0))
            def read():
                spec = importlib.util.spec_from_file_location("oeagent_cache_fixture", source)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.value
            write(1)
            self.assertEqual(read(), 1)
            write(2)
            # Reproduce the timestamp/size collision in an isolated fixture.
            self.assertEqual(read(), 1)
            py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
            self.assertEqual(read(), 2)
            write(3)
            self.assertEqual(read(), 3)


if __name__ == "__main__":
    unittest.main()
