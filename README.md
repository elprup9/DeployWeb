# DeployWeb

把本地静态网页一键发布到公网，得到一个能发给别人的网址。

**完全免费 —— 不需要买域名，不需要注册 Cloudflare 账号，不需要任何配置。**

基于 Cloudflare Quick Tunnel，纯 Python 标准库实现，Windows 双击即用。

---

## 快速开始

1. 下载本仓库，解压到任意文件夹
2. 双击 `DeployWeb.exe`
3. 把网页文件夹拖进黑色窗口，按回车
4. 几秒后窗口显示公网地址，已自动复制到剪贴板，粘贴发人即可

```
====================================================================
  公网地址   https://xxxx-xxxx-xxxx-xxxx.trycloudflare.com
  本地地址   http://127.0.0.1:54321
  网页根目录 D:\mysite
====================================================================
```

按 `Ctrl+C` 或直接关窗，地址立即失效，隧道自动清理。

> `DeployWeb.exe` 与 `cloudflared-windows-amd64.exe` 必须放在同一个文件夹里。

---

## 命令行用法

| 参数 | 作用 |
|---|---|
| `DeployWeb.exe D:\mysite` | 直接指定网页目录（也可把文件夹拖到 exe 图标上） |
| `--spa` | 单页应用（Vue/React 打包产物）路由回退，刷新子页面不会 404 |
| `--port 8080` | 指定本地端口，默认自动选空闲端口 |
| `--open` | 成功后自动用浏览器打开 |
| `-v` | 显示 cloudflared 完整日志，排查问题用 |
| `--cloudflared 路径` | 手动指定 cloudflared 位置 |

不打包也能直接跑源码，功能完全一样：

```bash
python deploy_web.py D:\mysite
```

---

## 关于 index.html

程序会自动定位网站根目录：给定目录下有 `index.html` 就用它；没有则向下扫描最多 4 层（跳过
`node_modules`、`.git` 等）。只有一个候选时自动选用，多个时列出清单让你选序号。

根目录取的是**包含 `index.html` 的那一层**，所以 `dist/index.html` 这类打包产物里
`./css/style.css` 的相对引用能正确加载。

---

## 限制（重要）

| 限制 | 说明 |
|---|---|
| **网址每次都会变** | 每次重启都是全新的随机子域名，旧地址立即失效。固定网址需域名 + 账号，是另一套方案。 |
| 无可用性保证 | Cloudflare 官方定位为开发测试用途，不保证 24 小时稳定。 |
| 并发上限 200 | 同时请求超过 200 会返回 429。 |
| 不支持 SSE | 服务端推送不可用，页面能开但实时更新会失效。 |
| 仅限静态网页 | 纯 HTML/CSS/JS 可用；需要后端逻辑的网站不适用。 |
| 会记录来源 IP | Cloudflare 会记录建立隧道的来源 IP，这不是匿名服务。 |

---

## 中国大陆用户须知

实测在 Windows 11 + 中国大陆网络下**直连正常，无需代理**。两个已知坑：

**1. cloudflared 不读取 `HTTP_PROXY` / `HTTPS_PROXY`。**
已实测验证：把这两个变量指向不存在的端口，cloudflared 照样能建隧道 —— 普通 HTTP 代理对它毫无作用。
连不上时需开启代理的 **TUN 模式或全局模式**（在网络层接管流量），再回窗口按回车重试。

**2. 本机 DNS 可能对 `*.trycloudflare.com` 返回 NXDOMAIN。**
表现是隧道建成功、地址也拿到了，但自己在浏览器里打不开。程序会通过公共 DoH 交叉验证并提示你。
解决办法：把本机 DNS 改为 `223.5.5.5`（阿里）或 `119.29.29.29`（腾讯）。

> 如果分享对象打不开而你自己能开，多半是**对方**的 DNS 有同样问题，让他也改 DNS 即可。

---

## 从源码重新打包

改完 `deploy_web.py` 后双击 `build.bat`：

- 需要 Python 3.8+（安装时勾选 "Add Python to PATH"）
- 自动检测并安装 PyInstaller，产物 `DeployWeb.exe` 放回项目根目录，中间文件自动清理

---

## 文件说明

| 文件 | 说明 |
|---|---|
| `DeployWeb.exe` | 主程序，双击运行 |
| `cloudflared-windows-amd64.exe` | Cloudflare 官方隧道客户端，必需，与上者同目录 |
| `deploy_web.py` | 源码 |
| `build.bat` | 重新打包脚本 |

---

## 致谢与许可

隧道能力来自 [Cloudflare cloudflared](https://github.com/cloudflare/cloudflared)（Apache License 2.0），
本仓库内的 `cloudflared-windows-amd64.exe` 为其官方发布版本，版权归 Cloudflare, Inc. 所有。

本项目的 `deploy_web.py`、`build.bat` 等自有代码未声明许可证 —— 若需他人自由使用，建议补充一个 LICENSE。
