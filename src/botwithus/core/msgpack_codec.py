"""Thin MessagePack wrapper for the RPC envelope.

The agent speaks a MessagePack subset (see PUBLIC_PROTOCOL.md Part B, section 5.2).
We keep this behind one module so the rest of the code never imports ``msgpack``
directly -- if the dependency ever changes, or a pure-Python fallback is needed, it
changes here only.

Encoding rules that matter for the wire:
- ``use_bin_type=True`` so Python ``bytes`` encode as msgpack ``bin`` (the agent
  reads ``bin`` for the few binary params it accepts).
- ``raw=False`` on decode so ``str`` comes back as ``str`` (UTF-8), not ``bytes``.
- ``strict_map_key=False`` -- the agent only ever sends string keys, but we do not
  want a non-string key (should one ever appear) to raise.
"""
from __future__ import annotations

from typing import Any

import msgpack


def pack(obj: Any) -> bytes:
    """Encode a Python object to a MessagePack payload (no length prefix)."""
    return msgpack.packb(obj, use_bin_type=True)


def unpack(data: bytes) -> Any:
    """Decode a MessagePack payload (no length prefix) to a Python object."""
    return msgpack.unpackb(data, raw=False, strict_map_key=False)
