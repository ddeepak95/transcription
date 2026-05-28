from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from transcription.config import FFMPEG_CONVERT_EXTENSIONS, SUPPORTED_INPUT_EXTENSIONS
from transcription.io_utils import move_file, unique_output_dir
from transcription.providers.base import BatchSubmission, TranscriptionProvider, TranscriptionResult
from transcription.transcript_formatter import build_transcript_file_content, write_transcript_docx

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

        source_ext = input_path.suffix.lower()
        if source_ext not in FFMPEG_CONVERT_EXTENSIONS:
            return PreparedInput(
                source_path=input_path,
                upload_path=input_path,
                conversion_meta=None,
                temp_dir=None,
            )

        logger.info("Converting %s to wav before batch upload: %s", source_ext, input_path.name)
        temp_dir = TemporaryDirectory()
        upload_path, conversion_meta = self._convert_to_wav(input_path, Path(temp_dir.name))
        return PreparedInput(
            source_path=input_path,
            upload_path=upload_path,
            conversion_meta=conversion_meta,
            temp_dir=temp_dir,
        )

    def _write_output(self, item: PreparedInput, result: TranscriptionResult) -> Path:
        source_stem = item.source_path.stem
        output_dir = unique_output_dir(self._processed_dir, source_stem)
        output_dir.mkdir(parents=True, exist_ok=True)

        destination_input_path = output_dir / item.source_path.name
        if item.source_path.exists():
            move_file(item.source_path, destination_input_path)
        elif destination_input_path.exists():
            logger.warning(
                "Source file already moved; using existing file at %s",
                destination_input_path,
            )
        else:
            logger.warning(
                "Source file not found at %s; writing transcripts without moving input",
                item.source_path,
            )

        transcript_path = output_dir / f"{item.source_path.stem}_transcript.txt"
        transcript_path.write_text(
            build_transcript_file_content(
                audio_name=item.source_path.name,
                full_response=result.full_response,
                fallback_transcript=result.transcript,
            ),
            encoding="utf-8",
        )
        transcript_docx_path = output_dir / f"{item.source_path.stem}_transcript.docx"
        write_transcript_docx(
            output_path=transcript_docx_path,
            audio_name=item.source_path.name,
            full_response=result.full_response,
            fallback_transcript=result.transcript,
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
        return output_dir


    def download_job(self, job_id_or_path: str) -> PipelineSummary:
        job_payload, job_file = self._load_job_record(job_id_or_path)
        job_id = job_payload["job_id"]

        if job_payload.get("status") == "downloaded":
            output_dirs = job_payload.get("output_dirs", [])
            logger.info(
                "Job %s was already downloaded. Output folder(s): %s",
                job_id,
                ", ".join(output_dirs) if output_dirs else "(none recorded)",
            )
            return PipelineSummary(processed=0, failed=0, skipped=0)

        prepared_items = self._prepared_items_from_job(job_payload)
        logger.info("Downloading results for job %s (%s file(s))", job_id, len(prepared_items))

        self._provider.wait_for_batch(job_id)
        results = self._provider.get_batch_results(job_id)
        by_source = {result.source_file: result for result in results}

        processed = 0
        failed = 0
        output_dirs: list[str] = []
        for item in prepared_items:
            result = by_source.get(item.upload_path.name)
            if not result:
                logger.error("No completed result found for file: %s", item.source_path.name)
                failed += 1
                continue
            output_dir = self._write_output(item, result)
            output_dirs.append(str(output_dir))
            processed += 1

        job_payload["status"] = "downloaded"
        job_payload["downloaded_at"] = datetime.now(timezone.utc).isoformat()
        job_payload["output_dirs"] = output_dirs
        job_file.write_text(json.dumps(job_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Job %s download complete. Tracking file updated: %s", job_id, job_file)

        return PipelineSummary(processed=processed, failed=failed, skipped=0)

    @staticmethod
    def list_job_records(processed_dir: Path) -> list[dict[str, Any]]:
        jobs_dir = processed_dir / "_jobs"
        if not jobs_dir.exists():
            return []

        records: list[dict[str, Any]] = []
        for job_file in sorted(jobs_dir.glob("*.json"), key=lambda path: path.name.lower()):
            try:
                payload = json.loads(job_file.read_text(encoding="utf-8"))
            except Exception as err:
                logger.warning("Skipping invalid job file %s: %s", job_file, err)
                continue
            if isinstance(payload, dict):
                payload["_tracking_file"] = str(job_file)
                records.append(payload)
        return records

    def _jobs_dir(self) -> Path:
        return self._processed_dir / "_jobs"

    def _load_job_record(self, job_id_or_path: str) -> tuple[dict[str, Any], Path]:
        candidate = Path(job_id_or_path)
        if candidate.is_file():
            job_file = candidate
        else:
            job_file = self._jobs_dir() / f"{job_id_or_path}.json"
            if not job_file.is_file():
                job_file = self._jobs_dir() / job_id_or_path
                if job_file.suffix != ".json":
                    job_file = job_file.with_suffix(".json")

        if not job_file.is_file():
            raise FileNotFoundError(
                f"Job tracking file not found for '{job_id_or_path}'. "
                f"Expected {self._jobs_dir() / f'{job_id_or_path}.json'} or an explicit path."
            )

        payload = json.loads(job_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload.get("job_id"):
            raise ValueError(f"Invalid job tracking file (missing job_id): {job_file}")
        return payload, job_file

    @staticmethod
    def _prepared_items_from_job(job_payload: dict[str, Any]) -> list[PreparedInput]:
        items_payload = job_payload.get("items")
        if isinstance(items_payload, list) and items_payload:
            prepared: list[PreparedInput] = []
            for entry in items_payload:
                if not isinstance(entry, dict):
                    continue
                source_file = entry.get("source_file")
                upload_file_name = entry.get("upload_file_name")
                if not source_file or not upload_file_name:
                    raise ValueError("Job tracking item is missing source_file or upload_file_name.")
                prepared.append(
                    PreparedInput(
                        source_path=Path(source_file),
                        upload_path=Path(upload_file_name),
                        conversion_meta=entry.get("conversion"),
                        temp_dir=None,
                    )
                )
            if prepared:
                return prepared

        source_files = job_payload.get("source_files", [])
        upload_file_names = job_payload.get("upload_file_names", [])
        if not source_files or not upload_file_names:
            raise ValueError("Job tracking file has no source_files/upload_file_names or items.")
        if len(source_files) != len(upload_file_names):
            raise ValueError("Job tracking file source_files and upload_file_names length mismatch.")

        return [
            PreparedInput(
                source_path=Path(source_file),
                upload_path=Path(upload_name),
                conversion_meta=None,
                temp_dir=None,
            )
            for source_file, upload_name in zip(source_files, upload_file_names, strict=True)
        ]

    def _write_job_tracking_file(self, submission: BatchSubmission, prepared_items: list[PreparedInput]) -> None:
        jobs_dir = self._jobs_dir()
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_file = jobs_dir / f"{submission.job_id}.json"
        payload = {
            "job_id": submission.job_id,
            "state": submission.state,
            "status": "submitted",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "source_files": [str(item.source_path) for item in prepared_items],
            "upload_file_names": [item.upload_path.name for item in prepared_items],
            "items": [
                {
                    "source_file": str(item.source_path),
                    "upload_file_name": item.upload_path.name,
                    "conversion": item.conversion_meta,
                }
                for item in prepared_items
            ],
            "raw_response": submission.raw_response,
        }
        job_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Batch submit mode: wrote job tracking file: %s", job_file)

    @staticmethod
    def _convert_to_wav(input_path: Path, temp_dir: Path) -> tuple[Path, dict[str, Any]]:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "ffmpeg is required for .mp4 and .m4a input support but was not found in PATH. "
                "Install ffmpeg and retry."
            )

        source_ext = input_path.suffix.lower()
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
            "source_format": source_ext,
            "transcription_format": ".wav",
            "ffmpeg_command": command,
        }
        return output_path, meta
