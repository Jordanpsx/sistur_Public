# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Hardware mock fixtures for thermal printers and QR scanners.

These mocks replace real hardware integrations during testing so the
test suite runs without physical devices or OS-level drivers.

References:
    CLAUDE.md — "Python Tooling (to be ported)"
    legacy/mu-plugins/externo/ponto.py — QR scanner reference
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# QR Scanner mock
# ---------------------------------------------------------------------------

class MockQRScanner:
    """
    Simulates the QR code scanner (pyzbar + OpenCV webcam capture).

    Reference: legacy/mu-plugins/externo/ponto.py
    """

    def __init__(self, tokens_to_yield: list[str] | None = None):
        """
        Args:
            tokens_to_yield: Sequence of QR token strings the mock will
                             "scan" when next_scan() is called in order.
        """
        self._tokens = list(tokens_to_yield or [])
        self._index = 0
        self.scan_count = 0

    def next_scan(self) -> str | None:
        """Return the next pre-configured token, or None if exhausted."""
        if self._index >= len(self._tokens):
            return None
        token = self._tokens[self._index]
        self._index += 1
        self.scan_count += 1
        return token

    def reset(self) -> None:
        self._index = 0
        self.scan_count = 0


@pytest.fixture
def mock_qr_scanner():
    """Pytest fixture: returns a MockQRScanner with no pre-loaded tokens."""
    return MockQRScanner()


@pytest.fixture
def mock_qr_scanner_factory():
    """
    Pytest fixture: factory that creates MockQRScanner with custom tokens.

    Usage in test:
        def test_ponto(mock_qr_scanner_factory):
            scanner = mock_qr_scanner_factory(["token-abc", "token-def"])
            assert scanner.next_scan() == "token-abc"
    """
    def _factory(tokens: list[str]) -> MockQRScanner:
        return MockQRScanner(tokens_to_yield=tokens)
    return _factory


# ---------------------------------------------------------------------------
# Thermal Printer mock
# ---------------------------------------------------------------------------

class MockThermalPrinter:
    """
    Simulates the thermal printer integration (win32print).

    Reference: CLAUDE.md — "win32print — Thermal printer integration (Windows)"
    """

    def __init__(self):
        self.printed_jobs: list[bytes] = []
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def print_raw(self, data: bytes) -> None:
        if not self.is_open:
            raise RuntimeError("Impressora não está aberta.")
        self.printed_jobs.append(data)

    @property
    def job_count(self) -> int:
        return len(self.printed_jobs)

    def last_job(self) -> bytes | None:
        return self.printed_jobs[-1] if self.printed_jobs else None


@pytest.fixture
def mock_printer():
    """Pytest fixture: returns an open MockThermalPrinter."""
    printer = MockThermalPrinter()
    printer.open()
    return printer


@pytest.fixture
def mock_win32print():
    """
    Patch win32print at import time so tests run on non-Windows systems.

    Usage:
        def test_something(mock_win32print):
            mock_win32print.OpenPrinter.return_value = "handle-123"
            ...
    """
    with patch.dict("sys.modules", {"win32print": MagicMock()}) as mock_modules:
        yield mock_modules["win32print"]
