# zero 自动发布

此部署针对公开仓库 `metaxora/Firefly-hyde` 和站点 `https://metaxora.com/`。

## 发布流程

1. 提交 main 后，GitHub Actions 并行执行源代码检查和构建。
2. 两个任务成功后，publish 任务将静态文件和逐文件 SHA-256 清单写入 `blog-deploy` 分支。此分支是自动生成的，不要手动编辑；每次发布替换分支头，只保留当前发布快照。
3. zero 的 systemd timer 在上一次检查结束两分钟后再次检查，下载固定提交的快照，确认对应的 main 工作流已完成且成功，校验全部文件后切换 current。
4. 发布期间旧网站继续提供服务。新首页验证失败或原有面板、状态页、接口健康检查响应变化时自动恢复前一版。

只有 publish 任务拥有仓库 `contents: write`；使用 GitHub 自动提供的临时 GITHUB_TOKEN。服务器访问公开 GitHub 不使用令牌，GitHub 不保存服务器 SSH 私钥。PR 构建不会发布。

## 服务器安装

通过已授权 SSH Agent 以 ubuntu 管理账号登录，由管理员安装；不需要给自动发布账号设置密码或 SSH 密钥。

- 系统账号：`blog-deploy`，禁止交互登录、无 sudo。
- 程序：`/usr/local/lib/metaxora-blog-deploy/pull.py`，root 所有，服务账号不能修改。
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

检查损坏构建包、路径穿越、占用现有路由的文件和健康检查失败时的回退。Linux CI 执行全部测试。
