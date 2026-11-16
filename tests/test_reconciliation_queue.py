"""Queue trust-boundary controls; actual SQL is rehearsed in temporary PG tables."""
from unittest.mock import Mock
import pytest
from recipe_wrangler.matching.reconciliation_queue import enqueue_reconciliation


@pytest.mark.parametrize('operation,recipes', [
    ('', ['r']), ('x'*129, ['r']), (None, ['r']),
    ('operation', []), ('operation', ['']), ('operation', [None]),
    ('operation', ['r', 1]), ('operation', [str(i) for i in range(251)]),
])
def test_invalid_queue_scope_is_rejected_before_any_database_call(operation,recipes):
    connection=Mock()
    with pytest.raises(ValueError):enqueue_reconciliation(connection,operation,recipes)
    connection.execute.assert_not_called()


@pytest.mark.parametrize('receipt', [
    {'recipe_ids':['different'], 'generations':{'different':1}},
    {'recipe_ids':['r'], 'generations':{}},
    {'recipe_ids':['r'], 'generations':{'r':0}},
    {'recipe_ids':['r'], 'generations':{'r':True}},
])
def test_reused_identity_or_incomplete_receipt_cannot_complete_the_handoff(receipt):
    connection=Mock()
    connection.execute.return_value.scalar.return_value=None
    connection.execute.return_value.mappings.return_value.one.return_value=receipt
    with pytest.raises(ValueError):enqueue_reconciliation(connection,'operation',['r'])
