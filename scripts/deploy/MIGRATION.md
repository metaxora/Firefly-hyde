# InstaBox 构建迁移

状态：代码实施中，正式博客尚未切换。没有把签名私钥导出或传入 InstaBox。

## 仓库与机器

- 私有源码：`metaxora/blog-source`，默认分支 `main`，必须为独立私有仓库。
- 公开签名及成品：`metaxora/Firefly-hyde`。
- 正式成品：`blog-deploy`；候选成品：`blog-deploy-candidate`。
- InstaBox：`zero-forge`，`m_agqxotxgzkyg7lawsooh5fr2hi`；免费版，空闲 30 分钟暂停、单次最长 8 小时。
- 实测 x86_64、4 CPU、7.8 GiB 内存，系统盘 16 GiB、数据盘 20 GiB；Node 22.23.3、Python 3.13.5、OpenSSL 3.5.7。
- 构建使用精确版本 Node 22.23.3、pnpm 11.22.0。构建机现有全局 pnpm 12.15.0 不会被修改。

## 凭据和变量

用户在 GitHub Settings → Secrets and variables → Actions 手动录入秘密值。不要发送到聊天、源码、笔记或工作流输入框。

| 位置 | 名称 | 范围 |
| --- | --- | --- |
| 私有仓库 Secret | `INSTABOX_API_KEY` | InstaBox 机器读取、操作和文件传输所需最小权限；账号不支持单机范围时不能宣称已限制单机 |
| 私有仓库 Secret | `RELEASE_DISPATCH_TOKEN` | Fine-grained PAT，只选择公开仓库，Actions read/write；无需 Contents 写权限 |
| 公开仓库 Secret | `SOURCE_READ_TOKEN` | Fine-grained PAT，只选择私有仓库，Actions read、Contents read |
| 公开仓库已有 Secret | `BLOG_SIGNING_PRIVATE_KEY` | 原值保持不变，不需要取回或重填 |
| 私有仓库变量 | `AUTO_BUILD_ENABLED` | 初始 `false`；验收完成后 `true` |
| 私有仓库变量 | `RELEASE_CHANNEL` | 初始 `candidate`；正式切换时 `production` |
| 公开仓库变量 | `INSTABOX_PRODUCTION_ENABLED` | 初始 `false`；正式切换时 `true` |

GitHub PAT 设置明确到期时间，建议初次使用 90 天；到期前轮换。InstaBox API Key 依平台支持设置到期与权限。zero 不保存这些令牌。InstaBox 只收到源码快照和构建任务，不接收 GitHub 令牌、平台 API Key、签名私钥或 zero SSH 密钥。

`source-policy.json` 是两端固定策略。创建私有仓库后，通过 GitHub API 获取数值 repository ID，写入 `source_repository_id`。空值会拒绝版本 3 构建与签名，不会猜测或放行。服务器安装的副本必须 root 所有、发布账号只读。

## 一次构建怎样交接

1. 私有工作流仅允许 main 的 push 或 workflow_dispatch，自动触发受 `AUTO_BUILD_ENABLED` 控制。
2. GitHub 从固定提交生成不带 Git 历史、环境秘密和凭据文件的快照。官方 InstaBox CLI 的 HTTPS 下载先验证供应商提供的 SHA-256，然后用 API Key 调用 CLI；CLI 不继承 GitHub 令牌。
3. 机器暂停时恢复、停止时启动；删除中或未知状态直接失败。任务在 `/data/firefly-builds/jobs/<run>-<attempt>` 隔离，跨任务持有构建锁，缓存位于同一根目录的独立目录。
4. 临时源码安装锁定依赖，运行 Python 安全测试和草稿筛除测试；生产构建前从临时快照删除所有 Markdown/MDX 的 `draft: true` 文档，不改原仓库或预览源码。随后运行 Astro、TypeScript 检查和完整构建。
5. 远端进程组有独立 24 分钟硬超时，GitHub 作业上限 30 分钟。取消时尝试只终止本次任务的进程组；不依赖后台无限保活。
6. 从临时 dist 删除调试 source map 和主题自带的 `pio/README.md`，只移除 JS/CSS 末尾独立的 source map 注释；遇到其他原稿或秘密仍拒绝打包。成品及元数据进入 `website.zip`。GitHub 取回后重新检查路径、大小、文件类型、秘密特征及源映射，上传唯一命名的私有 artifact，保留 3 天。正常清理任务目录，过期且不运行的任务在后续构建中清理；依赖缓存保留。
7. 构建工作流成功完成后，通知工作流触发公开签名工作流。公开端固定读取指定私有仓库，核对运行、重试、工作流、main 最新提交、artifact 归属和 GitHub 摘要；不信任任意下载 URL 或用户输入的仓库名。
8. 公开签名端不执行成品代码，使用原 Ed25519 Secret 签名，核对固定公钥并通过独立 Git 仓库和 force-with-lease 更新指定成品分支。并发签名串行，旧源码构建不能覆盖已发布的新构建。

冷启动、依赖安装和网络传输都计入耗时。远程构建期间 GitHub 作业仍占用 Actions 分钟，不能假定为零成本或固定 3 秒。

## 服务器只读验收和安装

先使用 KeePassXC 的 oracle 条目把管理员密钥手动加入 SSH Agent。不要导出管理私钥。所有操作先确认真实 current、previous 和 state.json，并备份原程序。

服务器运行时只需要三份 root 管理的文件：`pull.py`、`artifacts.py`、`source-policy.json`。现有公钥不替换，完整 SHA-256 应为：

```text
f607ca4303c9f5f110fd872d37c5a0bb6beed1545d6553774c683add31bfc5bc
```

先将新程序安装到独立、root 所有的候选程序目录，候选工作流完成成功后执行：

```sh
sudo -u blog-deploy python3 -B /usr/local/lib/metaxora-blog-deploy-candidate/pull.py \
  --verify-only --branch blog-deploy-candidate --expected-commit <候选分支的完整提交SHA>
```

此模式不创建状态锁、不清理正式目录、不切换 current、不修改 state.json；临时下载使用系统临时目录。必须看到 `VERIFIED read-only`，并核对验收前后正式状态完全一致。

接着在隔离目录运行全部部署测试。真实候选构建、验签、只读验收、回退及清理都通过后，才短暂停止 timer 和 service，把这三份程序安装到正式程序目录，再恢复 timer。systemd 单元、公钥和 Nginx 不因迁移而改变。

正式切换前先禁止旧构建流程与新流程争写成品分支；保留旧流程文件便于审查。然后开启公开端生产开关，将私有端发布通道改为 production，手动运行一次新构建。正式版本验收成功后才开启自动构建，最后整理公开 main 中的旧源码和不用的工作流。

## 版本 3 和回退边界

- `repository` 和 `workflow_run_id` 继续指公开签名仓库与运行；`publisher_commit` 单独绑定公开工作流提交。
- `source_repository`、数值 ID、`source_commit`、`build_run_id` 和 `build_run_attempt` 指私有构建来源；清单还绑定工作流路径、artifact ID、摘要及发布通道。
- zero 匿名核对公开签名流程成功。版本 3 校验两组运行顺序；首次成功上线版本 3 后，状态中的 `minimum_schema` 固定至少为 3，拒绝旧协议降级。
- 健康检查比较首页和 panel、status、wb API、whale.js 的基线。失败回退，保留当前和上一版，沿用超过一小时的残留清理。
- 紧急回退先停 timer 和 service，使用 state.json 的 previous 原子切换网站。保留新的验签程序和状态最低版本，不通过删除状态文件或恢复无签名入口回退。
- 私有源码不等于网站内容私有。公开成品、浏览器代码和被明确放入 public 的资源仍可见；旧公开 Git 历史也无法保证收回。

## 验收记录

本机源文件备份：`F:\WorkSpace\Codex\firefly-deployment\migration-backup-20261026T094234Z`。
备份分支：`backup/pre-instabox-20261026`，提交 `df8d3de`；本地原有修改已保存，未丢弃。

候选程序代码的首次 Linux CI：
https://github.com/metaxora/Firefly-hyde/actions/runs/40304258325
39 项 Python 测试通过；这不代表已经完成真实 InstaBox 构建或生产切换。

在真实流程上线前，不把 Obsidian 正式运维笔记改写为“已迁移”。
