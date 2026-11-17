import json
import time
from collections.abc import Callable, Iterator
from types import TracebackType

from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport
from azure.core.utils import CaseInsensitiveDict


class Credential:
    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        return AccessToken("synthetic-offline-token", int(time.time()) + 3600)

    def close(self) -> None:
        pass


class Stream(Iterator[bytes]):
    def __init__(self, data: bytes, response: "Response") -> None:
        self.chunks = iter([data])
        self.properties: object = None
        self.content_length = len(data)
        self.response = response

    def __iter__(self) -> "Stream":
        return self

    def __next__(self) -> bytes:
        return next(self.chunks)


class Response(HttpResponse):
    def __init__(
        self,
        request: HttpRequest,
        status: int = 200,
        value: object = None,
        *,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.reason = "Synthetic"
        self._data = json.dumps(value).encode() if raw is None else raw
        self.headers = CaseInsensitiveDict(
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(self._data)),
                **(headers or {}),
            }
        )

    def body(self) -> bytes:
        return self._data

    def stream_download(self, pipeline: object, **kwargs: object) -> Iterator[bytes]:
        return Stream(self._data, self)


class Transport(HttpTransport[HttpRequest, HttpResponse]):
    def __init__(self, handler: Callable[[HttpRequest], Response]) -> None:
        self.handler = handler
        self.requests: list[HttpRequest] = []

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, request: HttpRequest, **kwargs: object) -> HttpResponse:
        self.requests.append(request)
        return self.handler(request)

    def __enter__(self) -> "Transport":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None = None,
        value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.close()


def body(request: HttpRequest) -> dict[str, object]:
    raw = request.body
    assert isinstance(raw, (str, bytes))
    result: object = json.loads(raw)
    assert isinstance(result, dict)
    return dict(result)
