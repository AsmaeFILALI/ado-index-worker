"""Original in-memory Azure protocol fixtures. No SDK operation reaches a network."""

import json
import re
import struct
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlsplit

from azure.core.pipeline.transport import HttpRequest
from azure_fakes import Response, body
from test_contract import synthetic_schema


@dataclass
class Clock:
    value: float = 0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class Azure:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.etags: dict[str, int] = {}
        self.leases: dict[str, str] = {}
        self.docs: dict[str, dict[str, dict[str, object]]] = {}
        self.events: list[str] = []
        self.fail_journal = False
        self.fail_checkpoint = False
        self.conflict_next_control = False
        self.public_container = False
        self.permanent_key: str | None = None
        self.transient_once: set[str] = set()
        self.fail_delete = False
        self.visibility_lag = 0
        self._old_view: dict[str, dict[str, object]] | None = None

    def __call__(self, request: HttpRequest) -> Response:
        return self._blob(request) if ".blob." in request.url else self._search(request)

    def _error(self, request: HttpRequest, status: int, code: str) -> Response:
        return Response(
            request,
            status,
            {"error": {"code": code, "message": "synthetic failure"}},
            headers={"x-ms-error-code": code},
        )

    def _blob(self, request: HttpRequest) -> Response:
        url = urlsplit(request.url)
        path, query = unquote(url.path).lstrip("/"), parse_qs(url.query)
        if query.get("restype") == ["container"]:
            return Response(
                request,
                raw=b"",
                headers={
                    "ETag": '"container"',
                    "Last-Modified": "Mon, 26 Oct 2026 00:00:00 GMT",
                    **({"x-ms-blob-public-access": "blob"} if self.public_container else {}),
                },
            )
        if query.get("comp") == ["lease"]:
            action = request.headers.get("x-ms-lease-action")
            current = self.leases.get(path)
            if action == "acquire":
                if current is not None:
                    return self._error(request, 409, "LeaseAlreadyPresent")
                current = "00000000-0000-0000-0000-000000000123"
                self.leases[path] = current
                self.events.append("lease-acquire")
                return Response(request, 201, raw=b"", headers={"x-ms-lease-id": current})
            if current is None or request.headers.get("x-ms-lease-id") != current:
                return self._error(request, 412, "LeaseIdMismatchWithBlobOperation")
            if action == "release":
                self.leases.pop(path)
                self.events.append("lease-release")
            else:
                self.events.append("lease-renew")
            return Response(request, raw=b"", headers={"x-ms-lease-id": current})
        if request.method == "PUT":
            exists = path in self.blobs
            if request.headers.get("If-None-Match") == "*" and exists:
                return self._error(request, 409, "BlobAlreadyExists")
            if path in self.leases and request.headers.get("x-ms-lease-id") != self.leases[path]:
                return self._error(request, 412, "LeaseIdMissing")
            match = request.headers.get("If-Match")
            if match and match != f'"{self.etags.get(path, 0)}"':
                return self._error(request, 412, "ConditionNotMet")
            raw = request.body
            if isinstance(raw, str):
                raw = raw.encode()
            assert isinstance(raw, bytes)
            if path.endswith("publisher.json") and match:
                if self.conflict_next_control:
                    self.conflict_next_control = False
                    return self._error(request, 412, "ConditionNotMet")
                state = json.loads(raw)
                pending = any("pending" in value for value in state["scopes"].values())
                checkpoint = not pending and any(
                    "checkpoint" in value for value in state["scopes"].values()
                )
                if (pending and self.fail_journal) or (checkpoint and self.fail_checkpoint):
                    return self._error(request, 503, "ServerBusy")
                self.events.append(
                    "journal" if pending else "checkpoint" if checkpoint else "observation"
                )
            self.blobs[path] = raw
            self.etags[path] = self.etags.get(path, 0) + 1
            return Response(
                request,
                201,
                raw=b"",
                headers={
                    "ETag": f'"{self.etags[path]}"',
                    "Last-Modified": "Mon, 26 Oct 2026 00:00:00 GMT",
                    "x-ms-content-crc64": "",
                },
            )
        if path not in self.blobs:
            return self._error(request, 404, "BlobNotFound")
        raw = self.blobs[path]
        headers = {
            "ETag": f'"{self.etags[path]}"',
            "Last-Modified": "Mon, 26 Oct 2026 00:00:00 GMT",
            "x-ms-blob-type": "BlockBlob",
            "Content-Length": str(len(raw)),
            "Content-Range": f"bytes 0-{len(raw) - 1}/{len(raw)}",
        }
        return Response(
            request,
            206 if request.method == "GET" else 200,
            raw=raw if request.method == "GET" else b"",
            headers=headers,
        )

    def _search(self, request: HttpRequest) -> Response:
        path = unquote(urlsplit(request.url).path)
        match = re.search(r"/indexes\('([^']+)'\)", path)
        index = match.group(1) if match else path.split("/indexes/")[-1].split("/")[0]
        docs = self.docs.setdefault(index, {})
        if request.method == "GET" and "/docs" not in path:
            schema = synthetic_schema()
            schema["name"] = index
            vectors = schema["vectorSearch"]
            assert isinstance(vectors, dict)
            vectors["vectorizers"][0]["azureOpenAIParameters"]["resourceUri"] = (
                "https://synthetic.openai.azure.com"
            )
            return Response(request, value=schema)
        if request.method == "GET":
            key_match = re.search(r"/docs\('([^']+)'\)", path)
            key = key_match.group(1) if key_match else path.rsplit("/", 1)[-1]
            if key not in docs:
                return self._error(request, 404, "NotFound")
            return Response(request, value=deepcopy(docs[key]))
        data = body(request)
        if "search" in data:
            query = str(data["filter"])
            repo = re.search(r"repo_id eq '((?:[^']|'')*)'", query)
            ref = re.search(r"source_ref eq '((?:[^']|'')*)'", query)
            assert repo is not None and ref is not None
            source = self._old_view if self.visibility_lag and self._old_view is not None else docs
            if source is self._old_view:
                self.visibility_lag -= 1
                if not self.visibility_lag:
                    self._old_view = None
            rows = [
                deepcopy(doc)
                for doc in source.values()
                if doc["repo_id"] == repo.group(1).replace("''", "'")
                and doc["source_ref"] == ref.group(1).replace("''", "'")
                and doc["source"] == "azure_devops"
            ]
            selected = str(data.get("select", "")).split(",")
            count = len(rows)
            skip, top = data.get("skip", 0), data.get("top", 1000)
            assert isinstance(skip, int) and isinstance(top, int)
            rows = [
                {key: value for key, value in doc.items() if not selected[0] or key in selected}
                for doc in rows[skip : skip + top]
            ]
            return Response(request, value={"@odata.count": count, "value": rows})
        actions = data["value"]
        assert isinstance(actions, list)
        if self.visibility_lag and self._old_view is None:
            self._old_view = deepcopy(docs)
        results: list[dict[str, object]] = []
        for action in actions:
            key = action["id"]
            delete = action["@search.action"] == "delete"
            if key == self.permanent_key:
                status = 400
            elif key in self.transient_once:
                self.transient_once.remove(key)
                status = 503
            elif delete and self.fail_delete:
                status = 400
            else:
                status = 200 if delete else 201
                if delete:
                    docs.pop(key, None)
                    self.events.append("delete")
                else:
                    docs[key] = {k: v for k, v in action.items() if k != "@search.action"}
                    vector = docs[key].get("contentVector")
                    if isinstance(vector, list):
                        docs[key]["contentVector"] = list(
                            struct.unpack("<3072f", struct.pack("<3072f", *vector))
                        )
                    stamp = docs[key].get("ingested_at")
                    if isinstance(stamp, str):
                        docs[key]["ingested_at"] = (
                            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                            .astimezone(UTC)
                            .isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z")
                        )
                    self.events.append("upload")
            results.append({"key": key, "status": status < 300, "statusCode": status})
        return Response(request, value={"value": results})
