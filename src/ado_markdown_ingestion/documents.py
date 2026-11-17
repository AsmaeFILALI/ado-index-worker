from dataclasses import asdict, dataclass

from ado_markdown_ingestion import __version__
from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.contract import Contract, validate_document
from ado_markdown_ingestion.markdown import Chunk
from ado_markdown_ingestion.source import SourceFile, citation_url
from ado_markdown_ingestion.util import digest, fingerprint


@dataclass(frozen=True)
class DraftDocument:
    id: str
    parent_id: str
    title: str
    content: str
    url: str
    filepath: str
    source: str
    repo_id: str
    source_ref: str
    source_path: str
    heading_path: str
    section_id: str
    chunk_id: int
    part: int
    part_count: int
    start_line: int
    end_line: int
    source_version_id: str
    content_checksum_sha256: str
    pipeline_version: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_documents(
    file: SourceFile, chunks: list[Chunk], settings: Settings, contract: Contract
) -> list[DraftDocument]:
    parent = fingerprint([settings.repo_id, settings.source_ref, file.path])
    url = citation_url(settings.repo_url, file.path, settings.commit)
    result: list[DraftDocument] = []
    for chunk in chunks:
        section = fingerprint([parent, list(chunk.section_identity)])
        document_id = fingerprint([section, chunk.part])
        document = DraftDocument(
            id=document_id,
            parent_id=parent,
            title=chunk.title,
            content=chunk.content,
            url=url,
            filepath=url,
            source="azure_devops",
            repo_id=settings.repo_id,
            source_ref=settings.source_ref,
            source_path=file.path,
            heading_path=chunk.heading_path,
            section_id=section,
            chunk_id=int(document_id[:7], 16),
            part=chunk.part,
            part_count=chunk.part_count,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            source_version_id=settings.commit,
            content_checksum_sha256=digest(chunk.content),
            pipeline_version=__version__,
        )
        validate_document(
            document.to_dict(), contract, draft=True, max_content_chars=settings.max_content_chars
        )
        result.append(document)
    return result
