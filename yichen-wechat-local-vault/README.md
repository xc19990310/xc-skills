# yichen-wechat-local-vault

> 本机修订版 `2026.09.17-local.2`；入口 `~/.local/bin/wechat-vault`。先运行 `doctor`，安装不代表真实账号已解密。修复、依赖与未验证边界见 [本地安装记录](references/LOCAL-INSTALL.md)。

微信本地数字资产库：支持 Mac 4.x 全量/增量解析，并整合实验性的 Windows 离线明文快照查询。按指令选择全量解密、增量刷新、统一查询、指定联系人/群聊导出、朋友圈/收藏夹解析、群聊精华素材包和关系复盘。

## 从 GitHub 安装到 ChatGPT/Codex

在支持本地工具和技能安装的 ChatGPT/Codex 环境中，直接发送：

```text
请从 https://github.com/xc19990310/yichen-skills/tree/main/yichen-wechat-local-vault 安装 yichen-wechat-local-vault；安装后先运行 doctor，不要自动抓取密钥。
```

安装后先运行 `doctor`。首次抓取密钥、刷新明文库和读取聊天内容都必须由使用者明确提出；安装本身不会启动微信，也不会读取账号数据。普通的 ChatGPT 网页对话不能直接访问 Mac 文件系统，必须使用具备本地工具权限的 ChatGPT/Codex 桌面或本地运行环境。

## 使用边界

- 只用于本人或已明确获授权的微信账号和数据，不要尝试读取他人账号。
- Mac 首次初始化会在本机私有副本中执行密钥捕获；原始 `/Applications/WeChat.app` 不会被重签名或改写。
- 密钥、明文数据库、捕获日志和聊天导出不会写入仓库；不要把这些文件提交、上传或发送给模型。
- 仅在用户明确要求时执行 `extract`、`refresh`、`export-chat` 等涉及本机数据的操作；查询默认只读已生成的私有快照。
- Windows 模式只接受用户提供的、静止且已授权的明文快照，不提取密钥、不解密、不控制微信界面。

## 隐私路径

- 密钥和配置应只保存在本机私有配置区。
- 明文库、解密清单和增量状态应放在本机私有 vault。
- 可读导出报告可以放到用户自选的导出目录。

明文库包含完整本地微信隐私，不要同步、分享或复制到项目目录。

## Mac 常用命令

```bash
~/.local/bin/wechat-vault extract --list-dbs
~/.local/bin/wechat-vault extract --match-only --targets all --reuse-log
~/.local/bin/wechat-vault extract --targets all --duration 240
~/.local/bin/wechat-vault refresh --mode full
~/.local/bin/wechat-vault refresh --mode incremental
~/.local/bin/wechat-vault status --format text
~/.local/bin/wechat-vault sessions --format text
~/.local/bin/wechat-vault history "联系人或群名" --format text
~/.local/bin/wechat-vault search "关键词" --format text
~/.local/bin/wechat-vault stats "群名" --format text
~/.local/bin/wechat-vault favorites --format text
~/.local/bin/wechat-vault moments --name "联系人" --format text
~/.local/bin/wechat-vault digest-source "群名" --start "2026-05-01" --end "2026-05-14" --format text
~/.local/bin/wechat-vault export-chat --contact "联系人备注" --mode full
~/.local/bin/wechat-vault export-chat --contact "联系人备注" --mode incremental
~/.local/bin/wechat-vault export-chat --chat-id "contact_username" --since "2025-01-01"
```

`vault_cli.py` 默认 JSON 输出，适合被 Agent 调用；需要人工查看时加 `--format text`。

## Windows 离线快照查询

使用同一入口，不需要另装 Windows reader：

```bash
~/.local/bin/wechat-vault snapshot --snapshot /path/to/authorized-snapshot validate
~/.local/bin/wechat-vault snapshot --snapshot /path/to/authorized-snapshot chats
```

随后使用返回的精确 `chat_id` 调用 `history`、`search` 或 `export`。此模式不访问 Mac 配置、不抓密钥、不解密、不刷新；仅接受验证器支持的离线明文结构，真实 Windows 版本兼容性仍为实验性。输入契约、依赖、来源和完整用法见 [Windows 快照模式](references/windows-snapshot.md)。

## Mac 查询命令

`scripts/vault_cli.py` 吸收了 WeChat CLI 的常用产品化能力，但仍然只读本 skill 的已解密 vault：

- `status`：检查明文库是否齐全。
- `sessions` / `unread` / `new-messages`：最近会话、未读和增量新消息。
- `contacts` / `members`：联系人、群聊和群成员。
- `history` / `search`：按聊天对象、关键词、时间、消息类型查询。
- `stats`：消息总数、类型分布、发言排行、24 小时分布。
- `export`：Markdown 或 txt 导出。
- `favorites`：收藏夹，支持 text/image/article/card/video。
- `moments`：朋友圈，支持联系人、时间、关键词。
- `digest-source`：生成群聊摘要素材包，供后续写日报、群聊精华或画像。

## 群聊精华素材包

`digest-source` 会在 `{data_root}/{group_id}-{group_name}/` 下创建：

- `sources/*.json`：机器可读消息、统计和路径信息。
- `sources/*.md`：人工可读素材稿。
- `profiles/`：普通版群友画像。
- `profiles-roast/`：毒舌版画像。
- `imgs/`：图片说明扩展点，文件名形如 `{message_id}.txt`。

它只生成素材，不直接更新 `history.json`。最终摘要确认后再更新历史锚点，避免“从上次继续”读到半成品。

## 脚本

- `vault_cli.py`：统一查询、统计、导出、收藏夹、朋友圈和摘要素材包入口。
- `extract_keys.py`：本机 key 捕获、复用和匹配。
- `decrypt_all_dbs.py`：全量/增量解密，写入私密 vault。
- `export_chat.py`：按联系人、群聊或会话 ID 导出聊天。
- `list_contacts.py`：列出联系人和群聊。
- `wechat_digest.py`：按天摘要脚本，仅在明确需要摘要时使用。
- `search_sns.py`：朋友圈搜索辅助。
