import logging
import os
from collections.abc import Callable, Mapping
from functools import partial
from types import TracebackType
from typing import Any

from azure.identity import AzureCliCredential, AzurePipelinesCredential, get_bearer_token_provider
from openai import AzureOpenAI
from openai.types import CreateEmbeddingResponse, Embedding

from ado_markdown_ingestion.contract import validate_vector
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.publishing import PublishSettings
from ado_markdown_ingestion.retry import Retry, no_guard
from ado_markdown_ingestion.tokenizer import Encoder


def make_credential(
    target: PublishSettings,
    environ: Mapping[str, str] | None = None,
) -> AzureCliCredential | AzurePipelinesCredential:
    if target.auth_mode == "cli":
        return AzureCliCredential(tenant_id=target.tenant_id, process_timeout=15)
    if target.auth_mode != "pipeline" or not target.client_id or not target.service_connection_id:
        raise IngestionError("Pipeline tenant/client/service-connection identity must be explicit")
    env = os.environ if environ is None else environ
    token = env.get("SYSTEM_ACCESSTOKEN")
    if not token:
        raise IngestionError(
            "Pipeline job token SYSTEM_ACCESSTOKEN is required in the task environment"
        )
    return AzurePipelinesCredential(
        tenant_id=target.tenant_id,
        client_id=target.client_id,
        service_connection_id=target.service_connection_id,
        system_access_token=token,
    )


def make_openai(
    target: PublishSettings,
    credential: AzureCliCredential | AzurePipelinesCredential,
) -> AzureOpenAI:
    if any(
        os.environ.get(name)
        for name in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN", "OPENAI_API_KEY")
    ):
        raise IngestionError(
            "Remove ambient OpenAI keys/static tokens; explicit Entra authentication is required"
        )
    return AzureOpenAI(
        azure_endpoint=target.embedding_endpoint,
        api_version=target.openai_api_version,
        azure_ad_token_provider=get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        ),
        max_retries=0,
        timeout=30,
    )


def embedding_retry(**overrides: Any) -> Retry:
    """Sustained 429 throttling (Retry-After of 30 s or more) must not discard finished work."""
    return Retry(attempts=8, deadline=900, **overrides)


class Embeddings:
    def __init__(
        self,
        client: AzureOpenAI,
        encoder: Encoder,
        deployment: str,
        *,
        retry: Retry | None = None,
        guard: Callable[[], None] = no_guard,
    ) -> None:
        logging.getLogger("openai").setLevel(logging.WARNING)
        self.client = client
        self.encoder = encoder
        self.deployment = deployment
        self.retry = retry or embedding_retry()
        self.guard = guard
        self.calls = 0
        self.input_tokens = 0
        self.model_revision: str | None = None
        self._observed = False

    def embed(self, inputs: list[str]) -> list[list[float]]:
        batches: list[list[str]] = []
        batch: list[str] = []
        tokens = 0
        for text in inputs:
            count = self.encoder.count(text)
            if not text.strip() or count > 8000:
                raise IngestionError("Embedding input must be nonempty and at most 8000 tokens")
            if batch and (len(batch) == 16 or tokens + count > 16000):
                batches.append(batch)
                batch, tokens = [], 0
            batch.append(text)
            tokens += count
        if batch:
            batches.append(batch)
        result: list[list[float]] = []
        for batch in batches:
            response = self.retry.call(
                partial(self._request, batch),
                guard=self.guard,
                rate_limit=True,
                label="embeddings",
            )
            if response.model != "text-embedding-3-large":
                raise IngestionError("Embedding response model does not match the configured model")
            if not isinstance(response.data, list) or any(
                not isinstance(item, Embedding) or type(item.index) is not int
                for item in response.data
            ):
                raise IngestionError("Embedding response data/index types are invalid")
            revision = (response.model_extra or {}).get("model_version")
            if revision is not None and not isinstance(revision, str):
                raise IngestionError("Invalid reported model revision")
            if self._observed and revision != self.model_revision:
                raise IngestionError("Embedding model revision changed within the run")
            self._observed = True
            self.model_revision = revision
            indexed = {item.index: item.embedding for item in response.data}
            if len(response.data) != len(batch) or set(indexed) != set(range(len(batch))):
                raise IngestionError("Embedding response count/index mismatch")
            result.extend(validate_vector(indexed[index]) for index in range(len(batch)))
            self.input_tokens += sum(self.encoder.count(text) for text in batch)
        return result

    def _request(self, values: list[str]) -> CreateEmbeddingResponse:
        self.calls += 1
        return self.client.embeddings.create(
            input=values, model=self.deployment, dimensions=3072, encoding_format="float"
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Embeddings":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
