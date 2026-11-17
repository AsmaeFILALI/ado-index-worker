"""Production SDK composition, imported only after explicit publishing admission."""

import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import ContainerClient

from ado_markdown_ingestion.embeddings import Embeddings, make_credential, make_openai
from ado_markdown_ingestion.publishing import Prepared
from ado_markdown_ingestion.search import Search
from ado_markdown_ingestion.state import BlobState
from ado_markdown_ingestion.tokenizer import Encoder


@contextmanager
def clients(prepared: Prepared, encoder: Encoder) -> Iterator[tuple[Search, BlobState, Embeddings]]:
    source, target = prepared.source, prepared.target
    for name in ("openai", "httpx", "azure"):
        logging.getLogger(name).setLevel(logging.WARNING)
    with ExitStack() as stack:
        credential = make_credential(target)
        stack.callback(credential.close)
        search_client = SearchClient(
            target.search_endpoint,
            target.index_name,
            credential,
            retry_total=0,
            connection_timeout=5,
            read_timeout=30,
            logging_enable=False,
        )
        stack.callback(search_client.close)
        index_client = SearchIndexClient(
            target.search_endpoint,
            credential,
            api_version=target.search_schema_api_version,
            retry_total=0,
            connection_timeout=5,
            read_timeout=30,
            logging_enable=False,
        )
        stack.callback(index_client.close)
        container = ContainerClient(
            target.state_account_url,
            target.state_container,
            credential=credential,
            retry_total=0,
            connection_timeout=5,
            read_timeout=30,
            logging_enable=False,
        )
        stack.callback(container.close)
        model = Embeddings(make_openai(target, credential), encoder, source.deployment)
        stack.callback(model.close)
        state = BlobState(container, target.physical_id(), prepared.compatibility)
        search = Search(
            search_client,
            index_client,
            target.index_name,
            source.repo_id,
            source.source_ref,
            inventory_limit=target.inventory_limit,
            service_endpoint=target.search_endpoint,
        )
        yield search, state, model
