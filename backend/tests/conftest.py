import pytest

from app.config import get_settings
from app.spec_parser import parse_safe_13_organization_spec, parse_safe_chapter6_slice


@pytest.fixture(scope="session")
def organization_spec():
    return parse_safe_13_organization_spec(get_settings().safe_13_spec_path)


@pytest.fixture(scope="session")
def chapter6_specs():
    return parse_safe_chapter6_slice(get_settings().safe_13_spec_path)
