from dataclasses import replace

from open_stock_ai.config.settings import load_settings
from open_stock_ai.runtime import clear_runtime_engine_cache, get_runtime_engine, runtime_engine_cache_info


def test_runtime_engine_is_reused_per_effective_configuration(tmp_path):
    clear_runtime_engine_cache()
    settings = replace(load_settings(), sqlite_path=str(tmp_path / "one.sqlite"))

    first = get_runtime_engine(settings)
    second = get_runtime_engine(settings)

    assert first is second
    assert runtime_engine_cache_info() == {"size": 1, "max_size": 8}


def test_runtime_engine_separates_paper_ledgers(tmp_path):
    clear_runtime_engine_cache()
    first_settings = replace(load_settings(), sqlite_path=str(tmp_path / "one.sqlite"))
    second_settings = replace(load_settings(), sqlite_path=str(tmp_path / "two.sqlite"))

    first = get_runtime_engine(first_settings)
    second = get_runtime_engine(second_settings)

    assert first is not second
    assert runtime_engine_cache_info()["size"] == 2
