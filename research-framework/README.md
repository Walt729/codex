# 云端六小时研究框架

这是一个小型、仅依赖 Python 标准库的调度框架。技能负责研究方法，Python 程序负责计时和反复调用，任务文件负责保留跨上下文的记忆。没有研究题目时不会自行开始研究。

## 当前验证状态

- 云环境有 Python 3.12 和 Codex CLI 0.163.0，CLI 显示已通过 ChatGPT 登录。
- 调度器使用现有 `codex exec` 登录，不需要也不会回退到单独计费的 API key。登录状态本身不证明剩余额度或模型调用成功。
- 初次模型检查曾被 `chatgpt.com` 域名策略拒绝（CONNECT 403）。域名加入后重测，请求返回了更具体的登录错误：`Your access token could not be refreshed ... Please sign in again.`。当前真实模型调用受云端 Codex CLI 登录失效阻碍，需通过受支持的登录方式重新授权；这与 GitHub 权限无关。
- 当前命令沙箱中的 Codex 状态目录只读。CLI 必须在有正常状态目录写权限的云端启动上下文运行；不得通过复制凭证、关闭校验或危险绕过参数解决。
- 普通后台进程不能保证云机器六小时不被暂停或回收，也不能在实例被销毁后自行复活。平台存活策略仍需确认；本项目没有已验证的平台级定时任务或自动重启服务。

因此不能把本项目描述为“六小时真实模型端到端运行已经验证”。测试使用假 Codex 进程验证调度，不代表完成过实际研究。

## 运行方式

工作目录为 `/workspace/codex/research-framework`；研究数据建议放在独立的 `/workspace/codex/research-runs/<任务名>`。沿用现有独立云任务和检出目录，不创建 Git worktree。框架与研究档案均放在 `Walt729/codex` 的工作副本内。

先检查环境和测试：

```bash
cd /workspace/codex/research-framework
python3 runner.py --help
codex --version
codex login status
python3 -m unittest discover -s tests -v
```

把完整题目、定义、目标和已知条件保存为 UTF-8 文本，例如 `/workspace/codex/topic.md`。创建任务不调用模型、不启动六小时时钟：

```bash
python3 runner.py init --task-dir /workspace/codex/research-runs/problem-001 --topic-file /workspace/codex/topic.md --sync-git
```

默认总预算为 21600 秒，每 3600 秒复盘，最后 600 秒终审，探索最多以 600 秒为一步。各步骤提前完成时会继续下一项实质研究，不等待凑时长。若探索者声称提前完成，先进行独立核验；核验否定完成状态时，把意见送回探索步骤，继续推进。需要固定型号时在 `init` 添加 `--model <当前账户可用的型号>`，探索、审查、终审使用同一个设置；不指定时使用 CLI 默认配置，不声称其型号必然与本聊天相同。

真实模型的登录、网络和运行目录权限正常，且 GitHub 推送已验证后再启动：

```bash
python3 runner.py run /workspace/codex/research-runs/problem-001
```

如果云端启动入口允许后台进程，可在该入口运行：

```bash
nohup python3 /workspace/codex/research-framework/runner.py run /workspace/codex/research-runs/problem-001 > /workspace/codex/research-runs/problem-001/supervisor.log 2>&1 < /dev/null &
```

`nohup` 仅处理终端断开；不保证平台保留实例，不保证认证持续有效，不代表已安装外部调度服务。看到 shell 返回或 PID 也不等于研究成功，应检查阶段记录和证据。

查看、停止和恢复：

```bash
python3 runner.py status /workspace/codex/research-runs/problem-001
python3 runner.py stop /workspace/codex/research-runs/problem-001
python3 runner.py resume /workspace/codex/research-runs/problem-001
```

若只需要重试上传已保存的材料，可运行 `python3 runner.py sync /workspace/codex/research-runs/problem-001`。它不调用模型，不重置研究预算，也不恢复研究进程；上传完成不等于之前的研究阻碍已消失。

`run` 首次启动时记录绝对截止时间，包含研究、审查、中断和停止时间。`resume` 不重置预算，超过截止时间不能继续冒充原六小时任务。需要新的研究预算应创建新任务，显式引用已有材料。`--max-steps N` 可在有限次调用后留下检查点，但后续预算继续流逝。

## 研究与审查

每次 CLI 调用都用新上下文。探索者读取原题、证据和此前审查，保存增量结果。到小时边界触发只读审查：审查者独立读取原始题目、历史审查及实际证据，给出可核查的纠错和方向建议。下一探索步骤须对建议作接受、拒绝或待核验的有依据决定。

终审也使用只读角色。`final.md` 只在实际获得有效终审输出后生成。运行结束和题目解决是两件事：状态 `finished` 表示终审已交付，`outcome` 仍可能是 `continue`（未解决）或 `blocked`。超时、失效输出、额度耗尽和权限问题不会被记作通过。

任务目录包括：

| 路径 | 内容 |
| --- | --- |
| `original.md` | 原始题目；只读标记和哈希用于检测意外改动，不是针对恶意程序的安全隔离 |
| `state.json` | 截止时间、当前状态、调用记录和最近审查 |
| `notes/` | 检查点、路线变更及对审查意见的决定 |
| `evidence/` | 原始推导、来源定位、实验代码和结果 |
| `reviews/` | 已完成的独立阶段审查 |
| `calls/` | 每次调用的提示词、原始输出、错误、退出状态和结构化答复 |
| `checkpoint.md` | 未完成终审时的恢复说明 |
| `final.md` | 成功完成终审后的最终报告 |

任务档案是真正的持久化记忆。全局记忆只适合存一般偏好，不能承担计时、保存完整推导或唤醒进程的职责。详细角色规程见 [SKILL.md](SKILL.md)。

## 无人值守边界

不使用付费论文、收费 API、租用算力或其他付费资源；遇到付费墙使用合法免费替代，找不到则记录缺口并继续独立工作。允许调度器将研究档案同步到用户指定的 `Walt729/codex` 仓库，不发布到其他目的地。不向用户发问等待回复，不交互登录，不读取或复制凭证。现有订阅额度不足时保存状态停止，不切换付费接口。

可用工具仍取决于平台能力、权限和网络策略。添加 `chatgpt.com` 仅用于模型入口，并不代表所有公开论文站点都已放行。未放行来源不能绕过代理访问；应在任务启动前按实际题目配置所需域名，或在研究中记录访问限制并使用可用替代。

平台侧须在环境设置中检查并保存已准备的草稿，然后发布环境，才可能将配置用于后续任务。发布、启动技能保存、当前进程运行、六小时实例存活和新任务恢复是不同的状态，必须分别验证。

## GitHub 阶段备份

启用 `init --sync-git` 时，调度器在第一次模型调用前、各阶段之间和结束时对该任务目录执行正常的 Git 提交与推送。每次推送到现有分支，不强制推送、不重置历史、不自动合并。运行日志、原始题目、证据、审查和状态文件均属于研究档案。缓存、进程锁和凭证不入库。

检测到其他暂存改动、敏感文件、符号链接、GitHub 单文件大小限制或推送失败时，保留本地成果并停止继续调用模型，明确报告备份未完成。没有新提交时仍尝试推送，以便恢复之前未上传的提交。默认最后十分钟包含终审及其最后一次备份的时间。初始化本身不调用模型，也不推送；首次 `run` 会先检验备份链路。

`--sync-git` 关闭时只保存云机器本地文件，不能声称 GitHub 已备份。不要将个人认证目录、访问令牌或密钥复制到研究目录。
