# MiniMax 音色快速复刻（Voice Clone）

> 官方文档：[https://platform.minimaxi.com/docs/guides/speech-voice-clone](https://platform.minimaxi.com/docs/guides/speech-voice-clone)

把官方文档的 4 段示例（上传复刻音频 / 上传参考音频 / 音色克隆 / 完整示例）整理成 4 个可直接运行的 Python 脚本，并把官方示例里的几处 bug 修了。

## 📁 文件结构

```
MiniMax-voice-clone/
├── README.md                 # 本文件
├── requirements.txt          # Python 依赖
├── .env.example              # 环境变量模板（拷贝为 .env 后填入真实 Key）
│
├── upload_clone_audio.py     # Step 1：上传待克隆音频（独立脚本）
├── upload_prompt_audio.py    # Step 2：上传参考音频（独立脚本，可选）
├── voice_clone.py            # Step 3：调用 /v1/voice_clone（独立脚本）
│
├── clone_voice.py            # 一键脚本：上传 + 克隆 + 保存试听音频
│
└── audios/                   # 存放待上传的本地音频与克隆后的试听音频
    ├── clone_input.mp3       # 待克隆音频（10s~5min，<=20MB）
    └── cloned_preview.mp3    # 脚本运行后生成的试听音频
```

## ⚙️ 准备

1. 注册 [MiniMax 开放平台](https://platform.minimaxi.com) → 控制台 → 用户中心 → 拿到 **API Key**
2. 准备一段待克隆音频：10s ~ 5min、<= 20MB、mp3/m4a/wav，放到 `audios/clone_input.mp3`
3. （推荐）再准备一段 < 8s 的参考音频 `audios/clone_prompt.mp3`，并准备好对应的文字

## 🚀 三种运行方式

### 方式 A：一键脚本（推荐）

```bash
cp .env.example .env          # 填入真实 API Key
# 把音频放到 ./audios/ 下

pip install -r requirements.txt

python clone_voice.py \
  --clone-audio ./audios/clone_input.mp3 \
  --prompt-audio ./audios/clone_prompt.mp3 \
  --prompt-text "后来认为啊，是有人抓这鸡，可是抓鸡的地方呢没人听过鸡叫。" \
  --voice-id my_voice_01 \
  --text "大兄弟，听您口音不是本地人吧..." \
  --model speech-2.8-hd
```

跑完后，`./audios/cloned_preview.mp3` 就是新音色朗读试听文本的音频，可以直接听效果。
同时接口返回的 `voice_id`（与你传的 `--voice-id` 一致）可以拿去复用 T2A 语音合成接口。

### 方式 B：分三步独立调用

```bash
# Step 1：上传复刻音频
python upload_clone_audio.py --audio ./audios/clone_input.mp3
# → 拿到 file_id，复制下来

# Step 2：上传参考音频（可选）
python upload_prompt_audio.py --audio ./audios/clone_prompt.mp3 --text "..."
# → 拿到 prompt_file_id

# Step 3：发起克隆
python voice_clone.py \
  --file-id <Step 1 的 file_id> \
  --voice-id my_voice_01 \
  --prompt-file-id <Step 2 的 prompt_file_id> \
  --prompt-text "..." \
  --text "试听文本"
```

### 方式 C：作为库 import

```python
from clone_voice import clone_voice

body = clone_voice(
    clone_audio_path="./audios/clone_input.mp3",
    voice_id="my_voice_01",
    prompt_audio_path="./audios/clone_prompt.mp3",
    prompt_text="后来认为啊，是有人抓这鸡，可是抓鸡的地方呢没人听过鸡叫。",
    text="大兄弟，听您口音不是本地人吧...",
    model="speech-2.8-hd",
    preview_out="./audios/cloned_preview.mp3",
)
print(body["voice_id"])
```

## 💰 计费说明（重要）

- **9.9 元复刻费**：在 *首次* 使用该 `voice_id` 调用 T2A 语音合成接口时收取。
- **试听音频合成**：脚本运行时试听也会按字符数走 T2A 语音合成计费。
- 详细定价见官方 [产品定价页](https://platform.minimaxi.com/docs/pricing)。

> MiniMax 还有更省心的官方 Web 工具：**[音色复刻抽卡工具（Voice ID Lucky Draw）](https://solution.minimaxi.com/voice-id-lucky-draw/)**，上传一段音频自动生成 6 个候选 voice_id 并试听对比，**未激活的候选不产生 9.9 元复刻费**。适合反复试音、批量比对的场景。

## 🔧 相对官方文档的改动

官方文档的代码示例有几处会让脚本直接跑不起来，本仓库都已修复：

| 问题 | 官方示例 | 修复 |
|---|---|---|
| Step 3 的 `requests.post` 用了错的 headers / payload 变量名 | `requests.post(url, headers=headers, json=payload)` | 改为 `requests.post(CLONE_URL, headers=clone_headers, json=clone_payload)` |
| 缺少 `response.raise_for_status()`，失败时无报错 | 直接 `print(response.text)` | 每一步都加 `raise_for_status()` |
| 完整示例中 file_id / prompt_file_id 都是“裸”变量，没有上传步骤的处理 | — | 抽成函数 `_upload_file()` 复用 |
| 试听音频 URL 在响应里但没自动下载 | — | 自动下载到 `audios/cloned_preview.mp3` |
| 参数写死路径 | `/path/to/...` 占位 | 支持命令行参数 + 环境变量 |

## 📚 后续步骤

拿到 `voice_id` 后，可以用它继续调用：

- [同步语音合成](https://platform.minimaxi.com/docs/guides/speech-synthesis-sync) — 单次最长 10,000 字符
- [异步长文本语音合成](https://platform.minimaxi.com/docs/guides/speech-synthesis-async) — 超长文本走异步任务

## 🛡️ 安全提醒

- `.env` 不要提交到 Git，仓库已建议在 `.gitignore` 里忽略。
- `voice_id` 跟你的 Group ID 绑定，避免硬编码到公开仓库。
- 待克隆音频如涉及他人声音，请先取得书面同意。