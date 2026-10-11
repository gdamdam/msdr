import pytest


@pytest.fixture(autouse=True)
def _ml_on(ml_available):
    """Every test here exercises the dormant RF ML runtime."""
