# InstaBox 单仓库构建集成

状态：第一阶段代码改造与测试；未合并 main、未更新 blog-deploy、未操作 zero。

## 固定架构

仅使用公开仓库 `metaxora/Firefly-hyde`：源码、文章和配置保留在 main；成品仅由程序更新 blog-deploy。GitHub 调度 InstaBox 执行完整检查和构建，再在 GitHub 使用原签名 Secret 发布。无需其他仓库、额外 GitHub PAT 或 zero 的新增凭据。

生产端 `pull.py` 保持已验证基线原样，其 Git blob 为 `759ae7c9e5a228e902b35ad12a696090013487d3`。不更改 zero 的公钥、timer、服务账号、Nginx、回退或清理机制。

## 工作流与兼容性

工作流必须保留 `.github/workflows/build.yml` 路径，因为原生产程序会核对它。继续使用版本 2 的 `deployment.json`、`deployment.sig` 和 `site/` 布局；清单中的源码 SHA、运行编号和重试次数来自同一次 main 工作流，不引入新发布协议。

- PR 只执行不带秘密的部署代码和草稿筛除测试；不接触 InstaBox 和签名 Secret。
- main 的依赖安装、Astro 检查、TypeScript 检查和完整构建在 InstaBox 执行。
- 手动运行的 publish 选项默认 false：生成并复验签名预览包，不查询或更新远端分支。
- publish=true 才推送 blog-deploy。每次推送前重新核对 main 的最新 SHA，过时构建直接跳过。
- 自动构建变量默认关闭；真实试跑和正式验收成功后才启用。
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

## 后续需要的唯一新增 Secret

在本仓库 Settings → Secrets and variables → Actions 中添加：

| 类型 | 名称 | 配置 |
| --- | --- | --- |
| Secret | INSTABOX_API_KEY | 现有构建机查询、唤醒、执行和文件接口的最小可用权限 |
| 已有 Secret | BLOG_SIGNING_PRIVATE_KEY | 保持原值，不导出、不轮换 |
| Variable | INSTABOX_AUTO_BUILD_ENABLED | 初始 false，真实验收通过后才设 true |

发布使用 GitHub 自动提供的临时 GITHUB_TOKEN。没有额外 GitHub PAT，不给 zero 或 InstaBox 配置签名密钥或生产管理 SSH 密钥。签名公钥完整 SHA-256 仍为：

```text
f607ca4303c9f5f110fd872d37c5a0bb6beed1545d6553774c683add31bfc5bc
```

## 验收与范围

第一阶段只在迁移分支改代码、运行 Linux CI、更新现有草稿 PR；不合并 main，不更新 blog-deploy，不登录 zero。覆盖大文件、真实 HTTP 下载、截断、中断、重定向、错误长度与摘要、恶意 tar、超时、取消、源码精确获取及原版生产解析器兼容。

后续顺序：配置唯一新增 Secret → 在关闭自动构建的情况下安装工作流 → 手动 publish=false 真实试跑 → 检查签名预览 → 手动 publish=true → 确认线上版本、公钥摘要和原有服务 → 开启自动构建 → 更新正式运维笔记。

构建速度按实际唤醒、安装、检查、编译和传输耗时记录，不预设 2 秒或 3 秒。GitHub 等待远程构建期间仍计入 Actions 运行分钟。

保留原有本机源码备份及 backup/pre-instabox-20261026 分支。不要使用 git reset 或 git clean 清理用户修改。
