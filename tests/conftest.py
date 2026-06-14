"""Shared pytest fixtures: repo path + capture location."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CAPTURE = os.path.join(ROOT, "data", "capture_v4.jsonl")


@pytest.fixture
def capture_path():
    assert os.path.exists(CAPTURE), f"missing capture: {CAPTURE}"
    return CAPTURE
