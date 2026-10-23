"""openEuler resource control Agent."""
__version__ = "0.1.3"


def _prepare_checked_cache():
    # Source archives use deterministic mtimes. Timestamp/size caches can therefore
    # accept different same-size source; PEP 552 checked hashes prevent that reuse.
    import importlib.util
    import py_compile
    from pathlib import Path
    for source in Path(__file__).resolve().parent.glob("*.py"):
        cache = Path(importlib.util.cache_from_source(str(source)))
        try:
            data = cache.read_bytes() if cache.is_file() else b""
            valid = (len(data) >= 16 and data[:4] == importlib.util.MAGIC_NUMBER
                     and int.from_bytes(data[4:8], "little") == 3
                     and data[8:16] == importlib.util.source_hash(source.read_bytes()))
            if not valid:
                py_compile.compile(str(source), cfile=str(cache), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
        except (OSError, py_compile.PyCompileError) as exc:
            raise RuntimeError(f"Cannot prepare content-checked Agent cache for {source}: {exc}") from exc


_prepare_checked_cache()
del _prepare_checked_cache

