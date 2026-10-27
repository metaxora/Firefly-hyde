# InstaBox 单仓库构建集成

状态（2026-10-27）：正式发布及公网验收完成；INSTABOX_AUTO_BUILD_ENABLED=true 已核验，main 自动构建发布已启用。构建机为 zero-forge，生产 zero 保持原程序与配置。

## 固定架构

仅使用公开仓库 `metaxora/Firefly-hyde`：源码、文章和配置保留在 main；成品仅由程序更新 blog-deploy。GitHub 调度 InstaBox 执行完整检查和构建，再在 GitHub 使用原签名 Secret 发布。无需其他仓库、额外 GitHub PAT 或 zero 的新增凭据。

生产端 `pull.py` 保持已验证基线原样，其 Git blob 为 `759ae7c9e5a228e902b35ad12a696090013487d3`。不更改 zero 的公钥、timer、服务账号、Nginx、回退或清理机制。

## 工作流与兼容性

工作流必须保留 `.github/workflows/build.yml` 路径，因为原生产程序会核对它。继续使用版本 2 的 `deployment.json`、`deployment.sig` 和 `site/` 布局；清单中的源码 SHA、运行编号和重试次数来自同一次 main 工作流，不引入新发布协议。

- PR 只执行不带秘密的部署代码和草稿筛除测试；不接触 InstaBox 和签名 Secret。
- main 的依赖安装、Astro 检查、TypeScript 检查和完整构建在 InstaBox 执行。
- 手动运行的 publish 选项默认 false：生成并复验签名预览包，不查询或更新远端分支。
- main 提交自动构建并推送 blog-deploy；手动运行只有 publish=true 才推送。每次推送前重新核对 main 的最新 SHA，过时构建直接跳过。
- 真实试跑和正式验收通过后，用户已启用自动构建变量。设为 false 可暂停 main 自动构建，手动入口继续保留。
- 发布使用独立临时 Git 仓库、用户的 noreply 提交身份及 force-with-lease，源码工作区不切换、不清空。
- zero 继续等整个工作流成功后才验签上线；失败或运行未结束时保留原网站。

## 机器和缓存

InstaBox：zero-forge，`m_agqxotxgzkyg7lawsooh5fr2hi`，免费版，4 核、8 GiB、20 GiB 数据盘；空闲 30 分钟暂停，单次最长 8 小时。

固定 Node.js 22.23.3、pnpm 11.22.0。使用 `npx pnpm@11.22.0`，不改机器原有全局 pnpm。源码通过匿名 HTTPS 缓存在 `/data/home/firefly-blog/repository.git`，按本次完整 SHA 获取，避免误编译较新的分支头。任务独立放在 jobs/<run>-<attempt>，不覆盖预览目录；依赖缓存单独保留。

任务串行并持有机器级锁。远端进程组有独立 24 分钟硬超时，GitHub 构建作业上限 30 分钟。取消时仅尝试终止本次任务进程组；运行中的任务和未知目录不清理，超过一小时的已停止任务由后续构建清理。

## 官方文件接口传输

远端生成 `dist.tar.gz`，小型任务状态提供运行身份、文件大小和 SHA-256。控制信息和有限日志可经 exec 返回，成品二进制不进入 stdout、JSON 或 Base64。

官方接口：

```text
PUT /v1/machines/{id}/files?path=<绝对路径>&mode=0600
GET /v1/machines/{id}/files/content?path=<绝对路径>
```

上传仅用于无凭据的构建驱动脚本，并校验上传响应的大小和摘要。源码由机器匿名读取公开 GitHub，不上传本机源码、Git 历史或任何凭据。

下载客户端固定连接 https://api.instabox.sh，API Key 只在认证请求头里，不写入 URL、日志、远端环境或文件。禁止重定向和不安全 TLS；下载失败不改走 exec/stdout。

下载流式写入 .part，必须满足 HTTP 200、未压缩的传输编码、准确的 Content-Length、远端声明大小、80 MB 压缩上限和 SHA-256，随后原子改名。中断、截断、摘要错误时删除本次 partial，不覆盖已有文件。

解包前限制 gzip 的展开大小及 tar 头读取，拒绝路径穿越、链接、设备、稀疏文件、重复路径、原稿、调试映射及保留服务路由。网站文件上限 3,000 个、单文件 50 MB、总大小 200 MB。

## GitHub 内部交接与签名

验证后的 tar 包作为 `blog-dist-<run>-<attempt>` artifact 保存 3 天。构建作业通过 job output 传递整个 tar 的 SHA-256；签名作业下载同一运行同一次尝试的 artifact，重新核对摘要、元数据和包内容，再准备 dist。此时还没有读取签名私钥。

单独的签名步骤使用原 `BLOG_SIGNING_PRIVATE_KEY`，通过标准输入交给 OpenSSL；原固定公钥复验。生成的发布 ZIP 再由原版 pull.py 的验签和解包函数验证，确认零改动兼容。

手动试跑会保存额外的 `blog-signed-preview-<run>-<attempt>` artifact，内含 release.zip 与运行信息，同样保留 3 天；不会更新 blog-deploy。传输使用 tar.gz，签名预览 ZIP 仅用于验证原生产端的 GitHub archive 布局。

## 已配置的凭据与发布开关

在本仓库 Settings → Secrets and variables → Actions 中管理；两项 Secret 均已配置，值不记录在源码或运维笔记中：

| 类型 | 名称 | 配置 |
| --- | --- | --- |
| Secret | INSTABOX_API_KEY | 现有构建机查询、唤醒、执行和文件接口的最小可用权限 |
| 已有 Secret | BLOG_SIGNING_PRIVATE_KEY | 保持原值，不导出、不轮换 |
| Variable | INSTABOX_AUTO_BUILD_ENABLED | 已设 true，main 提交自动构建发布 |

发布使用 GitHub 自动提供的临时 GITHUB_TOKEN。没有额外 GitHub PAT，不给 zero 或 InstaBox 配置签名密钥或生产管理 SSH 密钥。签名公钥完整 SHA-256 仍为：

```text
f607ca4303c9f5f110fd872d37c5a0bb6beed1545d6553774c683add31bfc5bc
```

## 验收记录与后续范围

第一阶段的迁移分支改造与 Linux CI 已完成。测试覆盖大文件、真实 HTTP 下载、截断、中断、重定向、错误长度与摘要、恶意 tar、超时、取消、源码精确获取及原版生产解析器兼容。

用户随后明确授权合并并试跑，不发布正式网站。[PR #1](https://github.com/metaxora/Firefly-hyde/pull/1) 已合并，真实预览源码为 `6c8f6b8226b119e6b6ff9588dbb25c272066afdc`。[完整运行](https://github.com/metaxora/Firefly-hyde/actions/runs/40441528843) 第 2 次尝试成功：51 项 Python 测试、草稿筛除、Astro／TypeScript 检查与完整构建通过，生成 69 页；官方文件接口下载约 34 MB 成品后，在 GitHub 使用原 Secret 签名，原版 pull.py 的验签和解包函数通过。成品及签名预览 artifact 保存 3 天。

首轮试跑遇到 InstaBox HTTP 502 后停止，签名跳过；重试成功。远程检查、构建与下载共 153.48 秒，成功尝试从启动至工作流完成共 246 秒。该次运行不能代替独立冷／热缓存对照；实际暂停唤醒尚未实测。[API 预检](https://github.com/metaxora/Firefly-hyde/actions/runs/40441312502) 已验证 2,097,169 字节二进制下载的长度与摘要。实机不能由普通用户直接写入 /data 根目录，因此使用持久盘用户目录 /data/home/firefly-blog，无需 sudo。

上述预览结束时正式成品分支仍为 `b790010a1e1cc2fe65fd094c436ae5d4cd8783f6`，公网运行 `7078bcb`。随后用户另行授权一次手动正式发布，结果如下。

### 首次手动正式发布

[正式运行 40443435574](https://github.com/metaxora/Firefly-hyde/actions/runs/40443435574) 第 1 次尝试成功，输入 publish=true。源码 `d80dfe63dbf3bcc69aef04568c2039d06a35a2f8`，发布提交 `bc3dd2b6c6695b883b9b5e83930cc233495b07fc`。北京时间 2026-10-27 11:25:13，zero 已自动下载、验签并上线；公网 source_commit、deployment_commit、运行编号及重试次数匹配，signature_verified=true，signing_key_id=f607ca43。

InstaBox 的 51 项测试、草稿筛除、Astro／TypeScript 检查和完整构建通过，生成 69 页、385 个文件，共 62,120,365 字节。本次远程检查、构建及官方下载校验为 229.93 秒；GitHub 全流程 11:18:28 至 11:23:13，共 4 分 45 秒。blog-dist-40443435574-1 artifact 保留 3 天；成品分支不会随 artifact 到期删除。

公网 15 项核验通过。首页和文章仅多出 Cloudflare 注入的统计脚本及换行（367 字节），移除这段已识别注入后内容摘要匹配签名清单；图标、CSS、JS、Pagefind 代码与索引直接匹配。panel、status、wb、wb/v1、whale.js、whale.css 内容摘要与发布前一致；whale 目录保留既有 403。未修改 zero 的程序、timer、公钥、Nginx 或凭据；未通过 SSH 操作生产机。既有回退和清理机制沿用，本次未制造生产故障重复测试回退。

该次手动正式发布完成时自动构建仍关闭。随后用户明确授权启用，并亲自在 GitHub 设置页保存 INSTABOX_AUTO_BUILD_ENABLED=true；[配置核验运行 40448679418](https://github.com/metaxora/Firefly-hyde/actions/runs/40448679418) 成功读取 true，并核对 main 工作流的 push 条件、签名发布条件及 zero-forge 的固定机器 ID。核验仅检查配置，没有额外触发网站构建或正式发布；临时核验工作流随后移除。

当前日常流程为：提交 main → GitHub 调度 zero-forge 构建 → GitHub 校验、签名并更新 blog-deploy → zero 下载、验签上线。只更新 friends-latency-snapshot.json 的提交按原规则跳过，带 skip ci 的维护提交也不触发；手动预览／重试入口保留。本地部署记录和两份 Obsidian 笔记已同步当前状态。暂停唤醒和独立冷／热缓存基准仍未额外实测。

构建速度按实际唤醒、安装、检查、编译和传输耗时记录，不预设 2 秒或 3 秒。GitHub 等待远程构建期间仍计入 Actions 运行分钟。

保留原有本机源码备份及 backup/pre-instabox-20261026 分支。不要使用 git reset 或 git clean 清理用户修改。
