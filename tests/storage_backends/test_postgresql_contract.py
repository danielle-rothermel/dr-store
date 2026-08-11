from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dr_store import PostgresBackend, install_postgres
from tests.storage_backends import test_contract as contract_tests

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
async def backend(postgres_engine: AsyncEngine) -> PostgresBackend:
    await install_postgres(postgres_engine)
    return await PostgresBackend.open(postgres_engine)


test_put_absent_replay_and_competing_value = (
    contract_tests.test_put_absent_replay_and_competing_value
)
test_get_prefers_exact_then_alternate_schema = (
    contract_tests.test_get_prefers_exact_then_alternate_schema
)
test_bind_absent_replay_and_competing_reference = (
    contract_tests.test_bind_absent_replay_and_competing_reference
)
test_batch_put_get_and_conflict_rollback = (
    contract_tests.test_batch_put_get_and_conflict_rollback
)
test_every_schema_path_rejects_invalid_text = (
    contract_tests.test_every_schema_path_rejects_invalid_text
)
test_every_key_path_rejects_invalid_text = (
    contract_tests.test_every_key_path_rejects_invalid_text
)
test_every_content_hash_path_rejects_malformed_values = (
    contract_tests.test_every_content_hash_path_rejects_malformed_values
)
test_empty_key_is_valid = contract_tests.test_empty_key_is_valid
test_put_contention_has_one_correlated_winner = (
    contract_tests.test_put_contention_has_one_correlated_winner
)
test_bind_contention_has_one_correlated_winner = (
    contract_tests.test_bind_contention_has_one_correlated_winner
)
