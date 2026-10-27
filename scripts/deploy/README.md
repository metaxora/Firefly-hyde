# zero 自动发布

InstaBox 单仓库构建集成见 [MIGRATION.md](MIGRATION.md)。2026-10-27 已完成首次手动正式发布并由 zero 验签上线；手动运行默认仍仅保存签名预览，正式发布需选择 publish=true。自动构建仍关闭，单纯提交 main 不会自动上线。

此部署针对公开仓库 `metaxora/Firefly-hyde` 和站点 `https://metaxora.com/`。

## 发布流程

1. 手动运行 build.yml 可启动构建；提交 main 自动构建需变量 INSTABOX_AUTO_BUILD_ENABLED=true，目前未开启。GitHub 执行轻量部署检查，调度 InstaBox 完成锁文件依赖安装、部署测试、草稿筛除、Astro／TypeScript 检查和完整构建，再经官方文件接口下载、验证成品。
2. 检查与构建成功后，publish 任务用专用 Ed25519 私钥签署逐文件 SHA-256 清单，并经原版生产解析器复验。手动 publish=false 只保存签名预览；publish=true 或启用自动构建后的 main 提交才将静态文件、清单及 `deployment.sig` 写入 `blog-deploy` 分支。此分支是自动生成的，不要手动编辑；每次发布替换分支头，只保留当前发布快照。
3. zero 的 systemd timer 在上一次检查结束两分钟后再次检查，使用本机固定公钥验签，再确认对应 main 工作流成功、核对运行编号及重试次数、校验全部文件后切换 current。旧运行记录重放会被拒绝。
4. 发布期间旧网站继续提供服务。新首页验证失败或原有面板、状态页、接口健康检查响应变化时自动恢复前一版。

GitHub Actions 后续生成的 `blog-dist` 构建包保留 3 天。此设置不追溯修改已有构建产物的到期时间。

## 自动清理异常残留

每轮检查先取得部署锁，再做清理，然后才访问 GitHub；即使没有新版本或网络检查失败，也会运行清理。

- 清理超过一小时未修改的 `download-*` 下载目录、`.staging-*` 解压目录，以及未被引用的孤立 `auto-*` 发布目录。
- 同时清理超过一小时的 `state.next.json` 和未完成切换留下的 `current.next` 链接。
- 目录内部只要有最近一小时修改过的文件，就保留整个目录。符号链接不会被当作普通目录递归清理；未知文件名和目录不在清理范围内。
- 实际 current 指向、state.json 中的 release 和 previous 始终受到保护，不按时间删除。无法可靠识别这些引用时放弃自动清理，避免误删。
- 正常失败和成功仍会立即清理临时包；一小时清理主要补救断电、超时或强制终止后的遗留。

一小时是清理年龄阈值，不是严格到秒的删除时限。达到阈值后在下一轮约两分钟的检查时清理；停机期间不能清理，恢复运行后继续。日志 `CLEANED stale deployment path` 表示已回收残留。

只有 publish 任务拥有仓库 `contents: write`；使用 GitHub 自动提供的临时 GITHUB_TOKEN。服务器访问公开 GitHub 不使用令牌，GitHub 不保存服务器 SSH 私钥。PR 构建不会发布。

## 专用签名密钥

- 私钥仅存于该仓库的 Actions Secret：`BLOG_SIGNING_PRIVATE_KEY`，只有发布任务使用。
- 公钥在仓库 `scripts/deploy/signing-public.pem` 中公开，服务器信任副本固定在 `/etc/metaxora-blog-deploy/signing-public.pem`，由 root 管理。
- 发布账号不能修改服务器信任的公钥；不会从发布分支自动更新公钥。
- 这套密钥只用于文件签名，没有加入 SSH authorized_keys，不能用来登录服务器。
- 签名算法为 Ed25519。私钥在发布时通过标准输入交给 OpenSSL，不写入工作区、发布分支或构建包。
- 无私钥、无签名、签名错误、清单或文件被改动时拒绝发布，不会退回接受无签名文件的模式。
- 更换密钥需要先配置新的 GitHub Secret 与服务器公钥并实测；从 GitHub 无法取回已有 Secret 的明文。如密钥丢失，应生成新的签名密钥并替换双方配置。

签名保护发布文件不被仅有发布分支写权限者伪造。拥有足够权限修改 main 工作流并使用该 Secret 的人仍具有发布能力；GitHub 账号和授权工具的安全依然重要。

## 服务器安装

通过已授权 SSH Agent 以 ubuntu 管理账号登录，由管理员安装；不需要给自动发布账号设置密码或 SSH 密钥。

- 系统账号：`blog-deploy`，禁止交互登录、无 sudo。
- 程序：`/usr/local/lib/metaxora-blog-deploy/pull.py`，root 所有，服务账号不能修改。
- 验签公钥：`/etc/metaxora-blog-deploy/signing-public.pem`，root 所有，文件权限 0644、目录权限 0755。
- 服务和定时器：本目录同名 `.service`、`.timer` 安装到 `/etc/systemd/system/`。
- 网站目录：`/var/www/metaxora-blog/`，授予 blog-deploy 写权限。
- 状态：`/var/lib/metaxora-blog-deploy/state.json`，服务运行时创建，其他用户不可读。

service 使用只读系统文件、防提权、隐藏 home/data 等限制，仅允许写网站目录及自己的状态目录。无需改动 Nginx、Cloudflare 或防火墙。

安装后：

```sh
sudo systemd-analyze verify /etc/systemd/system/metaxora-blog-deploy.service /etc/systemd/system/metaxora-blog-deploy.timer
sudo systemctl daemon-reload
sudo systemctl enable --now metaxora-blog-deploy.timer
```

## 查看状态与手动检查

```sh
systemctl list-timers metaxora-blog-deploy.timer
sudo journalctl -u metaxora-blog-deploy.service -n 30 --no-pager
sudo systemctl start metaxora-blog-deploy.service
```

公网版本信息：https://metaxora.com/deployment-version.json 。包含源码提交、发布提交和构建编号；仅在首次自动发布后存在。GitHub 的 publish 成功表示发布文件已准备好，服务器上线结果以此版本信息和服务器日志为准。

通常在构建、发布任务完成后的两分钟左右上线。下载时间、GitHub 限流或故障可能延迟；遇到下载、校验错误时保留当前网站，下一轮重试。

## 回退和停止

先停止 timer，避免操作期间新版本覆盖人工回退。读取 state.json 中的 previous 路径，确认目录存在，创建 current.rollback 临时链接后用 mv -Tf 替换 current。验证后可恢复 timer。

```sh
sudo systemctl stop metaxora-blog-deploy.timer
sudo systemctl stop metaxora-blog-deploy.service
```

每次成功发布保留当前和上一版，清理更老的自动发布目录；下载包和 staging 目录完成后自动清理。此前手工归档的回退包与 Nginx 配置备份不在自动清理范围内。

## 测试

```sh
python3 -B -m unittest discover -s scripts/deploy -p 'test_*.py' -v
```

检查正确签名、清单篡改、错误公钥、缺少签名、旧构建重放、损坏构建包、路径穿越、占用现有路由的文件、健康检查失败时的回退，以及过期残留回收、新文件保护、版本保护和网络失败前清理。Linux CI 执行全部测试。
