"""Bounded service retries; exceptions never expose SDK response bodies to logs."""

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from openai import APIConnectionError, APIResponseValidationError, APIStatusError

from ado_markdown_ingestion.errors import IngestionError

T = TypeVar("T")


class RetryableResult(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("Transient per-document indexing failure")
        self.status_code = status_code


def no_guard() -> None:
    return None


@dataclass
class Retry:
    attempts: int = 5
    deadline: float = 120
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    retries: int = 0

    def call(
        self,
        action: Callable[[], T],
        *,
        guard: Callable[[], None] = no_guard,
        rate_limit: bool = False,
        label: str = "service",
    ) -> T:
        started = self.clock()
        for attempt in range(self.attempts):
            guard()
            try:
                return action()
            except (APIResponseValidationError, json.JSONDecodeError) as exc:
                raise IngestionError(f"{label} returned an invalid response") from exc
            except (
                HttpResponseError,
                ServiceRequestError,
                ServiceResponseError,
                APIConnectionError,
                APIStatusError,
                RetryableResult,
            ) as exc:
                status = getattr(exc, "status_code", None)
                transient = (
                    isinstance(exc, (ServiceRequestError, ServiceResponseError, APIConnectionError))
                    or status in {408, 409, 422, 500, 502, 503, 504}
                    or (rate_limit and status == 429)
                )
                code = str(getattr(exc, "code", "")).lower().replace("_", "").replace("-", "")
                if status == 429 and code in {
                    "insufficientquota",
                    "quotaexceeded",
                    "billinghardlimitreached",
                    "deploymentcapacityexceeded",
                }:
                    transient = False
                if not transient:
                    raise IngestionError(f"{label} failed permanently (status {status})") from exc
                response = getattr(exc, "response", None)
                headers = getattr(response, "headers", {}) or {}
                retry_after = headers.get("Retry-After") or headers.get("retry-after")
                delay = min(30.0, 2.0**attempt + self.jitter())
                if retry_after is not None:
                    try:
                        delay = max(delay, float(retry_after))
                    except (ValueError, TypeError):
                        try:
                            when = parsedate_to_datetime(str(retry_after))
                            delay = max(delay, (when - datetime.now(UTC)).total_seconds())
                        except (ValueError, TypeError, OverflowError) as parse_error:
                            raise IngestionError(
                                "Invalid service Retry-After header"
                            ) from parse_error
                if attempt + 1 == self.attempts or self.clock() - started + delay >= self.deadline:
                    raise IngestionError(f"{label} retry budget exhausted") from exc
                self.retries += 1
                logging.getLogger("ado_ingest").info(
                    '{"event":"service_retry","service":"%s","attempt":%d,"status":%s}',
                    label,
                    attempt + 1,
                    status,
                )
                guard()
                self.sleep(delay)
        raise IngestionError(f"{label} has an invalid retry configuration")
