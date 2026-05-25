"""Shared pytest fixtures for rvbbit E2E tests.

Tests run inside the bench container; the rvbbit DB is reachable as
pg-rvbbit (heap baseline as pg-heap). DSNs are pulled from the same
env vars the bench harness uses.
"""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)
HEAP_DSN = os.environ.get(
    "HEAP_DSN", "postgresql://postgres:rvbbit@pg-heap:5432/bench"
)


@pytest.fixture
def rvbbit():
    """Autocommit connection to the rvbbit instance, fresh per test."""
    with psycopg.connect(RVBBIT_DSN, autocommit=True) as c:
        yield c


@pytest.fixture
def heap():
    """Autocommit connection to the heap baseline instance."""
    with psycopg.connect(HEAP_DSN, autocommit=True) as c:
        yield c


@pytest.fixture
def temp_table(rvbbit):
    """Yields a unique table name; drops it (CASCADE) afterwards.

    Use for any test that needs a throw-away rvbbit table — DROP CASCADE
    cleans the catalog rows in rvbbit.tables / row_groups / shreds via
    the DDL trigger.
    """
    name = f"test_tbl_{uuid.uuid4().hex[:8]}"
    yield name
    try:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
    except psycopg.Error:
        pass
