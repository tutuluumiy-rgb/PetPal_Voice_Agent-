# 《Moshi》《Behavior-SD》深挖总结报告

> 深挖对象：
> 1. **Moshi: a speech-text foundation model for real-time dialogue**（Kyutai，arXiv:2410.00037，2024-10，开源）
> 2. **Behavior-SD: Behaviorally Aware Spoken Dialogue Generation with Large Language Models**（首尔大学，NAACL 2025 Long, pages 9574–9593）
>
> 目的：把两篇论文里跟「年年」语音宠物（拟人化、口语化、prompt 调优、评测体系）直接相关的机制、数据、评测方法挖出来，并给出落地转化建议。
> 提取文本见 `_pdf_extract/Moshi.txt`、`_pdf_extract/Behavior-SD.txt`。

---

## 一、两份论文速览

| | Moshi | Behavior-SD |
|---|---|---|
| 一句话 | 第一个**实时全双工**语音↔语音对话大模型（每时每刻都在听、都能说） | 第一个带**会话行为标签**的 10 万级口语对话数据集 + 行为可控的对话模型 BeDLM |
| 核心资产 | Helium(7B 文本 LLM) + Mimi(神经音频编解码) + 多流 RQ-Transformer + Inner Monologue | Behavior-SD 数据集（108,174 段对话 / 2,164 小时）+ BeDLM（Llama3.2-1B） |
| 与我们的关系 | 对照"年年"管道的天花板形态；提供**对话轮转的客观评估方法**（§5.6） | 提供**口语化/拟人化的可调节行为轴**（V/F/B/I）与**现成的评测模板**（三维人工评分 + 行为 adherence） |

---

## 二、Moshi 深挖

### 2.1 它解决了级联管道的三个"不像人"的问题

论文开篇就批评传统管道（VAD→ASR→文本LLM→TTS）——**这正是「年年」现在的形态**，三个原生缺陷：

1. **延迟**：多组件叠加，典型总延迟数秒；而人类对话平均响应只有 ~230ms（跨 10 种语言统计，Stivers et al. 2009）；
2. **文本瓶颈**：一切经由文字，情绪、口音、副语言、环境音全丢；
3. **轮次模型**：假设对话是"你一句我一句"；实际上真实对话**重叠说话占 10–20% 的时间**（Çetin & Shriberg 2006），还有不打断的回馈语（backchannel："嗯""OK"）和打断。

**Moshi 的方案**：把口语对话建模成 speech-to-speech 生成——两个并行音频流（用户流 + 系统流）+ 无显式轮次边界，理论延迟 160ms、实测 200ms，全双工。

### 2.2 架构三板斧（我们不用复刻，但每招都有启发）

1. **Mimi 编解码器**：12.5Hz 帧率、因果、1.1kbps；用 split-RVQ 把语义信息蒸馏进第一层 token。关键实验结论：**只用对抗训练（去掉重建损失）主观听感反而大提升**（MUSHRA 81.0 vs 58.8）——客观指标和主观听感会脱钩。
2. **RQ-Transformer 多流建模**：Temporal(≈Helium) + Depth(6层小 Transformer) 分层预测；acoustic delay 压缩到 240ms；语义 token 损失权重 x100。
3. **Inner Monologue（内心独白）**：在音频 token 前按帧对齐预测**文本 token**（用 Whisper 时间戳对齐，PAD/EPAD 填充）。效果惊人：
   - 生成语言质量大幅提升（转写 NLL 2.77 vs 3.65，长度 1920 vs 602 字符）；
   - 口语问答准确率**几乎翻三倍**（WebQ 26.6 vs 无 IM 9.2）；
   - 只调 text/audio 之间的延迟参数，同一个模型就能当**流式 ASR（WER 5.7%）**或**流式 TTS（WER 4.7%）**用。

> **对我们的启示**：Inner Monologue 证明"文本先行 + 音频跟随"能显著提升口语质量与可控性——「年年」本来就是"文本 LLM → 流式 TTS 逐句"，这条路线是对的；反过来说，纯端到端（不做文本中间态）反而会丢语言质量（论文里 Spectron/SpeechGPT 的 Chain-of-Modality 必须整段文本生成完才能开口，不兼容实时）。

### 2.3 训练数据：口语化是"喂"出来的，且 prompt 里直接写

这是全篇对 prompt 调优最有借鉴价值的部分（§4.3）：

- 用 LLM 生成 2 万+ 小时**合成对话**做指令微调，生成 prompt 里**明晃晃写着**：
  - `Use some backchanneling. Use short turns.`（用一些回馈语，用短句）
  - `Use a lot of backchanneling.`（多用回馈语）
  - 角色扮演场景：`Write a dialogue between Blake and Moshi, {{situation}}. Use a lot of backchanneling.`（"一个如释重负的宇航员""一个讨厌闲聊的侦探"等 92 种说话风格，见 Table 19）
- **鲁棒性数据增强**（直接对口语音场景）：用户流随机增益 -24~+15dB；30% 加噪；模拟回声（年年自己的声音缩放到 0~0.2 倍、延迟 100~500ms 混回麦克风流）；30% 加混响；训练含拼写错误/发音错误的输入，让模型学会"没听清→主动请对方重复/澄清"。
- 系统声音一致性：全程用同一个配音演员的声音微调，就足以保证推理时不串声。

### 2.4 评估方法论（§5.6 我们可直接抄）

Moshi 用**离线生成双人对话**来评估对话质量（多流模型可以自己扮演用户那侧）：
- **DialoGPT 困惑度**：转写后测语义连贯性；
- **轮转统计四件套**（"像不像人对话"的客观指标）：
  - **IPU**（Inter-Pausal Unit）：连续语段，两侧静音 ≥0.2s；
  - **Pause**：同一说话人语段之间的静音（内部停顿）；
  - **Gap**：不同说话人之间的静音（接话间隙）；
  - **Overlap**：双方同时说话的时长（重叠/插话）。
  - 论文里 Moshi 在 temp=1.0 时这些统计最接近 Ground Truth（Pause 7.0s vs GT 6.4s、Gap 4.5s vs 4.2s、Overlap 4.1s vs 3.3s）。
- **说话人一致性**（§6.3）：WavLM speaker embedding 余弦相似度，98.7% 情况下系统声音更接近自身参照段而非用户，且随时间不漂移。
- **客观音质**：MOSNet + 自研"熵谱"伪影检测（gibberish/噪声/背景噪声/重复文本四类伪影，用 token 熵的斜率/阈值判定——**重复文本 = 文本熵平坦**，可直接用来检测 LLM 复读）。
- **关键教训**：VisQOL/MOSNet 与主观听感（MUSHRA）相关性差，**"客观分高不代表听着好"**——所以他们的音质结论以人类评测为准。对年年：自动指标只能做回归监控，口语化最终要人听。

### 2.5 安全（简记）

毒性（ALERT 83.05，中游）、训练数据 regurgitation（去重+微调后为 0）、语音水印（负结果：codec 非幂等导致水印被编解码抹掉）。

---

## 三、Behavior-SD 深挖

### 3.1 核心贡献：四个行为轴 × 三档控制

把"口语化/拟人化"从玄学拆成**可调节的行为旋钮**（Table 1）：

| 行为 | 定义 | 三档（0/1/2） |
|---|---|---|
| **V Verbosity 冗长度** | 一轮话的长短 | 0=极简只说干货 / 1=适中 / 2=长篇详细 |
| **F Filler words 填充词** | "嗯、那个、你懂的"这类 | 0=绝不用 / 1=适度 / 2=频繁 |
| **B Backchannels 回馈语** | 对方说话时"嗯哼、是吗" | 0=无 / 1=少 / 2=多 |
| **I Interruptions 打断** | 插话抢话 | 0=绝不 / 1=适度 / 2=频繁 |

打断还细分 **7 类**（Goldberg 1990）：同意、反对、抢话、离题、要求澄清、帮忙补充、换话题。

### 3.2 数据怎么造（LLM 流水线，我们可复刻到 chirp 的 prompt 里）

1. 从 SODA 采样叙事 + 随机给两个说话人配行为档位（如 Hugo: V1 F2 I0 B0 / Keaton: V0 F0 I2 B1）；
2. GPT-4o 按档位生成 8–12 轮口语对话，用 `[laughter]`、`<laughter>yeah</laughter>` 标注笑，用 `(interrupt)`/`[interrupted]` 标注打断；
3. **BOPs 检测**：让 LLM 把句子在自然停顿处切分，按回馈档位选 0–30%（B1）或 30–60%（B2）的切点插入 `[MASK]`，再让 LLM 填**语境合适的回馈语**；回馈词汇库：yeah / uh-huh / hmm / mhm / okay / wow / oh / cool / really / great / nice / interesting / right，双词级：yeah yeah、oh really?、that's great；
4. 每句标注**语音风格**：pitch（low/normal/high）+ speed（slow/normal/fast）+ emotion（neutral/happy/sad/angry/fearful）——**这就是 TTS 指令的三维格式**；
5. CosyVoice-Instruct 合成，**时序按真人对话统计放置**：正常句间间隔 N(0.4s, 0.2s)、回馈语紧跟 N(0.2s, 0.02s)、打断与前句重叠 N(0.45s, 0.05s)；
6. 有声回馈语（"hmm""mhm"）用 ElevenLabs 声音克隆增强，避免 TTS 念不好。

### 3.3 评测方法（§4/§7——我们打分表可直接对齐这套口径）

- **人工三维评分**（MTurk，30 秒音频片段，1–5 李克特，3 人/段）：
  - *Dialogue Naturalness 对话自然度*：回馈语和笑是否合适、说听转换是否无缝、是否流畅不尬；
  - *Meaningfulness 有意义性*：内容是否有意义、能否听懂；
  - *Sound Quality 音质*：是否清晰无噪。
  - Behavior-SD 得分 3.94/3.78/3.87，压过 CANDOR/MELD/DailyTalk/StyleTalk 所有对比数据集。
- **行为 adherence（服从度）**：模型生成 vs 标注档位之间算 **Wasserstein 分布距离**（越低越听话）；叙事一致性用 GPT-4o 按 1–5 打分。
- **说话人一致性**：WavLM-Base+ 余弦相似度，`(s0, si)` 0.885–0.889。
- **TTS 可懂度**：Whisper-Large V3 测 WER=3.55%；注意：**插入填充词/回馈语会让 WER 变高**（口语化特征天然"更不规整"，评测时别误判成烂）。

### 3.4 结论

BeDLM（Llama3.2-1B + HuBERT 单元 + ⟨A⟩⟨B⟩⟨GAP⟩⟨OVERLAP⟩⟨BC_S⟩⟨BC_E⟩ 控制 token）在自然度 4.09/有意义性 4.04/音质 4.05（Ground Truth 4.14/4.02/4.12），行为服从全面优于级联 GPT-4o/TTS 管线。局限：复杂情绪控制有限、除笑声外非词汇发声有限、偶发发音错误。

---

## 四、对「年年」的落地转化（重点）

### 4.1 Prompt 层：把口语化变成四根可调旋钮

Behavior-SD 的 V/F/B/I × 3 档，正好对应你要的"口语化优化"——给年年的 system prompt 加一段**行为档位（默认推荐）**：

```text
【行为档位】（按当前人设与场景动态调整）
- 冗长度：默认 1（回答 1~3 句，点到为止）；撒娇时 2，被问正经事时 1
- 填充词：默认 1（适度"嗯/那个/嘛"）；思考时 2，但别每句都带
- 回馈语：默认 2（主人说话停顿处给"嗯嗯/真的吗/哇"）；播报任务时 0
- 打断：默认 0（不插话）；主人叫名字/连续两次没回应时才主动开口
```

> 直接抄 Behavior-SD 的档位文案：V0="说得很简短，只说必要的"、V2="尽量详细展开"；F0="绝不用填充词"、F2="频繁使用填充词"（Table 6）。调 prompt 时只动这四个词，就是"口语化旋钮"。

### 4.2 停顿/接话时序：用真实语料统计替换拍脑袋参数

你的 `拟人化改造分析.md` 里句间 sleep 150–500ms 随机。Behavior-SD 用真人对话统计（Reece et al. CANDOR）标定：

| 场景 | 真实数据 | 对年年的建议 |
|---|---|---|
| 正常句间 | N(0.4s, 0.2s) | 350~500ms（句子短可偏短） |
| 回馈语/接话 | N(0.2s, 0.02s) | **要快，≤250ms**（慢半拍就不像"嗯嗯"了） |
| 打断/插话 | 重叠 N(0.45s, 0.05s) | 打断反应（"嗯？"）要"叠"在主人话音上，而不是等说完 |
| 人类平均响应 | ~230ms（Moshi 引 Stivers 2009） | 年年可以有思考延迟，但 ≤1~1.5s，超了就播思考占位 |

### 4.3 评测层：升级你的四维打分表（外部标准对口版）

| 现有维度 | 升级点 | 对标 |
|---|---|---|
| A3 语气词/停顿 | 新增**客观统计**：回馈语/填充词发生率（"嗯/那/呗/喵"计数）、句间 Gap 统计、IPU 统计 | Behavior-SD 行为统计 + Moshi §5.6 轮转四件套 |
| A4 情绪适配 | 收窄成三维：pitch(低/中/高) + speed(慢/中/快) + emotion，让 `voice_style.py` 的指令可量化 | Behavior-SD style captioning |
| A5 人设一致 | 新增语音一致性检查：WavLM 说话人嵌入余弦相似度（年年音色漂移检测） | Moshi §6.3 / Behavior-SD §7.3 |
| B 任务完成 | 新增**行为 adherence**：生成 vs 目标档位的分布距离（Wasserstein） | Behavior-SD §7.2 |
| C/D | 新增**伪影回归**：用熵谱法检测 gibberish/噪声/**复读**（文本熵平坦=复读，专治 LLM 车轱辘话） | Moshi Appendix D |
| 主观 | 对齐三维评分口径：自然度/有意义性/音质（1–5） | Behavior-SD MTurk |

**自动化回归建议**：跑完测试集 → 录音 → Whisper 转写 → ①LLM-as-judge 按三维打分；②统计轮转四件套与行为词频；③对比基线盈亏。三个指标一起看，避免"口语化变好、任务漏项"的此消彼长。

### 4.4 可直接引用的几个"口语化弹药库"

- **回馈语库**（Behavior-SD §3.2）：单字级 yeah/uh-huh/hmm/mhm/okay/wow/oh/cool/really/great/right → 年年版：**嗯嗯/真的吗/哇哦/原来如此/然后呢/喵**；
- **凑热闹的搭话**：`<laughter>yeah</laughter>` 这种"边说边笑"标注 → 年年可用 `<笑>真的假的</笑>` 式的标注（如果 TTS 支持）；
- **92 种说话风格清单**（Moshi Table 19）：agreeing/amazed/annoyed/anxious/appreciative/sincere/skeptical/slow/surprised/sympathetic/whispering + 角色（1920s gangster/confident CEO/robot/sarcastic comedian…）——比 `voice_style.py` 的 7 种情绪细得多，可挑几个往 EMOTION_INSTRUCTIONS 里扩（如 sarcastic 傲娇、whispering 犯困）。
- **Moshi 的合成对话 prompt 原文**：`Write the transcript of a conversation between Blake and Moshi. {{summary}} Moshi is knowledgeable about the topic. Use some backchanneling. Use short turns.` ——这就是"口语化 prompt"的成熟写法，可直接套成 `用一些回馈语。用短句。`。

---

## 五、关键结论

1. **"口语化"在学界已经被拆成了可测量的行为旋钮**（V/F/B/I × 3 档 + 7 类打断），不再是主观玄学——年年的人设可以直接映射成一组"默认档位"，prompt 调优就是对档位的调优。
2. **停顿/接话时序有真人语料统计可抄**（句间 0.4s、回馈 0.2s、重叠 0.45s），把 `拟人化改造分析.md` 的 150–500ms 拍脑袋参数升级成有依据的参数。
3. **"文本先行"路线是对的**：Moshi 的 Inner Monologue 证明了文本中间态显著提升口语质量与可控性，年年"文本 LLM → 流式 TTS"的结构有学术背书；层级管线不是缺陷，纯端到端反而更差。
4. **评估要主客观并重**：Moshi 实测客观指标（VisQOL/MOSNet）与人类听感相关性差；Behavior-SD 给出了现成的三维人工评分口径 + 行为 adherence 客观指标。年年最终应该以"人听"为准，自动指标做回归监控。
5. **附带发现**：插入填充词/回馈语会抬高 ASR 的 WER（Behavior-SD 3.55% vs 2.89%）——做口语化评测打分时，别把"ASR 转写变差"误判为"回答变差"。

---

## 六、下一步可选动作

- [ ] 把 4.1 的"四根旋钮"写进 `personality.py`/system prompt，做一版 A/B（默认档位 vs 无档位）；
- [ ] 在回归脚本里加"轮转统计 + 行为词频 + 复读检测"三个自动化指标；
- [ ] 把 4.2 的时序参数套进 `main.py` 的句间 sleep，回放对比听感；
- [ ] 按 Behavior-SD 口径把 `评测打分表模板.csv` 的主观列升级为"自然度/有意义性/音质"三列。