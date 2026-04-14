"""memory_keeper.adapters.base — Abstract adapter interfaces.

Defines the tool-neutral contracts for:
    IKanbanAdapter       — fetch completed tasks from any task-tracking tool
    IMemoryAdapter       — check if tasks are covered in a memory document
    ISessionLogAdapter   — read and distill AI session logs
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Task:
    """A completed task from any kanban/issue-tracking source."""
    id: str              # "#87", "issue/123", or UUID
    title: str
    done_at: datetime | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Gap:
    """A task that has no corresponding entry in the memory document."""
    task: Task
    reason: str          # human-readable explanation of why it's a gap


@dataclass
class Session:
    """A parsed AI session log."""
    session_id: str
    project: str
    path: Path
    started_at: datetime | None
    turns: int
    raw_text: str        # filtered/redacted text for LLM processing


class IKanbanAdapter(ABC):
    """Fetch completed tasks from a task-tracking tool."""

    @abstractmethod
    def test(self, project_path: Path) -> bool:
        """Return True if this adapter applies to the given project directory."""
        ...

    @abstractmethod
    def fetch_done_tasks(self, project_path: Path, since: datetime) -> list[Task]:
        """Return completed tasks in the project since the given datetime."""
        ...


class IMemoryAdapter(ABC):
    """Check coverage of tasks in a memory document."""

    @abstractmethod
    def test(self, project_path: Path) -> bool:
        """Return True if a memory document exists in the given directory."""
        ...

    @abstractmethod
    def check_coverage(self, project_path: Path, tasks: list[Task]) -> list[Gap]:
        """Return tasks that are NOT mentioned in the memory document."""
        ...


class ISessionLogAdapter(ABC):
    """Read and distill AI session logs."""

    @abstractmethod
    def test(self, project_path: Path) -> bool:
        """Return True if session logs exist in the given directory."""
        ...

    @abstractmethod
    def fetch_sessions(self, project_path: Path, since: datetime) -> list[Session]:
        """Return sessions for the project created/modified since the given datetime."""
        ...

    @abstractmethod
    def distill(self, session: Session) -> str:
        """Distill a session into a Markdown summary."""
        ...
