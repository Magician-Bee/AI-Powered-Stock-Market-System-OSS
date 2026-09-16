"""A malformed fetch must retain the original failure, not break failure recording."""
import pytest

from stock_ai.data_platform.incremental import IncrementalLoader
from stock_ai.data_platform.service import MarketDataPlatform


@pytest.mark.parametrize("metadata", [None, []])
def test_non_mapping_fetch_metadata_records_original_type_error(tmp_path, metadata):
    platform = MarketDataPlatform(database_path=tmp_path / "isolated.sqlite")
    with pytest.raises(TypeError, match="incremental fetch metadata must be a dict"):
        IncrementalLoader(platform).run(source_id="twse_openapi", dataset="security_master",
            partition_key="isolated_failure", fetch=lambda cursor: ([], "next", metadata),
            persist=lambda records, acquired: pytest.fail("invalid fetch must not persist"),
            as_of="2026-09-12T12:00:00+00:00")
    checkpoint = platform.warehouse.get_checkpoint(source_id="twse_openapi", dataset="security_master", partition_key="isolated_failure")
    assert checkpoint["status"] == "failed"
    assert checkpoint["error"] == {"type": "TypeError", "message": "incremental fetch metadata must be a dict"}
