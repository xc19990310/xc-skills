# 本地修订架构

## 2026-09-17：私有存储与可靠快照

**改变与原因。** 上游直接读取主库并覆盖明文，可能遗漏 WAL 提交，且失败会删除旧结果。本地改为 `DB + WAL -> 私有副本 -> SQLCipher 恢复/checkpoint/完整性与HMAC检查 -> 物理页解密 -> 明文完整性检查 -> 原子替换`。增量比较主库、WAL、输出哈希；源文件变化时失败。原始微信数据库不通过 SQLite/SQLCipher 打开，无任何源 checkpoint 或业务写入。

**模块与合同。** `encrypted_snapshot.py` 负责恢复、校验、物理解密和只读连接；`private_io.py` 负责 0700/0600、原子文件写入及导出路径边界。`decrypt_all_dbs.py` 调度逐库刷新，私有锁禁止并行刷新，生成永久清单与 `refresh_status.json`；只有通过校验的数据可标 `ok` 或 `unchanged`，另有 `miss`、`missing_key`、`retained_orphan`、`skip`。每个库记录快照时点，失败的旧库保留原时点。开始刷新即写 `complete=false/status=refreshing`，进程中断不会继续呈现上次完整成功。

`extract_keys.py` 取消隐式安装、限制捕获日志权限、屏蔽账号路径、禁止原应用重签名、多账号不默认选择。`vault_cli.py` 和导出脚本默认读取私有快照，导出默认私有目录；所有查询仍属于历史数据，不等于实时同步。Windows `snapshot` 分支继续独立，不触发 Mac 配置或解密。

**明确排除。** 不对源库在线 checkpoint，不忽略 WAL，不在校验失败后退回 AES 主库解密，不使用逻辑 `sqlcipher_export`（会重排行号，破坏 Name2Id 身份映射），不自动删除旧明文，不默认向模型发送全库。不把本次安装扩展成金融台数据接入或后台采集，不伪造多账号支持。

**运行环境。** Homebrew SQLCipher 4.19.0 + 独立 Python 3.14 环境，依赖见 `requirements-local.lock`。`scripts/wechat-vault -> scripts/run.py` 是统一入口，`doctor` 不读取账号数据；`extract`、`refresh`、`export-chat` 转发对应脚本，其他子命令进入查询 CLI。

**验证与未完成。** 用真实 SQLCipher 加密的合成 fixture 校验 WAL 提交/回滚、坏帧与坏salt、源变化、错钥保留、输出原子性、隐藏rowid、权限与路径隔离，并运行上游快照测试。真实微信 4.1.7 捕获和 WCDB schema 兼容性尚未验证。WAL 回收尾部保守拒绝，频繁写入可能要求正常退出后刷新；多库快照不是共同事务时点。详细结果见 `../VERIFICATION.md`。


## 2026-09-17：真实 WCDB 兼容修复（local.2）

首次真实运行发现已清空的 WAL 仍保留预分配尾部。新增严格的空日志例外：仅第一帧 salt 不匹配，且复制的 SHM 两份头完全一致、头校验和有效、版本/页大小/字节序/salt 与 WAL 对应，并且 mxFrame、nBackfill、nBackfillAttempted 都为 0 时，允许 SQLCipher 按空 WAL 恢复。主库、WAL、SHM 均加入复制前后指纹；SQLCipher 运行前只删除副本 SHM，由它重建。活跃日志损坏、无索引、坏索引、非空尾部仍拒绝，不修改源日志。依据为 SQLite 官方 WAL-index 合同：https://www.sqlite.org/walformat.html 。引擎指纹升级 v3，避免复用旧语义状态。

联系人表还存在声明为 TEXT 的二进制头像元数据。load_contacts 改为局部以 bytes 读取并逐字段解码，不让无关头像阻断备注查找；其他查询连接语义不变。禁止用模糊查询第一条默认认定目标，本次查询以唯一精确备注确认。

验证：新增空重用 WAL、坏/缺 SHM 拒绝与二进制联系人元数据合成回归；保留活跃 WAL、错钥、发言人 rowid 等测试。真实 Mac 微信 4.1.7 的联系人、会话、一个消息分库已校验解密，并验证一个明确授权会话的近期文本读取。其他未捕获密钥的库仍标 missing_key；不声明账号全量完整，不把媒体占位当成媒体已解析。原文、账号标识与密钥均不写入本技能文档。
