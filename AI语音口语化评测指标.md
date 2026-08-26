# AI 语音口语化评测指标（V1 框架）

> 目的：为「年年」等 AI 语音助手的**口语化/拟人化**建立可执行、可自动化、可回归的评测指标体系。
> 来源：行业标准（[YD/T 4394.2-2023](https://www.spc.org.cn/news/stdbzgg?id=258.html)、[T/CCSA 375.1/375.4](https://www.spc.org.cn/online/c8e32fd959559ad6f8d3eeefa29d1e36.html)、TTAF 203）、论文（Moshi、[Behavior-SD](https://github.com/yhytoto12/Behavior-SD)、[HumDial（ICASSP 2026）](https://ar5iv.labs.arxiv.org/html/2601.05564)、[TELEVAL（中文口语交互）](https://www.arxiv.org/pdf/2507.18061)、VoiceMOS/UTMOS、formality 度量）、量表（Godspeed / PA / 恐怖谷）、GitHub（UltraEval-Audio、RPBench-Auto、MaAI、ChatTTS）。

---

## 一、总体结构：四条评测线

级联管道（LLM 文本 → TTS → 交互）决定评测也分层，每层独立打分、单独回归：

```
L1 文本层口语化 —— LLM 输出文字本身（100% 可脚本化，每次改 prompt 全量回归）
L2 语音层口语化 —— TTS 读出后的韵律/语气词/停顿/音色（自动预测器 + 人听）
L3 交互层 —— 轮转/打断/回馈/记忆（录音统计）
L4 全局拟人感 —— 主人整体听感 + 人设（人工 1-5 听评为主，LLM-judge 抽查）
```

**评价方法三轨制**：
- **A 轨 · 客观自动指标**：脚本统计，成本≈0，适合每版回归；
- **B 轨 · 自动预测器/LLM-judge**：UTMOS、GPT-4o 判分，适合批量抽查；
- **C 轨 · 主观人工听评**：真人 1-5 打分，每周/每版本跑 20 条，是最终裁判（[Moshi 实测：客观指标与主观听感相关性差](https://ar5iv.labs.arxiv.org/html/2410.00037)，不可省）。

---

## 二、L1 文本层口语化指标（LLM 输出文字）

| # | 指标 | 定义 / 公式 | 目标口径 | 来源/对标 |
|---|---|---|---|---|
| T1 | 句长与句数 | 每句字数均值、≥15 字句占比、每条句数 | 均值 ≤12~15 字、≤3 句/条 | Moshi 训练口径 "Use short turns"；v3 清单 |
| T2 | 书面连接词率 | 黑名单词频（然而/因此/此外/首先/综上所述/其次/总之）按条统计 | =0（任务类可容忍 1 个"不过/但是"） | v3 清单；EACL [formality 词表法](https://preview.aclanthology.org/alta-23-ingestion/2021.eacl-main.174.pdf) |
| T3 | 非正式度评分 | 口语词/正式词占比（词表法，如 F-score） | 口语词占比 ≥80% | [Informality metrics（ACL W11）](https://aclanthology.org/W11-4523.pdf) |
| T4 | 语气词密度 | 句尾语气词（吧/呀/嘛/呗）+ 句中填充（嗯/啊）计数/条 | 每 2~3 句 ≥1 个；句尾优先 | v3 清单；JNLP [disfluency 更像人](https://www.jstage.jst.go.jp/article/jnlp/33/1/33_186/_article/-char/ja) |
| T5 | 口头禅一致性 | 固定口头禅（1~2 个）在跨轮中出现的稳定性 | 稳定，不每句都来 | Moshi 92 风格（个体惯性）；Character-LLM |
| T6 | 思考填充计数 | "这个嘛/嗯…让我想想" 次数/条 | 复杂场景 ≤1 次；简单场景 =0 | v3 清单；改造分析的思考占位 |
| T7 | 碎片数与收束 | 分段碎句数、是否有收束句 | 3~4 个碎片 + 收束 | Behavior-SD V 轴的可读化 |
| T8 | 首句回指率（chat） | 首句是否回应上一轮关键词 | chat 模式 ≥80% | v3 清单；Moshi 对话训练 |
| T9 | 接话听感 | 轻承接词（嗯/那/不过）使用；禁用"根据您的描述" | 按条人工/LLM 抽查 | 客服腔黑名单 |
| T10 | 回馈语出现率 | chat 情绪分享场景是否带"嗯嗯/真的吗" | 情绪场景 ≥50%；指令场景 =0 | Behavior-SD B 轴 |
| T11 | 不确定性缓和语 | "大概/好像是/我猜"频率 | 猜测场景有；事实场景 =0 | v3 清单补充条 |
| T12 | 人设词密度 | 喵/撒娇类词/条 | 闲聊 ≤1；任务 =0 | 恐怖谷（[过度拟人→瘆人](https://onlinelibrary.wiley.com/doi/10.1002/hfm.70046)） |
| T13 | 完整性检测（work） | 任务/事实回复是否含结论（答案存在性） | =100% | YD/T 4394.2 任务完成 |
| T14 | 数字写法 | 金额/时间是否阿拉伯数字 | =100% | 语音可听性常识 |

## 三、L2 语音层口语化指标（TTS 读出后）

| # | 指标 | 定义 / 公式 | 目标口径 | 来源/对标 |
|---|---|---|---|---|
| S1 | 自然度 MOS（人听） | ITU-T P.800 1-5 听评 | ≥4.0（参考） | [T/CCSA 375.1](https://www.spc.org.cn:443/online/f7cc990c60711effdb72413ccc3bffd6.html)、P.85 |
| S2 | 自动自然度预测 | [UTMOS/SpeechMOS/DNSMOS](https://katalog.lib.cas.cz/EdsRecord/edsarx,edsarx.2409.09305)（VoiceMOS Challenge 系） | 与人工相关性监控用 | [VoiceMOS Challenge](https://searchworks-lb.stanford.edu/articles/edsarx__edsarx.2409.09305) |
| S3 | 句间停顿分布 | 句间 Gap 均值/方差（录音统计） | 350~500ms（[Behavior-SD：0.4±0.2s](https://github.com/yhytoto12/Behavior-SD)） | Behavior-SD 时序统计 |
| S4 | 回馈/接话延迟 | 回馈音频相对停顿点延迟 | ≤0.2s（[Behavior-SD 统计](https://github.com/yhytoto12/Behavior-SD)） | B 轴 |
| S5 | 语气词/笑声渲染 | TTS 是否自然读出"嗯""哈哈"（人听） | 不僵硬、不吞字 | ChatTTS 社区；Affectron |
| S6 | 可懂度 WER | Whisper 转写 WER | 参考基线 ±2 | Behavior-SD（[注意：口语化特征会抬 WER](https://github.com/yhytoto12/Behavior-SD)） |
| S7 | 音色一致性 | WavLM speaker embedding 余弦 | ≥0.88（跨句） | [Moshi §6.3](https://ar5iv.labs.arxiv.org/html/2410.00037)、Behavior-SD |
| S8 | 伪影检测 | 熵谱法：复读/噪声/含糊/静默 | 复读=0，其他 <5% | Moshi Appendix D |
| S9 | 音质对比 MUSHRA | 锚定真人语音的 0-100 对比听评 | 参考 80+ | Moshi §5.2 MUSHRA 协议 |

## 四、L3 交互层指标（录音统计）

| # | 指标 | 定义 / 公式 | 目标口径 | 来源/对标 |
|---|---|---|---|---|
| X1 | 轮转四件套 | IPU / Pause / Gap / Overlap 统计 | 接近真人基线（[Moshi Table 9](https://ar5iv.labs.arxiv.org/html/2410.00037)） | [Moshi §5.6](https://ar5iv.labs.arxiv.org/html/2410.00037) |
| X2 | 响应延迟 | 首字延迟按难度分类 | 简单 <0.5s、复杂 1~2s（思考占位可接受） | Moshi 160ms 理论 / 人类 230ms |
| X3 | 打断正确性 | 被打断反应、打断后语义不串 | 回归通过 | T/CCSA 375.4 全双工；修复记录 |
| X4 | 回馈语触发（可选） | 停顿点触发率（[MaAI](https://github.com/MaAI-Kyoto/MaAI)/[phiresky](https://github.com/phiresky/backchannel-prediction) 参照） | 未实现=N/A | 回馈预测研究线 |
| X5 | 多轮记忆回指率 | 抛记忆事件后的回指率 | ≥1 次/会话 | HumDial 记忆维度 |

## 五、L4 全局拟人感指标（主观为主）

| # | 指标 | 定义 | 评分法 | 来源/对标 |
|---|---|---|---|---|
| H1 | 对话自然度 | 回馈语/笑声**适当**、转换无缝、流畅不尬 | 1-5，3 人 | [Behavior-SD MTurk 三维](https://github.com/yhytoto12/Behavior-SD) |
| H2 | 有意义性 | 内容可言、能听懂 | 1-5 | 同上 |
| H3 | 音质 | 清晰无噪 | 1-5 | 同上 |
| H4 | 拟人感量表 | Godspeed Anthropomorphism 子量表（机械↔拟人） | 5 点语义差分 | HRI 经典 |
| H5 | 感知拟人性 PA | 是否感到有意识/情感/意图 | 5 点量表 | Waytz et al. |
| H6 | 恐怖谷三轴 | Humanness / Eeriness / Attractiveness | 5 点 | [Uncanny Valley 语音助手研究](https://onlinelibrary.wiley.com/doi/10.1002/hfm.70046) |
| H7 | 情绪适配 | 文本情绪 ↔ 语音情绪 ↔ 用户情绪三方一致 | 匹配率 | Behavior-SD 情绪适当性 |
| H8 | 人设一致性 | 跨轮/跨场景角色统一 | 1-5 | CharacterEval / RPBench-Auto |

## 六、LLM-as-judge 轨道（批量抽查）

| # | 指标 | 做法 | 来源 |
|---|---|---|---|
| J1 | 叙事/指令一致性 | GPT-4o 按 1-5 判与任务要求的贴合 | [Behavior-SD 叙事 adherence](https://github.com/yhytoto12/Behavior-SD) |
| J2 | 角色一致性 IA | 是否"完全在角色内"（按角色规范生成） | [RPBench-Auto](https://github.com/boson-ai/RPBench-Auto) |
| J3 | 三维 LLM 判分 | 自然度/情感适当/风格贴合自动打分 | HumDial / TELEVAL 思路 |
| J4 | judge 偏见体检 | 用对抗样本测 LLM judge 是否偏袒/走捷径 | [Biased Judges（ACL 2026）](https://aclanthology.org/2026.acl-long.2006/) |

> ⚠️ LLM-judge 是"替代抽样"不是"代替人工"：批量看趋势，定版前必须 C 轨人工复审。

---

## 七、落地与汇总公式

**权重建议**（按你的目标——口语化优化，可调）：
```
口语化总分 = 0.35×L1 + 0.25×L2 + 0.20×L3 + 0.20×L4
```
- L1、L3、X 系：**每版 prompt 全量自动回归**（脚本化，≈秒级）；
- S2/S7/S8：**录音后批量跑**（UTMOS + WavLM + 熵谱脚本）；
- L4 人工：**每周 20 条**（复用 `语音测试集与评测指标.md` 的 20 条测试集），3 人 1-5；
- J 系：**每版 50 条抽查**；
- 改 prompt 的验收标准：目标维度 ≥+0.3，其余维度不下降（防此消彼长）。

**两个防误判红线**：
1. **口语化会抬 ASR WER**（[Behavior-SD 实测 3.55% vs 2.89%](https://github.com/yhytoto12/Behavior-SD)）——S6 只做"不劣化超过 2 个点"监控，不做口语化好坏的判据；
2. **客观指标与主观听感脱钩**（[Moshi 实测](https://ar5iv.labs.arxiv.org/html/2410.00037)：VisQOL/MOSNet 与 MUSHRA 不相关）——任何自动指标只用来抓回归，最终以 C 轨人听为准。

---

## 八、参考来源

- 行业标准：[YD/T 4394.2-2023 对话系统评估](https://www.spc.org.cn/news/stdbzgg?id=258.html)、[T/CCSA 375.1 中文语音合成](https://www.spc.org.cn:443/online/f7cc990c60711effdb72413ccc3bffd6.html)、[T/CCSA 375.4 全双工语音交互](https://www.spc.org.cn/online/c8e32fd959559ad6f8d3eeefa29d1e36.html)
- 论文与基准：[Moshi](https://ar5iv.labs.arxiv.org/html/2410.00037)、[Behavior-SD](https://github.com/yhytoto12/Behavior-SD)、[HumDial（ICASSP 2026）](https://ar5iv.labs.arxiv.org/html/2601.05564)、[HumDial-FDBench](https://github.com/ASLP-lab/HumDial-FDBench)、[TELEVAL 中文口语交互基准](https://www.arxiv.org/pdf/2507.18061)、[ViSpeak-Bench（信通院）](https://aihub.caict.ac.cn/datasets/AI-Ms/ViSpeak-Bench)、[VoiceMOS/UTMOS](https://searchworks-lb.stanford.edu/articles/edsarx__edsarx.2409.09305)、[Uni-VERSA 统一语音评估](https://ar5iv.labs.arxiv.org/html/2505.20741v1)
- 口语化文本度量：[Web 2.0 非正式度度量](https://aclanthology.org/W11-4523.pdf)、[formality 词表法（EACL）](https://preview.aclanthology.org/alta-23-ingestion/2021.eacl-main.174.pdf)、[填充词/语流不流畅测量（JSLP）](https://www.jbe-platform.com/content/journals/10.1075/jslp.25048.bel)、[What the Filler（INTERSPEECH 2025）](https://www.isca-archive.org/interspeech_2025/wepner25_interspeech.html)
- 拟人感量表：[Godspeed 系（博物馆机器人研究）](https://www.sciencedirect.com/science/article/pii/S0921889023002002)、[PA 感知拟人性](https://developer.aliyun.com/article/1336080)、[语音助手恐怖谷](https://onlinelibrary.wiley.com/doi/10.1002/hfm.70046)
- 工程与评测工具：[UltraEval-Audio](https://github.com/OpenBMB/UltraEval-Audio)、[RPBench-Auto](https://github.com/boson-ai/RPBench-Auto)、[MaAI（回馈/轮转预测）](https://github.com/MaAI-Kyoto/MaAI)、[TELEVAL 仓库](https://github.com/Tele-AI/TELEVAL)