from __future__ import annotations

import pytest

from memory.store import MemoryStore


def test_memory_store_instances_are_tenant_bound() -> None:
    MemoryStore.reset()
    tenant_a = MemoryStore.get("tenant-a")
    tenant_b = MemoryStore.get("tenant-b")
    assert tenant_a is not tenant_b
    assert tenant_a.tenant_id == "tenant-a"
    assert tenant_b.tenant_id == "tenant-b"


@pytest.mark.asyncio
async def test_memory_store_rejects_scope_switch() -> None:
    MemoryStore.reset()
    store = MemoryStore.get("tenant-a")
    with pytest.raises(ValueError, match="cannot be changed"):
        await store.search("incident", tenant_id="tenant-b")
