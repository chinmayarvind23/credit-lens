from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class QueryRequest(_message.Message):
    __slots__ = ("borrower_id", "question", "effective_at")
    BORROWER_ID_FIELD_NUMBER: _ClassVar[int]
    QUESTION_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_AT_FIELD_NUMBER: _ClassVar[int]
    borrower_id: str
    question: str
    effective_at: str
    def __init__(self, borrower_id: _Optional[str] = ..., question: _Optional[str] = ..., effective_at: _Optional[str] = ...) -> None: ...

class PacketResponse(_message.Message):
    __slots__ = ("schema_version", "packet_json")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    PACKET_JSON_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    packet_json: bytes
    def __init__(self, schema_version: _Optional[int] = ..., packet_json: _Optional[bytes] = ...) -> None: ...
