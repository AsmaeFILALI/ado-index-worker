from copy import deepcopy

import pytest
from conftest import CONTRACT

from ado_markdown_ingestion.contract import Contract, validate_schema
from ado_markdown_ingestion.errors import IngestionError


def synthetic_schema() -> dict[str, object]:
    """Test-only REST shape; never evidence of a provisioned platform index."""
    contract = Contract.load(CONTRACT)
    return {
        "name": "synthetic-not-deployed",
        "fields": [
            *deepcopy(list(contract.fields.values())),
            {
                "name": "category",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
                "filterable": True,
            },
            {"name": "additional_platform_field", "type": "Edm.String"},
        ],
        "vectorSearch": {
            "profiles": [{"name": "default", "algorithm": "hnsw", "vectorizer": "query"}],
            "algorithms": [
                {"name": "hnsw", "kind": "hnsw", "hnswParameters": {"metric": "cosine"}}
            ],
            "vectorizers": [
                {
                    "name": "query",
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "modelName": "text-embedding-3-large",
                        "deploymentId": "text-embedding",
                        "resourceUri": "https://synthetic-not-deployed.openai.azure.com",
                    },
                }
            ],
        },
        "semantic": {
            "defaultConfiguration": "semantic-config",
            "configurations": [
                {
                    "name": "semantic-config",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "heading_path"},
                            {"fieldName": "category"},
                        ],
                    },
                }
            ],
        },
    }


def test_consumer_and_synthetic_extra_platform_fields() -> None:
    contract = Contract.load(CONTRACT)
    assert len(contract.fields) == 23
    validate_schema(synthetic_schema(), contract)


CONSTRAINTS = [
    (name, prop)
    for name, field in Contract.load(CONTRACT).fields.items()
    for prop in field
    if prop != "name"
]


@pytest.mark.parametrize(("name", "prop"), CONSTRAINTS)
def test_every_declared_constraint_is_compared(name: str, prop: str) -> None:
    schema = synthetic_schema()
    fields = schema["fields"]
    assert isinstance(fields, list)
    field = next(field for field in fields if field["name"] == name)
    current = field[prop]
    field[prop] = (
        not current
        if isinstance(current, bool)
        else (current + 1 if isinstance(current, int) else "incompatible")
    )
    with pytest.raises(IngestionError):
        validate_schema(schema, Contract.load(CONTRACT))


def test_defaults_and_unconstrained_properties() -> None:
    schema = synthetic_schema()
    fields = schema["fields"]
    assert isinstance(fields, list)
    for field in fields:
        field.pop("retrievable", None)
        if field.get("key") is False:
            field.pop("key")
    content = next(field for field in fields if field["name"] == "content")
    content["filterable"] = True  # Not constrained by the consumer.
    validate_schema(schema, Contract.load(CONTRACT))
    next(field for field in fields if field["name"] == "parent_id").pop("filterable")
    with pytest.raises(IngestionError, match="parent_id.filterable"):
        validate_schema(schema, Contract.load(CONTRACT))


def test_missing_duplicate_extra_key_and_embedding_mismatch() -> None:
    contract = Contract.load(CONTRACT)
    schema = synthetic_schema()
    fields = schema["fields"]
    assert isinstance(fields, list)
    removed = fields.pop(0)
    with pytest.raises(IngestionError, match="missing"):
        validate_schema(schema, contract)
    fields.append(removed)
    fields.append(deepcopy(removed))
    with pytest.raises(IngestionError, match="duplicate"):
        validate_schema(schema, contract)
    fields.pop()
    fields[-2]["key"] = True
    with pytest.raises(IngestionError, match="Only id"):
        validate_schema(schema, contract)
    with pytest.raises(IngestionError, match="deployment"):
        validate_schema(synthetic_schema(), contract, deployment="other")
    with pytest.raises(IngestionError, match="model"):
        validate_schema(synthetic_schema(), contract, model="text-embedding-3-small")
    with pytest.raises(IngestionError, match="resource"):
        validate_schema(synthetic_schema(), contract, embedding_resource="https://other.invalid")


def test_sdk_hidden_attributes_cannot_masquerade_as_rest_retrievability() -> None:
    schema = synthetic_schema()
    fields = schema["fields"]
    assert isinstance(fields, list)
    fields[0].pop("retrievable")
    fields[0]["hidden"] = True
    with pytest.raises(IngestionError, match="SDK"):
        validate_schema(schema, Contract.load(CONTRACT))


def test_query_vectorizer_and_semantic_fields_are_required() -> None:
    schema = synthetic_schema()
    vector = schema["vectorSearch"]
    assert isinstance(vector, dict)
    vector["vectorizers"][0]["azureOpenAIParameters"]["resourceUri"] = "http://insecure.invalid"
    with pytest.raises(IngestionError, match="HTTPS"):
        validate_schema(schema, Contract.load(CONTRACT))
    schema = synthetic_schema()
    fields = schema["fields"]
    assert isinstance(fields, list)
    next(field for field in fields if field["name"] == "category")["searchable"] = False
    with pytest.raises(IngestionError, match="Semantic"):
        validate_schema(schema, Contract.load(CONTRACT))
