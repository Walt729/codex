# 六小时自主研究工作区

此仓库 `Walt729/codex` 用于存放研究框架与研究过程中产生的题目、证据、代码、审查和报告。

- `research-framework/`：Python 调度程序、研究规程和测试。
- `research-runs/<任务名>/`：每个题目的完整研究档案。

框架使用现有 Codex/ChatGPT 登录，不使用单独计费 API 或付费研究资源。默认总预算六小时，每小时独立审查，最后十分钟终审。没有题目时不会启动研究。

## 两种不同的保存

**提交并推送到 GitHub**：把这里的文件变成 Git 提交，再通过 `git push` 上传到本仓库。只放在云机器里或只做本地 commit，都不等于 GitHub 已经备份。

**发布云环境**：平台保存已准备好的开发机器配置与文件快照，供后续任务使用。它不会代替 Git 提交和推送，也不是发布一个公开网站。云环境的“仅限自己”描述环境使用范围，不改变 GitHub 仓库的可见性。

两者是独立操作。GitHub 认证通过平台已有的 HTTPS Git 代理提供，不要求把 GitHub 密钥填进环境变量或 HTTP 密钥字段。GitHub 仓库访问与云端 Codex CLI 的 ChatGPT 登录也是两套授权。

## 使用

先读取 [框架说明](research-framework/README.md) 和 [研究规程](research-framework/SKILL.md)。题目和任务目录都放在此仓库内。启用 `--sync-git` 的任务会在研究阶段之间、停止或终审时同步任务档案到本仓库；同步失败会保留本地材料并停止继续调用模型，不进行强制推送。

```bash
cd /workspace/codex
python3 research-framework/runner.py init --task-dir research-runs/problem-001 --topic-file topic.md --sync-git
python3 research-framework/runner.py run research-runs/problem-001
```

凭证、缓存和进程锁不进入 Git。GitHub 单文件大小限制等阻碍会明确报告，不能将上传失败说成已备份。普通后台进程不能保证云平台保留机器六小时；发布环境也不代表研究已开始。
