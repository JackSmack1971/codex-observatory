from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from google.protobuf import (  # type: ignore[import-untyped]
    any_pb2 as _any_pb2,  # noqa: F401 - registers google/protobuf/any.proto
)
from google.protobuf import (  # type: ignore[import-untyped]
    descriptor_pb2,
    descriptor_pool,
    message_factory,
)
from google.protobuf.json_format import Parse  # type: ignore[import-untyped]
from google.protobuf.message import DecodeError  # type: ignore[import-untyped]

from .models import RawEnvelope
from .normalization import message_type
from .sqlite import utc_now

MAX_BODY = 64 * 1024 * 1024


class OtlpError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request", status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Decoded:
    envelope: RawEnvelope
    message: object


def decode(signal: str, body: bytes, content_type: str, content_encoding: str | None, source_version: str | None = None) -> Decoded:
    media = content_type.split(";", 1)[0].strip().lower()
    if media not in {"application/x-protobuf", "application/json"}:
        raise OtlpError(f"unsupported Content-Type: {media}", "unsupported_media_type", 415)
    if content_encoding and content_encoding.lower() != "gzip":
        raise OtlpError(f"unsupported Content-Encoding: {content_encoding}", "unsupported_encoding", 415)
    try:
        decoded = gzip.decompress(body) if content_encoding else body
    except (OSError, EOFError) as exc:
        raise OtlpError(f"gzip decompression failed: {exc}", "malformed_compression") from exc
    if len(decoded) > MAX_BODY:
        raise OtlpError("decompressed request exceeds 64 MiB", "request_too_large", 413)
    digest = hashlib.sha256(decoded).hexdigest()
    message: Any = message_type(signal)()
    try:
        if media == "application/json":
            Parse(decoded.decode("utf-8"), message, ignore_unknown_fields=True)
        else:
            message.ParseFromString(decoded)
    except (UnicodeDecodeError, DecodeError, ValueError) as exc:
        raise OtlpError(f"invalid OTLP {signal} protobuf payload: {exc}", "invalid_protobuf") from exc
    envelope = RawEnvelope(str(uuid.uuid4()), utc_now(), "native_otel", "local-default", signal, source_version, media,
                           content_encoding.lower() if content_encoding else None, "json" if media == "application/json" else "protobuf",
                           f"sha256:{digest}", len(decoded), None, "accepted", retention_class="metadata", payload=decoded)
    return Decoded(envelope, message)


def response(signal: str, content_type: str) -> tuple[bytes, str]:
    message_type_name = {"logs": "ExportLogsServiceResponse", "metrics": "ExportMetricsServiceResponse", "traces": "ExportTraceServiceResponse"}[signal]
    module_name = {"logs": "logs", "metrics": "metrics", "traces": "trace"}[signal]
    signal_package = {"logs": "logs", "metrics": "metrics", "traces": "trace"}[signal]
    module = __import__(f"opentelemetry.proto.collector.{signal_package}.v1.{module_name}_service_pb2", fromlist=[message_type_name])
    message = getattr(module, message_type_name)()
    if content_type == "application/json":
        from google.protobuf.json_format import (
            MessageToJson,  # type: ignore[import-untyped]
        )
        return MessageToJson(message, preserving_proto_field_name=False).encode(), content_type
    return message.SerializeToString(), content_type


def error_response(message: str, content_type: str) -> tuple[bytes, str]:
    file_descriptor = descriptor_pb2.FileDescriptorProto(name="google/rpc/status.proto", package="google.rpc", syntax="proto3")
    file_descriptor.dependency.append("google/protobuf/any.proto")
    status = file_descriptor.message_type.add(name="Status")
    status.field.add(name="code", number=1, label=1, type=5)
    status.field.add(name="message", number=2, label=1, type=9)
    status.field.add(name="details", number=3, label=3, type=11, type_name=".google.protobuf.Any")
    try:
        descriptor = descriptor_pool.Default().Add(file_descriptor)
    except TypeError:
        descriptor = descriptor_pool.Default().FindFileByName("google/rpc/status.proto")
    status_class = message_factory.GetMessageClass(descriptor.message_types_by_name["Status"])
    status_message = status_class(message=message)
    if content_type == "application/json":
        return json.dumps({"message": message}).encode(), content_type
    return status_message.SerializeToString(), content_type
