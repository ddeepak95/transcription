from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from transcription.config import SUPPORTED_INPUT_EXTENSIONS
from transcription.io_utils import move_file, unique_output_dir
from transcription.providers.base import BatchSubmission, TranscriptionProvider, TranscriptionResult
from transcription.transcript_formatter import build_transcript_file_content

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineSummary:
    processed: int
    failed: int
    skipped: int


@dataclass(slots=True)
class PreparedInput:
    source_path: Path
    upload_path: Path
    conversion_meta: dict[str, Any] | None
    temp_dir: TemporaryDirectory[str] | None


class TranscriptionPipeline:
    def __init__(
        self,
        provider: TranscriptionProvider,
        processed_dir: Path,
        language_code: str | None = None,
        batch_mode: str = "wait",
        num_speakers: int | None = None,
    ) -> None:
        self._provider = provider
        self._processed_dir = processed_dir
        self._language_code = language_code
        self._batch_mode = batch_mode
        self._num_speakers = num_speakers

    def process_many(self, paths: list[Path]) -> PipelineSummary:
        total = len(paths)
        valid_paths: list[Path] = []
        skipped = 0
        for index, path in enumerate(paths, start=1):
            logger.info("Processing file %s/%s: %s", index, total, path.name)
            if path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
                logger.warning("Skipping unsupported file: %s", path)
                skipped += 1
                continue
            valid_paths.append(path)

        if not valid_paths:
            return PipelineSummary(processed=0, failed=0, skipped=skipped)

        prepared_items = [self._prepare_input(path) for path in valid_paths]
        try:
            submission = self._provider.submit_batch(
                file_paths=[item.upload_path for item in prepared_items],
                language_code=self._language_code,
                with_diarization=True,
                num_speakers=self._num_speakers,
            )
            logger.info("Batch submitted: job_id=%s state=%s", submission.job_id, submission.state)

            if self._batch_mode == "submit":
                self._write_job_tracking_file(submission, prepared_items)
                return PipelineSummary(processed=0, failed=0, skipped=skipped)

            return self._finalize_wait_mode(submission, prepared_items, skipped=skipped)
        finally:
            for item in prepared_items:
                if item.temp_dir is not None:
                    item.temp_dir.cleanup()

    def _finalize_wait_mode(
        self,
        submission: BatchSubmission,
        prepared_items: list[PreparedInput],
        skipped: int,
    ) -> PipelineSummary:
        self._provider.wait_for_batch(submission.job_id)
        results = self._provider.get_batch_results(submission.job_id)
        by_source = {result.source_file: result for result in results}

        processed = 0
        failed = 0
        for item in prepared_items:
            result = by_source.get(item.upload_path.name)
            if not result:
                logger.error("No completed result found for file: %s", item.source_path.name)
                failed += 1
                continue
            self._write_output(item, result)
            processed += 1

        return PipelineSummary(processed=processed, failed=failed, skipped=skipped)

    def _prepare_input(self, input_path: Path) -> PreparedInput:
        if not input_path.exists() or not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        if input_path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
            raise ValueError(f"Unsupported input extension: {input_path.suffix}")

        if input_path.suffix.lower() != ".mp4":
            return PreparedInput(
                source_path=input_path,
                upload_path=input_path,
                conversion_meta=None,
                temp_dir=None,
            )

        logger.info("Converting .mp4 to wav before batch upload: %s", input_path.name)
        temp_dir = TemporaryDirectory()
        upload_path, conversion_meta = self._convert_mp4_to_wav(input_path, Path(temp_dir.name))
        return PreparedInput(
            source_path=input_path,
            upload_path=upload_path,
            conversion_meta=conversion_meta,
            temp_dir=temp_dir,
        )

    def _write_output(self, item: PreparedInput, result: TranscriptionResult) -> None:
        source_stem = item.source_path.stem
        output_dir = unique_output_dir(self._processed_dir, source_stem)
        output_dir.mkdir(parents=True, exist_ok=True)

        destination_input_path = output_dir / item.source_path.name
        move_file(item.source_path, destination_input_path)

        transcript_path = output_dir / f"{item.source_path.stem}_transcript.txt"
        transcript_path.write_text(
            build_transcript_file_content(
                audio_name=item.source_path.name,
                full_response=result.full_response,
                fallback_transcript=result.transcript,
            ),
            encoding="utf-8",
        )

        result_payload: dict[str, Any] = {
            "source_file": item.source_path.name,
            "uploaded_file_name": item.upload_path.name,
            "request_id": result.request_id,
            "language_code": result.language_code,
            "transcript": result.transcript,
            "provider_response": result.raw_response,
            "full_response": result.full_response,
        }
        if item.conversion_meta:
            result_payload["conversion"] = item.conversion_meta

        result_json_path = output_dir / "result.json"
        result_json_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        output_file_name = result.raw_response.get("output_file") or "sarvam_output.json"
        full_output_path = output_dir / output_file_name
        full_output_path.write_text(json.dumps(result.full_response, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Output written to: %s", output_dir)


    def _write_job_tracking_file(self, submission: BatchSubmission, prepared_items: list[PreparedInput]) -> None:
        jobs_dir = self._processed_dir / "_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_file = jobs_dir / f"{submission.job_id}.json"
        payload = {
            "job_id": submission.job_id,
            "state": submission.state,
            "source_files": [str(item.source_path) for item in prepared_items],
            "upload_file_names": [item.upload_path.name for item in prepared_items],
            "raw_response": submission.raw_response,
        }
        job_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Batch submit mode: wrote job tracking file: %s", job_file)

    @staticmethod
    def _convert_mp4_to_wav(input_path: Path, temp_dir: Path) -> tuple[Path, dict[str, Any]]:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "ffmpeg is required for .mp4 input support but was not found in PATH. "
                "Install ffmpeg and retry."
            )

        output_path = temp_dir / f"{input_path.stem}.wav"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output_path),
        ]
        process = subprocess.run(command, capture_output=True, text=True, check=False)
        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed: {process.stderr.strip() or process.stdout.strip()}")
        meta = {
            "source_format": ".mp4",
            "transcription_format": ".wav",
            "ffmpeg_command": command,
        }
        return output_path, meta
