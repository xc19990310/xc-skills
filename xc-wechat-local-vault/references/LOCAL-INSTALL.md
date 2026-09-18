# 本地修订与安装记录

日期：2026-09-17。上游来源：`mcncarl/yichen-skills` 的历史目录 `yichen-wechat-local-vault`；本仓库发布目录为 `xc-wechat-local-vault`。
固定源提交：`fa0b5471dd868421212b1e7fe643b3f15b8a2aa4`。
本地版本：`2026.09.17-local.2`。这是个人本地修订，不是上游已发布版本。

## 安装位置与命令

- 技能：`~/.codex/skills/xc-wechat-local-vault/`
- 统一入口：`~/.local/bin/wechat-vault`
- 独立 Python：`~/Library/Application Support/wechat-local-vault/runtime/`
- 密钥：`~/.config/wechat-keys.json`（首次抓钥后才创建）
- 配置：`~/.config/wechat-local-vault.json`（首次配置后才创建）
- 密钥捕获日志：`~/Library/Application Support/wechat-local-vault/private/capture.jsonl`
- 私有微信副本：`~/Library/Application Support/wechat-local-vault/app/WeChat.app`
- 明文：`~/Library/Application Support/wechat-local-vault/decrypted/current/`
- 默认报告与素材：`~/Library/Application Support/wechat-local-vault/exports/`
- 刷新清单：私有根目录 `manifests/`；当前状态：明文目录 `refresh_status.json`。

```bash
~/.local/bin/wechat-vault doctor
~/.local/bin/wechat-vault status --format json
~/.local/bin/wechat-vault extract --list-dbs
~/.local/bin/wechat-vault extract --match-only --targets all --reuse-log
~/.local/bin/wechat-vault extract --targets all --duration 240
~/.local/bin/wechat-vault refresh --mode incremental
~/.local/bin/wechat-vault contacts --query '群名' --format text
~/.local/bin/wechat-vault history '群名' --start-time '2026-09-16' --end-time '2026-09-17' --format text
~/.local/bin/wechat-vault digest-source '群名' --start '2026-09-16' --end '2026-09-17'
```

`doctor` 仅检查软件、依赖和配置文件是否存在，不读取账号内容。
安装流程不运行上述首次抓钥命令、不启动微信、不读取真实聊天。安装完成与账号已初始化是两个状态。
首次捕获可能需要用户在私有副本中登录或打开对应页面；程序不能自行保证这些交互成功。禁止自动关闭 SIP、修改原应用签名或杀死正在运行的微信。

## 修复内容

1. 取消脚本自动全局 pip 安装，使用独立运行时和版本锁定文件。
2. 捕获日志从 `/tmp` 移入私有目录，在写入前限制权限；配置/密钥采用原子写入，文件 0600；敏感目录 0700。默认日志隐藏账号路径。
3. 拒绝给原微信重签名；私有副本移出桌面。多账号不自动选第一个，不允许覆盖全局配置切换账号。
4. 增量指纹同时覆盖主库、WAL 和输出库；校验 WAL 头与活跃帧校验和、salt。SQLCipher 在副本上恢复已提交事务并 checkpoint，执行 SQLite 完整性与 HMAC 检查，再按物理页解密。
5. 避免 `sqlcipher_export` 重排隐藏 rowid，保留 Name2Id 发言人映射、原始 rowid 和数据库元数据。
6. 新明文先写私有临时文件并校验，再原子替换；错钥、损坏、变化或超时均不删除旧库。失败返回非零，缺钥和孤立保留库有独立状态。
7. Mac 查询只读打开，不创建缺失数据库；查询提示快照时点。导出使用私有原子写入，禁止符号链接、硬链接和数据库/配置目录覆盖。群聊素材默认离开项目目录。
8. 旧辅助脚本的解密函数也路由到同一安全引擎，但日常调用统一入口。

## 验证与已知边界

测试使用公开合成密钥和 SQLCipher 创建的隔离数据库，覆盖：WAL 独有提交、未提交事务、错钥、WAL 正文及 salt 损坏、复制期间变化、旧库保留、增量判断、缺钥、孤立库、隐藏 rowid、只读查询、权限、符号链接导出、多账号选择与原应用签名保护。上游 Windows 快照测试一并运行。

最终数量及结果见根目录 `VERIFICATION.md`。没有读取真实账号库，没有完成微信 4.1.7 的真实密钥捕获或真实 schema 兼容性验证。

- 微信 WCDB 自定义 tokenizer / schema、加密布局或 Frida 接口差异可能导致失败；保留旧库并报告，不降级成未经校验的数据。
- 活跃微信持续写入时可能需要重试；WAL 中回收周期的旧尾部也保守拒绝，正常退出微信再刷新通常可避免。程序不修改源库、不强制 checkpoint 源库。
- 多个库逐库生成快照，不承诺跨库事务一致性。`complete=true` 是最近一次枚举结果，不等于当前实时，也不覆盖未下载到电脑的消息。
- 仅一个明确配置的 Mac 账号。完整多账号工作区功能未实现，禁止将文档描述当成已实现支持。
- `--clean` 和 `--no-manifest` 禁用，避免破坏旧快照或隐藏新鲜度。
- 未接入金融台、Obsidian 或任何后台定时采集；后续若接入金融台持续数据源，按项目规则登记 Scheduler Registry 和证据时点。

## 重建依赖

在 macOS 安装 SQLCipher（本机已装 4.19.0，依赖 openssl@4 4.0.2）：

```bash
brew install sqlcipher
python3 -m venv "$HOME/Library/Application Support/wechat-local-vault/runtime"
"$HOME/Library/Application Support/wechat-local-vault/runtime/bin/python" -m pip install -r requirements-local.lock
```

`LOCAL-PROVENANCE.json` 保存上游提交、文件哈希和安装状态；`local-fixes.patch` 保存相对固定上游的修改。更新上游前保留本地补丁并重新验证，不覆盖个人修复。


## local.2 实机验证更新

已完成微信 4.1.7 三个基础库的真实抓钥、解密与指定会话近期文本读取。先前“未执行/未验证”条目描述安装时状态；其他媒体、朋友圈、收藏及全量账号完整性仍未验证。此更新不包含聊天内容或身份信息。空 WAL 预分配尾部新增受校验的 SHM 例外，二进制联系人 TEXT 元数据单独容错；细节见架构 local.2 记录。保护规则不变，活跃损坏日志仍拒绝。
