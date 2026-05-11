# Qwen3 TTS 批量工具

[English](./README.md)

这是一个小型批量 TTS 工具，用于把 UTF-8 或常见中文编码的文本文件转换成 WAV 音频，并可按规则插入本地音效。

项目已经整理成可独立克隆和运行的形态，不包含体积较大的模型文件。

当前这套代码主要基于 macOS 做开发和测试，其他环境不保证已经完成同等验证。

## 功能

- 支持单个文本、目录内全部 `.txt` 文件，或直接通过 `--text` 输入生成 WAV。
- 支持通过 `tts_resume_state.json` 进行中断续跑。
- 使用 `chardet` 识别常见文本编码。
- 自动把长文本拆成更安全的 TTS 片段。
- 可选插入本地 WAV 音效，用于明确的非语音提示。
- 记录运行日志和系统资源使用情况。

## 目录结构

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

## 环境要求

- 当前代码主要在 macOS 下开发和测试。
- 原始工作流更适合在 macOS 上运行。
- 推荐 Python 3.11。
- 需要一个兼容 `mlx_audio.tts.generate` 的本地模型。

建议先创建虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 模型与配置

仓库不包含模型文件，因为模型体积较大且依赖本地环境。

可以在 `config/default_config.json` 中设置默认模型路径：

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

音效关键词映射已经放到 `config/sfx_config.json`，后续如果要替换 wav 或调整匹配规则，不需要再改 Python 代码。

当前启用的 SFX 配置只保留非语音类音效，例如撞击、风雨、雷声、门响、摩擦、爆裂等；NSFW 倾向的语音拟声不会在默认配置中被匹配或插入。

## 后续扩展音频库

如果后面要继续加本地 wav 资源，建议按下面的方式处理：

1. 把新的 `.wav` 文件放进 `sfx_library/`。
2. 在 `config/sfx_config.json` 里补充或修改关键词到 wav 文件的映射。
3. 如果希望脚本能自动识别这些新音效，再同步调整同一个配置文件里的 `non_speech_sfx_keys` 和 `sfx_patterns`。

也就是说，后续新增音频库和调整音效行为，优先改 `config/sfx_config.json`，而不是回到 Python 代码里改常量。

也可以在运行时直接覆盖模型路径：

```bash
python tts_from_utf8txt_final_v2.py --text "你好，欢迎使用。" --model /path/to/Qwen3-TTS-model
```

## 使用方式

直接从一段文本生成音频：

```bash
python tts_from_utf8txt_final_v2.py --text "你好，欢迎使用。"
```

处理单个文本文件：

```bash
python tts_from_utf8txt_final_v2.py --input input.txt --output output_audio
```

处理目录下全部 `.txt` 文件：

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --output output_audio
```

禁用续传，从头开始：

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --no-resume
```

禁用本地音效插入：

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --disable-sfx
```

修改分段长度：

```bash
python tts_from_utf8txt_final_v2.py --input ./texts --chunk-size 160
```

## 运行产物

以下文件会在本地产生：

- `output_audio/`
- `tts_run.log`
- `tts_resume_state.json`
- `*.normalized.wav`
- `*.prepared.wav`

## 说明

- `audioop` 用于 WAV 格式转换，但它在 Python 3.13 中已被弃用，当前建议使用 Python 3.11 或 3.12。
- `sfx_library` 中只保留 `config/sfx_config.json` 当前会引用到的非语音 wav 资源。
- 如果 TTS 后端找不到 `mlx_audio.tts.generate`，先确认当前环境已经正确安装 `mlx-audio`。
- 如果后面要维护多套音效资源，建议统一把 wav 放在 `sfx_library/`，再通过 `config/sfx_config.json` 和 `config/default_config.json` 控制启用方式。
