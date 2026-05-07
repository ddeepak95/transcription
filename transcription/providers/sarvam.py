from __future__ import annotations

import logging
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError

from transcription.providers.base import BatchSubmission, TranscriptionProvider, TranscriptionResult

logger = logging.getLogger(__name__)


class SarvamTranscriptionProvider(TranscriptionProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "saaras:v3",
        mode: str = "translate",
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        job_poll_interval_seconds: float = 2.0,
    ) -> None:
        self._client = SarvamAI(api_subscription_key=api_key)
        self._model = model
        self._mode = mode
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._job_poll_interval_seconds = job_poll_interval_seconds
        self._job_handles: dict[str, Any] = {}

    def submit_batch(
        self,
        file_paths: list[Path],
        language_code: str | None = None,
        with_diarization: bool = True,
        num_speakers: int | None = None,
    ) -> BatchSubmission:
        if not file_paths:
            raise ValueError("No files provided for batch submission.")

        job = self._with_retries(
            lambda: self._client.speech_to_text_job.create_job(
                model=self._model,
                mode=self._mode,
                language_code=language_code,
                with_diarization=with_diarization,
                num_speakers=num_speakers,
            ),
            operation="create_job",
        )

        logger.info("Created Sarvam batch job: %s", getattr(job, "job_id", None))
        self._with_retries(
            lambda: job.upload_files(file_paths=[str(path) for path in file_paths]),
            operation="upload_files",
        )
        logger.info("Uploaded %s file(s) to batch job.", len(file_paths))
        self._with_retries(job.start, operation="start_job")
        logger.info("Started batch job: %s", getattr(job, "job_id", None))
        if getattr(job, "job_id", None):
            self._job_handles[job.job_id] = job

        return BatchSubmission(
            job_id=getattr(job, "job_id", ""),
            state=getattr(job, "job_state", None),
            raw_response=self._to_raw_dict(job),
        )

    def wait_for_batch(self, job_id: str) -> dict[str, Any]:
        job = self._get_job_handle(job_id)
        logger.info("Waiting for batch job completion: %s", job_id)
        def _wait() -> Any:
            try:
                return job.wait_until_complete(poll_interval_seconds=self._job_poll_interval_seconds)
            except TypeError as err:
                # Backward compatibility for older SDK versions that do not support poll interval arg.
                if "unexpected keyword argument 'poll_interval_seconds'" not in str(err):
                    raise
                logger.debug("SDK wait_until_complete has no poll_interval_seconds parameter; using default polling.")
                return job.wait_until_complete()

        self._with_retries(_wait, operation="wait_until_complete")
        final_state = self._to_raw_dict(job)
        logger.info("Batch job completed: %s", job_id)
        return final_state

    def get_batch_results(self, job_id: str) -> list[TranscriptionResult]:
        job = self._get_job_handle(job_id)
        file_results = self._with_retries(job.get_file_results, operation="get_file_results")
        successful = file_results.get("successful", []) if isinstance(file_results, dict) else []
        output_payloads = self._download_output_payloads(job, job_id=job_id)
        normalized: list[TranscriptionResult] = []
        for item in successful:
            output_payload = self._resolve_output_payload(item, output_payloads)
            normalized.append(self._normalize_batch_file_result(item, output_payload))
        return normalized

    def _with_retries(self, fn: Any, operation: str) -> Any:
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                return fn()
            except ApiError as err:
                last_error = err
                should_retry = err.status_code in {429, 503}
                if not should_retry or attempt >= self._max_retries:
                    logger.error("Sarvam API error during %s (status=%s): %s", operation, err.status_code, err)
                    raise
                sleep_seconds = self._retry_backoff_seconds * (2**attempt)
                logger.warning(
                    "Transient Sarvam API error during %s (status=%s). Retry %s/%s in %.1fs",
                    operation,
                    err.status_code,
                    attempt + 1,
                    self._max_retries,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
            except Exception as err:
                last_error = err
                if attempt >= self._max_retries:
                    logger.error("Operation %s failed after retries: %s", operation, err)
                    raise
                sleep_seconds = self._retry_backoff_seconds * (2**attempt)
                logger.warning(
                    "Unexpected error during %s. Retry %s/%s in %.1fs: %s",
                    operation,
                    attempt + 1,
                    self._max_retries,
                    sleep_seconds,
                    err,
                )
                time.sleep(sleep_seconds)

        if last_error:
            raise last_error
        raise RuntimeError("Unexpected transcription failure without error.")

    @staticmethod
    def _to_raw_dict(response: Any) -> dict[str, Any]:
        if hasattr(response, "model_dump"):
            return response.model_dump()
        if isinstance(response, dict):
            return response
        data = {}
        for name in ("job_id", "job_state", "request_id", "transcript", "language_code"):
            if hasattr(response, name):
                data[name] = getattr(response, name)
        return data

    @staticmethod
    def _normalize_batch_file_result(item: dict[str, Any], output_payload: dict[str, Any]) -> TranscriptionResult:
        transcript = (
            output_payload.get("transcript")
            or output_payload.get("text")
            or item.get("transcript")
            or item.get("text")
            or ""
        )
        source_file = item.get("file_name") or item.get("source_file") or "unknown"
        request_id = output_payload.get("request_id") or item.get("request_id")
        language_code = output_payload.get("language_code") or item.get("language_code")
        return TranscriptionResult(
            source_file=source_file,
            transcript=transcript,
            request_id=request_id,
            language_code=language_code,
            raw_response=item,
            full_response=output_payload,
        )

    def _get_job_handle(self, job_id: str) -> Any:
        if job_id in self._job_handles:
            return self._job_handles[job_id]
        if hasattr(self._client.speech_to_text_job, "get_job"):
            return self._client.speech_to_text_job.get_job(job_id=job_id)
        raise RuntimeError(f"Unable to retrieve batch job handle for job_id={job_id}")

    def _download_output_payloads(self, job: Any, job_id: str) -> dict[str, dict[str, Any]]:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            self._with_retries(
                lambda: job.download_outputs(output_dir=str(output_dir)),
                operation="download_outputs",
            )
            payloads: dict[str, dict[str, Any]] = {}
            for json_file in output_dir.rglob("*.json"):
                try:
                    payloads[json_file.name] = json.loads(json_file.read_text(encoding="utf-8"))
                except Exception as err:
                    logger.warning("Failed to parse batch output JSON %s: %s", json_file, err)
            logger.info("Resolved %s batch output JSON file(s) for job %s.", len(payloads), job_id)
            return payloads

    @staticmethod
    def _resolve_output_payload(item: dict[str, Any], output_payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
        # Different SDK/storage paths may expose output keys differently (e.g. "0.json" vs "HuliNayak.wav.json").
        candidates: list[str] = []
        output_file = item.get("output_file")
        file_name = item.get("file_name")
        if isinstance(output_file, str):
            candidates.append(Path(output_file).name)
        if isinstance(file_name, str):
            candidates.append(Path(file_name).name)
            candidates.append(f"{Path(file_name).name}.json")
            candidates.append(f"{Path(file_name).stem}.json")

        for key in candidates:
            if key in output_payloads:
                return output_payloads[key]

        if len(output_payloads) == 1:
            return next(iter(output_payloads.values()))

        return {}
