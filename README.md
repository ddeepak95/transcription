# Transcription Pipeline

Batch transcription pipeline using Sarvam Speech-to-Text, designed with a provider abstraction so additional services can be added later.

## Prerequisites

- Python 3.13+
- `SARVAM_API_KEY` in environment or in `.env` file
- `ffmpeg` in `PATH` if you want `.mp4` input support

## Install

```bash
pip install -e .
```

## Usage

Default run (batch mode `wait`):

```bash
python main.py
```

By default, inputs are read from `data/source` and outputs are written to `data/processed`.

Default transcription mode is `translate`. Override with `--mode` if needed.
Default batch jobs run with diarization enabled.

Custom folders:

```bash
python main.py --source "custom_source" --processed "custom_processed"
```

Process exactly one file:

```bash
python main.py --file "source/example.mp3"
```

Batch submit-only mode (do not wait for completion):

```bash
python main.py --batch-mode submit
```

Language code and mode override:

```bash
python main.py --mode "translate" --language-code "hi-IN"
```

Set fixed speaker count for diarization:

```bash
python main.py --num-speakers 2
```

Format JSON to transcript text only (no transcription run):

```bash
python main.py --format-json "data/processed/HuliNayak/0.json"
```

Optional output path and display audio name:

```bash
python main.py --format-json "data/processed/HuliNayak/0.json" --format-output "data/processed/HuliNayak/HuliNayak_transcript.txt" --format-audio-name "HuliNayak.mp4"
```

## Batch modes

- `wait`: submit batch job and wait until complete, then write outputs and move source files.
- `submit`: submit batch job and exit immediately. Job metadata is written in `data/processed/_jobs/<job_id>.json`.

## Supported inputs

- Audio: `.wav`, `.mp3`, `.aac`, `.flac`, `.ogg`
- Video: `.mp4` (audio is extracted to temporary wav using ffmpeg before batch upload)

## Output structure

In `wait` mode, each processed input is moved to its own folder:

```text
data/processed/
  sample_audio/
    sample_audio.mp3
    transcript.txt
    result.json
    0.json
  _jobs/
    <job_id>.json
```

`result.json` includes transcript metadata, file-level provider response, and full resolved batch output JSON (`full_response`). The raw downloaded output file (for example `0.json`) is also saved in the same processed audio folder. For `.mp4` sources, conversion metadata is also included.

Transcript text output is diarized when diarization entries are available, with speaker labels and timestamps.
