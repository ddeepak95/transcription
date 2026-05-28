from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from transcription.config import AppConfig, SUPPORTED_INPUT_EXTENSIONS
from transcription.io_utils import find_source_files, is_supported_input
from transcription.pipeline import TranscriptionPipeline
from transcription.transcript_formatter import (
    build_transcript_file_content,
    default_transcript_output_path,
    write_transcript_docx,
)


class ColorFormatter(logging.Formatter):
    RESET = "\x1b[0m"
    LEVEL_COLORS = {
        logging.DEBUG: "\x1b[36m",    # cyan
        logging.INFO: "\x1b[32m",     # green
        logging.WARNING: "\x1b[33m",  # yellow
        logging.ERROR: "\x1b[31m",    # red
        logging.CRITICAL: "\x1b[35m", # magenta
    }

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = self.LEVEL_COLORS.get(record.levelno)
        if not color:
            return message
        return f"{color}{message}{self.RESET}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process speech files with Sarvam STT.")
    parser.add_argument("--source", type=Path, default=Path("data/source"), help="Source directory of input files.")
    parser.add_argument(
        "--processed",
        type=Path,
        default=Path("data/processed"),
        help="Processed output directory.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Process exactly one file; skips source directory scan.",
    )
    parser.add_argument("--model", default="saaras:v3", help="Sarvam model name.")
    parser.add_argument("--mode", default="translate", help="Sarvam mode (default: translate).")
    parser.add_argument(
        "--language-code",
        default=None,
        help="Optional input BCP-47 language code (for example: hi-IN).",
    )
    parser.add_argument(
        "--batch-mode",
        choices=["wait", "submit"],
        default="wait",
        help="Batch execution mode: wait for completion or submit-only.",
    )
    parser.add_argument(
        "--job-poll-interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds while waiting for batch completion.",
    )
    parser.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Optional fixed speaker count for diarization.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--format-json",
        type=Path,
        default=None,
        help="Format a Sarvam output JSON into transcript text and exit.",
    )
    parser.add_argument(
        "--format-output",
        type=Path,
        default=None,
        help="Output path for --format-json mode.",
    )
    parser.add_argument(
        "--format-audio-name",
        default=None,
        help="Audio name to display in transcript header for --format-json mode.",
    )
    parser.add_argument(
        "--download-job",
        metavar="JOB_ID",
        default=None,
        help="Wait for a submitted batch job, download results, and write outputs. "
        "JOB_ID is the Sarvam job id or a path to data/processed/_jobs/<job_id>.json.",
    )
    parser.add_argument(
        "--list-jobs",
        action="store_true",
        help="List submitted batch jobs from data/processed/_jobs and exit.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> AppConfig:
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("Missing SARVAM_API_KEY environment variable.")

    return AppConfig(
        source_dir=args.source,
        processed_dir=args.processed,
        one_file=args.file,
        api_key=api_key,
        model=args.model,
        mode=args.mode,
        language_code=args.language_code,
        batch_mode=args.batch_mode,
        job_poll_interval=args.job_poll_interval,
        num_speakers=args.num_speakers,
    )


def collect_inputs(config: AppConfig) -> list[Path]:
    if config.one_file is not None:
        if not config.one_file.exists():
            raise FileNotFoundError(f"Specified --file does not exist: {config.one_file}")
        if not is_supported_input(config.one_file, SUPPORTED_INPUT_EXTENSIONS):
            raise ValueError(f"Unsupported --file extension: {config.one_file.suffix}")
        return [config.one_file]

    return find_source_files(config.source_dir, SUPPORTED_INPUT_EXTENSIONS)


def run_list_jobs(args: argparse.Namespace) -> int:
    records = TranscriptionPipeline.list_job_records(args.processed)
    logger = logging.getLogger("transcription")
    if not records:
        logger.info("No batch jobs found in: %s", args.processed / "_jobs")
        return 0

    for record in records:
        job_id = record.get("job_id", "unknown")
        status = record.get("status", "unknown")
        source_files = record.get("source_files") or [
            item.get("source_file") for item in record.get("items", []) if isinstance(item, dict)
        ]
        source_summary = ", ".join(Path(path).name for path in source_files[:3])
        if len(source_files) > 3:
            source_summary += f", +{len(source_files) - 3} more"
        logger.info("job_id=%s status=%s files=[%s]", job_id, status, source_summary)
        if status == "downloaded":
            output_dirs = record.get("output_dirs", [])
            if output_dirs:
                logger.info("  output: %s", ", ".join(output_dirs))
    return 0


def run_download_job(args: argparse.Namespace) -> int:
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("Missing SARVAM_API_KEY environment variable.")

    from transcription.providers.sarvam import SarvamTranscriptionProvider

    logger = logging.getLogger("transcription")
    provider = SarvamTranscriptionProvider(
        api_key=api_key,
        model=args.model,
        mode=args.mode,
        job_poll_interval_seconds=args.job_poll_interval,
    )
    pipeline = TranscriptionPipeline(
        provider=provider,
        processed_dir=args.processed,
    )
    summary = pipeline.download_job(args.download_job)
    logger.info(
        "Download complete. processed=%s failed=%s skipped=%s",
        summary.processed,
        summary.failed,
        summary.skipped,
    )
    return 1 if summary.failed else 0


def run_format_only(args: argparse.Namespace) -> int:
    if args.format_json is None:
        return -1
    if not args.format_json.exists():
        raise FileNotFoundError(f"--format-json file does not exist: {args.format_json}")

    payload = json.loads(args.format_json.read_text(encoding="utf-8"))
    full_response = payload.get("full_response", payload) if isinstance(payload, dict) else payload
    audio_name = args.format_audio_name or payload.get("source_file") or args.format_json.stem
    output_path = args.format_output or default_transcript_output_path(args.format_json, audio_name=Path(audio_name).stem)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fallback_transcript = payload.get("transcript", "") if isinstance(payload, dict) else ""
    output_text = build_transcript_file_content(
        audio_name=audio_name,
        full_response=full_response if isinstance(full_response, dict) else {},
        fallback_transcript=fallback_transcript,
    )
    output_path.write_text(output_text, encoding="utf-8")
    docx_output_path = output_path.with_suffix(".docx")
    write_transcript_docx(
        output_path=docx_output_path,
        audio_name=audio_name,
        full_response=full_response if isinstance(full_response, dict) else {},
        fallback_transcript=fallback_transcript,
    )
    logging.getLogger("transcription").info(
        "Formatted transcript written to: %s and %s",
        output_path,
        docx_output_path,
    )
    return 0


def main() -> int:
    load_dotenv()
    args = parse_args()
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, args.log_level, logging.INFO))
    root_logger.handlers.clear()

    handler = logging.StreamHandler()
    log_format = "%(asctime)s %(levelname)s %(name)s - %(message)s"
    if handler.stream.isatty():
        handler.setFormatter(ColorFormatter(log_format))
    else:
        handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(handler)

    logger = logging.getLogger("transcription")
    if args.format_json is not None:
        return run_format_only(args)
    if args.list_jobs:
        return run_list_jobs(args)
    if args.download_job is not None:
        return run_download_job(args)

    config = build_config(args)

    config.processed_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Starting pipeline mode=%s batch_mode=%s source=%s processed=%s single_file=%s language_code=%s diarization=true num_speakers=%s",
        config.mode,
        config.batch_mode,
        config.source_dir,
        config.processed_dir,
        config.one_file,
        config.language_code,
        config.num_speakers,
    )

    from transcription.providers.sarvam import SarvamTranscriptionProvider

    provider = SarvamTranscriptionProvider(
        api_key=config.api_key,
        model=config.model,
        mode=config.mode,
        job_poll_interval_seconds=config.job_poll_interval,
    )
    pipeline = TranscriptionPipeline(
        provider=provider,
        processed_dir=config.processed_dir,
        language_code=config.language_code,
        batch_mode=config.batch_mode,
        num_speakers=config.num_speakers,
    )

    inputs = collect_inputs(config)
    if not inputs:
        logger.info("No supported input files found in: %s", config.source_dir)
        return 0

    summary = pipeline.process_many(inputs)
    logger.info(
        "Run complete. processed=%s failed=%s skipped=%s",
        summary.processed,
        summary.failed,
        summary.skipped,
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
