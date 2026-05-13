"""Tests for cobalt.core.exceptions."""

import pytest

from cobalt.core.exceptions import (
    FileOwnershipViolation,
    LedgerWriteError,
    LLMCallFailure,
    SchemaValidationError,
    ConnectorFailure,
    IntakeError,
    WorkspaceCreationError,
    HaltError,
    RecoverableError,
)

_ALL_EXCEPTIONS = [
    FileOwnershipViolation,
    LedgerWriteError,
    LLMCallFailure,
    SchemaValidationError,
    ConnectorFailure,
    IntakeError,
    WorkspaceCreationError,
    HaltError,
    RecoverableError,
]


@pytest.mark.parametrize("exc_class", _ALL_EXCEPTIONS)
def test_exception_can_be_raised_and_caught(exc_class):
    with pytest.raises(exc_class):
        raise exc_class("test message")


@pytest.mark.parametrize("exc_class", _ALL_EXCEPTIONS)
def test_exception_has_non_empty_docstring(exc_class):
    assert exc_class.__doc__, f"{exc_class.__name__} is missing a docstring"
    assert exc_class.__doc__.strip(), f"{exc_class.__name__} has an empty docstring"


@pytest.mark.parametrize("exc_class", _ALL_EXCEPTIONS)
def test_exception_inherits_from_exception(exc_class):
    assert issubclass(exc_class, Exception)
