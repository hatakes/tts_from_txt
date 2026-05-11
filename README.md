# Qwen3 TTS Batch Toolkit

[中文说明](./README.zh.md)

A small batch TTS utility for converting UTF-8 or common Chinese text files into WAV audio with optional local sound-effect insertion.

The project is packaged so it can be cloned and run independently. Large model files are intentionally not included.

This codebase is currently developed and tested on macOS. Other environments may work, but they are not the primary validation target yet.

## Features

- Convert one text file, a directory of `.txt` files, or direct `--text` input to WAV.
- Resume interrupted batch runs with `tts_resume_state.json`.
- Read common text encodings with `chardet`.
- Split long text into safer TTS chunks.
- Optionally insert local WAV sound effects for clear non-speech cues.
- Log runtime progress and system resource usage.

## Project Layout

```text
.
├── tts_from_utf8txt_final_v2.py
├── monitor_logger.py
├── config/
│   ├── default_config.json
│   └── sfx_config.json
├── sfx_library/
│   └── *.wav
├── requirements.txt
├── README.md
└── README.zh.md
```

## Requirements

- The code is currently developed and tested on macOS.
- macOS is recommended for the original MLX workflow.
- Python 3.11 is recommended.
- A local model compatible with `mlx_audio.tts.generate`.

Install dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Model Setup

Model files are not included in this repository because they are large and environment-specific.

Set your local model path in `config/default_config.json`:

```json
{
  "model": "/path/to/Qwen3-TTS-model",
  "voice": "serena",
  "lang": "zh",
  "cfg_scale": "1.2",
  "enable_sfx": true,
  "sfx_dir": "sfx_library",
  "sfx_config_file": "config/sfx_config.json",
  "resume_state_file": "tts_resume_state.json"
}
```

The sound-effect keyword mapping now lives in `config/sfx_config.json`, so changing which local WAV is used no longer requires editing Python code.

The current bundled SFX set is intentionally limited to non-speech cues such as impacts, weather, creaks, and door knocks. NSFW-style voice cues are not included in the active configuration.

## Extending Audio Assets

To add more local WAV assets later:

1. Put the new `.wav` files into `sfx_library/`.
2. Add or update keyword-to-file mappings in `config/sfx_config.json`.
3. Adjust `non_speech_sfx_keys` and `sfx_patterns` in the same file if you want the new assets to be detected automatically.

In practice, audio-library expansion and behavior tuning should happen in `config/sfx_config.json` first, rather than by changing Python constants.

You can also override the model path at runtime:

```bash
python tts_from_utf8txt_final_v2.py --text "你好，欢迎使用。" --model /path/to/Qwen3-TTS-model
```

## Usage

Generate audio from direct text:

```bash
python tts_from_utf8txt_final_v2.py --text "你好，欢迎使用。"
```

Generate audio from one text file:

```bash
python tts_from_utf8txt_final_v2.py --input input.txt --output output_audio
```

Generate audio from every `.txt` file in a directory:

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --output output_audio
```

Disable resume state and start from the beginning:

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --no-resume
```

Disable local sound effects:

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --disable-sfx
```

Use a different chunk size:

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --chunk-size 160
```

## Runtime Files

The following files are generated locally and ignored by git:

- `output_audio/`
- `tts_run.log`
- `tts_resume_state.json`
- `*.normalized.wav`
- `*.prepared.wav`

## Notes

- `audioop` is used for WAV conversion and is deprecated in Python 3.13. Use Python 3.11 or 3.12 for now.
- The bundled `sfx_library` only keeps the non-speech WAV assets referenced by `config/sfx_config.json`.
- If your TTS backend cannot find `mlx_audio.tts.generate`, verify that `mlx-audio` is installed in the active environment.
- If you plan to maintain multiple sound-effect sets, keep the WAV files in `sfx_library/` and switch behavior through `config/sfx_config.json` and `config/default_config.json`.
