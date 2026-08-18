from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from yuxi.knowledge.parser.factory import DocumentProcessorFactory


class _FakeProcessor:
    init_count = 0
    active_calls = 0
    max_active_calls = 0
    state_lock = threading.Lock()

    def __init__(self):
        type(self).init_count += 1

    def process_file(self, file_path: str, params: dict | None = None) -> str:
        with type(self).state_lock:
            type(self).active_calls += 1
            type(self).max_active_calls = max(type(self).max_active_calls, type(self).active_calls)
        try:
            time.sleep(0.01)
            return file_path
        finally:
            with type(self).state_lock:
                type(self).active_calls -= 1


def test_factory_constructs_and_uses_cached_processor_serially(monkeypatch):
    DocumentProcessorFactory.clear_cache()
    _FakeProcessor.init_count = 0
    _FakeProcessor.active_calls = 0
    _FakeProcessor.max_active_calls = 0
    monkeypatch.setattr(DocumentProcessorFactory, "PROCESSOR_TYPES", {"fake": ("ignored", "Fake")})
    monkeypatch.setattr(
        DocumentProcessorFactory,
        "_load_processor_class",
        classmethod(lambda cls, processor_type: _FakeProcessor),
    )

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            processors = list(executor.map(lambda _: DocumentProcessorFactory.get_processor("fake"), range(8)))
            results = list(
                executor.map(
                    lambda index: DocumentProcessorFactory.process_file("fake", f"file-{index}"),
                    range(8),
                )
            )

        assert _FakeProcessor.init_count == 1
        assert len({id(processor) for processor in processors}) == 1
        assert results == [f"file-{index}" for index in range(8)]
        assert _FakeProcessor.max_active_calls == 1
    finally:
        DocumentProcessorFactory.clear_cache()
