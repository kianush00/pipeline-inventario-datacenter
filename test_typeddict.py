from typing import TypedDict, Any

class NetBoxPayload(TypedDict, total=False):
    name: str
    status: str

def build_payload() -> NetBoxPayload:
    payload: NetBoxPayload = {}
    target = "name"
    # This might fail
    payload[target] = "test"
    return payload
