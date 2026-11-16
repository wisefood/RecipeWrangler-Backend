"""Idempotent handoff from a committed graph journal to the projection outbox."""
from __future__ import annotations

import json
from sqlalchemy import text


def enqueue_reconciliation(connection, operation_id: str, recipe_ids: list[str]) -> dict[str, int]:
    """Queue the complete sibling set once, inside the caller's PG transaction.

    The graph journal must commit first and retain its queue-pending state
    until this transaction commits. A crash before the graph acknowledgement
    can then retry this call without incrementing generations again.
    """
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise ValueError('A bounded reconciliation operation ID is required')
    if not recipe_ids or any(not isinstance(value, str) or not value.strip() for value in recipe_ids):
        raise ValueError('Reconciliation requires 1–250 nonempty recipe IDs')
    ids = sorted(set(recipe_ids))
    if len(ids) > 250:
        raise ValueError('Reconciliation requires 1–250 nonempty recipe IDs')
    inserted = connection.execute(text('''INSERT INTO matching_reconciliation_receipts(operation_id,recipe_ids)
        VALUES(:operation,CAST(:recipes AS jsonb)) ON CONFLICT(operation_id) DO NOTHING
        RETURNING operation_id'''), {'operation':operation_id, 'recipes':json.dumps(ids)}).scalar()
    if not inserted:
        receipt = connection.execute(text('''SELECT recipe_ids,generations
            FROM matching_reconciliation_receipts WHERE operation_id=:operation'''),
            {'operation':operation_id}).mappings().one()
        if receipt['recipe_ids'] != ids:
            raise ValueError('Reconciliation operation was reused with a different sibling set')
        generations = receipt['generations']
        if set(generations) != set(ids) or any(type(value) is not int or value < 1 for value in generations.values()):
            raise ValueError('Incomplete reconciliation queue receipt')
        return dict(generations)
    generations = {}
    for start in range(0, len(ids), 100):
        rows = connection.execute(text('''INSERT INTO matching_projection_jobs(recipe_id)
            SELECT unnest(CAST(:recipes AS text[]))
            ON CONFLICT(recipe_id) DO UPDATE SET generation=matching_projection_jobs.generation+1,
                requested_at=now(),last_error=NULL
            RETURNING recipe_id,generation'''), {'recipes':ids[start:start+100]}).mappings()
        generations.update({row['recipe_id']:row['generation'] for row in rows})
    if set(generations) != set(ids):
        raise ValueError('Reconciliation failed to queue the complete sibling set')
    connection.execute(text('''UPDATE matching_reconciliation_receipts
        SET generations=CAST(:generations AS jsonb) WHERE operation_id=:operation'''),
        {'operation':operation_id,'generations':json.dumps(generations,sort_keys=True)})
    return generations
