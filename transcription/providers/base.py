from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TranscriptionResult:
    source_file: str
    transcript: str
    request_id: str | None
    language_code: str | None
    raw_response: dict[str, Any]
    full_response: dict[str, Any]


@dataclass(slots=True)
class BatchSubmission:
    job_id: str
    state: str | None
    raw_response: dict[str, Any]


class TranscriptionProvider(ABC):
    @abstractmethod
    def submit_batch(
        self,
        file_paths: list[Path],
        language_code: str | None = None,
        with_diarization: bool = True,
        num_speakers: int | None = None,
    ) -> BatchSubmission:
        """Submit a batch job and return job metadata."""
        raise NotImplementedError

    @abstractmethod
    def wait_for_batch(self, job_id: str) -> dict[str, Any]:
        """Wait for a batch job to complete and return final state data."""
        raise NotImplementedError

    @abstractmethod
    def get_batch_results(self, job_id: str) -> list[TranscriptionResult]:
        """Return normalized per-file results for a completed job."""
        raise NotImplementedError
