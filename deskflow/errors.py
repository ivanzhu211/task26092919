"""Exceptions raised by the Deskflow batch-editing library."""

from __future__ import annotations


class DeskflowError(Exception):
    """Base class for all library errors."""


class TaskNotFoundError(DeskflowError):
    """Raised when a referenced task id does not exist."""


class BatchNotFoundError(DeskflowError):
    """Raised when a referenced batch id does not exist."""


class InvalidChangeError(DeskflowError):
    """Raised when a batch submission is malformed."""


class UndoWindowExpiredError(DeskflowError):
    """Raised when undo is attempted after the 300-second window."""


class AlreadyInitializedError(DeskflowError):
    """Raised when initialization is attempted twice."""
