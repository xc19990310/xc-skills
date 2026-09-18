# 验证报告

日期：2026-09-17；版本：2026.09.17-local.1。

- 完整隔离测试：52 项，50 项通过，2 项可选旧版输出逐字比较跳过。
- 其中新增 Mac 安全回归：19 项全部通过。SQLCipher 真实加密合成库，不使用微信真实库。
- 独立代码审查与复核：已完成；发现的 WAL salt 静默丢失、符号链接导出覆盖、孤立库未标状态三项均已修复并回归。
- 额外发现并修复 Name2Id 隐藏 rowid 在逻辑导出后重排：改物理解密，行号及数据库元数据回归通过。
- Python 语法、技能入口文档引用及官方 quick_validate 技能元数据检查通过。
- `doctor`：Python 3.14.7；Crypto/zstandard/frida 可用；SQLCipher 4.19.0 community；微信应用版本 4.1.7。
- 安装时无账号配置、无捕获密钥、无真实明文快照。doctor 明确返回 `account_data_read=false`。

跳过的两项依赖额外的 before-integration 旧版行为基线，且新版本有意增加 stderr 新鲜度提示，因此未声称逐字兼容。其余 Windows 快照与路由回归已通过。

未执行：启动/重签名微信副本、真实抓钥、读取真实聊天数据库、真实微信 schema 兼容性验证、金融台/Obsidian 接入、后台定时采集。此报告证明修复与安装检查通过，不等于真实账号初始化成功。

复验命令（从技能目录运行）：

```bash
"$HOME/Library/Application Support/wechat-local-vault/runtime/bin/python" -m unittest discover -s tests -v
~/.local/bin/wechat-vault doctor
~/.local/bin/wechat-vault status --format json
```


## local.2 真实初始化验证（2026-09-17）

完成一个 Mac 微信 4.1.7 账号的基础初始化：联系人、会话及 message_0 三个库真实密钥匹配、SQLCipher/HMAC/SQLite 完整性校验与指定会话最新文本读取成功。没有把其他未初始化库误报成功，14 个未捕获密钥的库仍显式标为 missing_key。解密文件保存在本机私有应用数据目录，含明文隐私；未复制到项目、未上传全库。聊天原文和身份不进入本报告。

兼容修复后的 21 项 Mac 隔离回归全部通过，独立复核同样通过。新增：有效空 WAL-index 支持预分配旧尾部，坏/缺 SHM 仍拒绝；二进制联系人 TEXT 字段不阻断查找。本轮只修改 Mac 路径并执行相关测试，未重复上游 Windows 测试；上次完整验证仍为 50 通过、2 可选跳过。安装阶段未验证的真实三库能力现已验证，其他能力仍未验证。
