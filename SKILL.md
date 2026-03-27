---
name: x-fetch
description: 获取 X (Twitter) 指定用户的推文。当用户提到推文、Twitter、X、tweet，或提到 elonmusk/teslascope/teslaownersSV/ChrisZheng001/WholeMarsBlog/raines1220/Tsla99T/Tesla_Cybercat 等用户名时，直接执行命令获取推文，不需要额外解释。
triggers:
  - "推文"
  - "Twitter"
  - "tweet"
  - "elonmusk"
  - "teslascope"
  - "teslaownersSV"
  - "ChrisZheng001"
  - "WholeMarsBlog"
  - "raines1220"
  - "Tsla99T"
  - "Tesla_Cybercat"
---

# X 推文获取

**激活即执行，不要解释流程，直接运行命令输出结果。**

## 执行规则

1. 用户说「XX 的推文」→ 直接运行命令，输出结果
2. 没有指定时间 → 不加 `--since`
3. 说「最近一天/今天」→ 加 `--since 1d`
4. 说「最近一周/这周」→ 加 `--since 1w`
5. 说「最近一个月」→ 加 `--since 1m`
6. 说「最新 N 条」→ 加 `--count N`

## 命令

```bash
python3 /Users/jeremy/.catpaw/skills/skills-market/x-fetch/scripts/x_fetch.py <username> [--since 1d|1w|1m] [--count N]
```

## 已追踪用户（直接可用）

elonmusk / teslascope / teslaownersSV / ChrisZheng001 / WholeMarsBlog / raines1220 / Tsla99T / Tesla_Cybercat

查看所有用户及数据状态：
```bash
python3 /Users/jeremy/.catpaw/skills/skills-market/x-fetch/scripts/x_fetch.py --list
```

## 数据说明

- 数据每 3 小时自动更新，每次命令直接从 GitHub 读取最新缓存
- 每个用户最多保留 20 条最新推文
- 数据仓库：https://github.com/qimiguang/x-fetch
