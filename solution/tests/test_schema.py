"""Tests for ledger.schema, using the real schema files."""

import pytest

from ledger.config import SCHEMAS_DIR
from ledger.schema import load_schemas


@pytest.fixture(scope="module")
def schemas():
    return load_schemas(SCHEMAS_DIR)


def test_both_workflows_load(schemas):
    assert schemas["motor_renewal"].reference_field.name == "policy_number"
    assert schemas["policy_feedback"].reference_field.name == "order_id"


def test_pii_fields_follow_the_schema_flags(schemas):
    assert [f.name for f in schemas["motor_renewal"].pii_fields] == ["customer_name", "phone"]


def test_alias_headers_from_batch_003(schemas):
    headers = ["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date", "Premium", "agent_notes"]
    assert schemas["motor_renewal"].map_headers(headers) == {
        0: "preferred_language",
        1: "customer_name",
        2: "policy_number",
        3: "phone",
        4: "policy_expiry",
        5: "premium_amount",
    }


def test_policy_feedback_alias_headers(schemas):
    headers = ["Order ID", "Name", "Contact", "Product", "Order Date", "Language", "agent_notes"]
    mapping = schemas["policy_feedback"].map_headers(headers)
    assert mapping[0] == "order_id"
    assert mapping[2] == "phone"
    assert 6 not in mapping  # agent_notes is ignored


def test_headers_are_normalised_before_matching(schemas):
    assert schemas["motor_renewal"].map_headers(["  PHONE_NUMBER ", "customer__name"]) == {
        0: "phone",
        1: "customer_name",
    }


def test_first_column_wins_when_two_map_to_the_same_field(schemas):
    assert schemas["motor_renewal"].map_headers(["mobile", "phone"]) == {0: "phone"}
