# Transcription Pipeline

Batch transcription pipeline using Sarvam Speech-to-Text, designed with a provider abstraction so additional services can be added later.

## Prerequisites

- Python 3.13+
- `SARVAM_API_KEY` in environment or in a `.env` file (required for transcription and job download; not required for `--format-json`)
- `ffmpeg` in `PATH` for `.mp4` and `.m4a` input (audio is converted before upload)

## Install

```bash
pip install -e .
```

## Quick start

```bash
python main.py
```

Reads supported files from `data/source`, transcribes via Sarvam, and writes outputs under `data/processed/`. Default batch mode is `wait` (submit, poll until done, write transcripts).

## CLI reference

All options for `python main.py`:

| Option | Default | Description |
|--------|---------|-------------|
| `--source` | `data/source` | Directory scanned for input files (ignored when `--file` is set). |
| `--processed` | `data/processed` | Output directory for transcripts, `result.json`, and `_jobs/` tracking files. |
| `--file` | *(none)* | Process a single file instead of scanning `--source`. |
| `--model` | `saaras:v3` | Sarvam model name passed to the batch API. |
| `--mode` | `translate` | Sarvam output mode (for example `translate`, `transcribe`, `verbatim`). |
| `--language-code` | *(none)* | Optional BCP-47 input language hint (for example `kn-IN`, `hi-IN`). |
| `--batch-mode` | `wait` | `wait`: submit and block until complete. `submit`: submit and exit (use `--download-job` later). |
| `--job-poll-interval` | `2.0` | Seconds between polls while waiting for batch completion (`wait` mode and `--download-job`). |
| `--num-speakers` | *(none)* | Optional fixed speaker count for diarization. |
| `--log-level` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `--format-json` | *(none)* | Format an existing Sarvam output JSON into `.txt` and `.docx` transcripts; no API call. |
| `--format-output` | *(none)* | Output `.txt` path for `--format-json` (default: derived from input path and audio name). |
| `--format-audio-name` | *(none)* | Display name in the transcript header for `--format-json`. |
| `--download-job` | *(none)* | Job ID or path to `_jobs/<job_id>.json`; wait, download results, write outputs. |
| `--list-jobs` | *(flag)* | List batch jobs under `data/processed/_jobs/` and exit. |

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SARVAM_API_KEY` | Yes* | Sarvam API subscription key. |

\* Not required when using only `--format-json`.

## Usage examples

### Default batch run

```bash
python main.py
```

### Custom source and output folders

```bash
python main.py --source "custom_source" --processed "custom_processed"
```

### Single file

```bash
python main.py --file "data/source/example.mp3"
```

### Language, mode, and diarization

```bash
python main.py --mode translate --language-code kn-IN --num-speakers 3
```

### Submit now, download later

```bash
# 1) Submit and exit (tracking file: data/processed/_jobs/<job_id>.json)
python main.py --batch-mode submit --file "data/source/recording.m4a" --language-code kn-IN

# 2) List jobs
python main.py --list-jobs

# 3) Download when ready
python main.py --download-job "20260528_402eec78-fb55-4809-8a5c-d55e41c5969f"
```

Or pass the tracking file path:

```bash
python main.py --download-job "data/processed/_jobs/20260528_402eec78-fb55-4809-8a5c-d55e41c5969f.json"
```

### Format existing Sarvam JSON (no transcription)

```bash
python main.py --format-json "data/processed/sample/0.json"
```

With optional output path and header name:

```bash
python main.py \
  --format-json "data/processed/sample/0.json" \
  --format-output "data/processed/sample/sample_transcript.txt" \
  --format-audio-name "sample.mp4"
```

### Debug logging

```bash
python main.py --log-level DEBUG --file "data/source/test.wav"
```

## Batch workflows

| Workflow | Command(s) | Result |
|----------|------------|--------|
| One-step | `python main.py` (default `--batch-mode wait`) | Submit → wait → transcripts → move source files |
| Two-step | `--batch-mode submit`, then `--download-job <id>` | Submit now; fetch results later |
| List jobs | `--list-jobs` | Print job id, status, and source file names |
| Re-format only | `--format-json <path>` | Build `.txt` / `.docx` from saved Sarvam JSON |

Job tracking files live at `data/processed/_jobs/<job_id>.json`. After a successful `--download-job`, status is set to `downloaded`.

## Supported inputs

- Audio: `.wav`, `.mp3`, `.aac`, `.flac`, `.ogg`, `.m4a`
- Video: `.mp4`
- Container formats (`.mp4`, `.m4a`): extracted to temporary 16 kHz mono WAV via ffmpeg before batch upload

## Output structure

In `wait` mode (or after `--download-job`), each input is moved into its own folder:

```text
data/processed/
  sample_audio/
    sample_audio.mp3
    sample_audio_transcript.txt
    sample_audio_transcript.docx
    result.json
    0.json
  _jobs/
    <job_id>.json
```

`result.json` includes transcript metadata, per-file provider response, and full batch output (`full_response`). The raw Sarvam output file (for example `0.json`) is saved in the same folder. For `.mp4` and `.m4a` sources, `result.json` also includes `conversion` metadata.

Transcript output is diarized when diarization entries are available (speaker labels and timestamps). Both `.txt` and `.docx` files are written.
