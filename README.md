# 流萤桌宠（在线版）客户端

PyQt5 桌面宠物 + 多人联机客户端，当前版本 **v4.7.0**（Windows 10/11 64 位）。

服务端仓库：<https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online-Server>

---

## 一、下载与运行

- 开箱即用：解压 `流萤桌宠_v4.7.0.zip` → 双击 `流萤桌宠/流萤桌宠.exe`
- 也可以直接下载仓库里的 `流萤桌宠.exe`（PyInstaller onefile，资源全部内嵌，单文件即可跑）
- 首次运行会在 exe 同目录自动创建 `user_data/`（账号、设置、缓存）
- 如果需要联机，Windows 防火墙提示请选「允许」（走 TCP）

## 二、功能一览

| 功能 | 单机 | 联机 | 说明 |
| --- | :-: | :-: | --- |
| 桌宠互动 | ✅ | ✅ | 投喂、比心、睡觉、唤醒、拖动、更改桌宠大小、修改指针、音量 |
| 每日签到 / 当月收获报告 | ✅ | ✅ | 收获报告按月统计 |
| 兑换码 / 兑换明细 | ✅ | ✅ | |
| 商店 | ✅ | ✅ | 信用点、星琼、橡木蛋糕卷、星铁专票；管理员上架的自定义商品 |
| 抽卡 | ✅ | ✅ | 卡池（限定/常驻）、保底与 UP、历史记录；**1600 星琼 = 10 张专票** |
| 邮箱 | ✅ | ✅ | 一键领取，物品直接进背包 |
| 背包 | ✅ | ✅ | 内置物品用自带彩色图标；服务器新增物品用管理员上传的图片 |
| 时装 | ✅ | ✅ | 内置时装 + 自定义时装（四尺寸 GIF） |
| 游戏社区 → 小三月日记 | ✅ | ✅ | |
| 多人联机 | — | ✅ | 在线对话、私聊、好友、@提及、撤回、禁言、在线列表、通知 |
| 资源热更新 | — | ✅ | 服务端改完物品/商店/卡池/页面后，客户端收到广播或点「检查更新」自动刷新 |

## 三、连服务器

1. 右键桌宠 →「🌐 多人联机」→「第三方的服务器功能」→ 添加服务器，地址格式 `IP:端口`
   （服务端默认 **TCP 20082**，管理员可以在服务端窗口的设置行里改；**端口要和客户端填的一致**）
2. 服务端的管理页默认在 `http://127.0.0.1:8080`（以服务端设置为准）
3. 账号安全：登录用 SCRAM 风格挑战应答（**客户端不发送明文口令**），首次登录会与设备建立信任
   （TOFU / 绑定码）；如果被管理员重置，可在服务端控制台用 `/bindcode` 重新绑定

## 四、本地数据与缓存

| 目录 / 文件 | 说明 |
| --- | --- |
| `user_data/` | 账号、设置、签到与抽卡记录、`custom_items_cache.json`（服务器物品定义缓存） |
| `custom_assets/` | 从服务器下载的物品图（按物品版本号命名），被改动会自动删除并重下 |
| `server/<识别码>/shop/self_area_img/` | 商店/自定义物品图片；目录名是**哈希**，不包含服务器 IP 与端口 |
| `server_resources/<菜单id>/` | 联机页面资源（index.html、图片、视频 + 完整性清单 `.integrity.json`） |
| `clothes_custom/` | 自定义时装资源（按版本号缓存） |

资源防篡改：下载的页面/图片都会登记 sha256，发现被改动就删除重下；
服务端下发的页面资源包是**加密 + 签名**的，客户端校验通过后才落盘。

## 五、从源码运行 / 自行打包

需要 Python 3.10 与下列主要依赖（仓库无 `requirements.txt`，按报错安装即可）：

```
PyQt5==5.15.2          # 含 QtWebEngineWidgets / QtWebChannel
lunardate, pytz, psutil, win10toast, pygame
```

```bash
python main.py         # 源码运行
```

自行打包（PyInstaller onefile，资源目录要一起打进去）：

```bash
pyinstaller --noconfirm --clean --onefile --windowed --name FireflyPet ^
  --icon assets/images/firefly/icon/icon.ico ^
  --add-data "assets;assets" --add-data "clothes;clothes" --add-data "html;html" ^
  --add-data "icon;icon" --add-data "mouse;mouse" --add-data "music;music" ^
  --add-data "Large_Music;Large_Music" --add-data "config;config" ^
  --add-data "gacha_base;gacha_base" --add-data "tools;tools" ^
  --hidden-import win10toast --hidden-import lunardate --hidden-import pytz --hidden-import psutil ^
  main.py
```

## 六、最近的修复摘要

- **第136批**：管理页上传图片后「待上传」不再卡住（保存成功即清；「发布 / 热更新」会先把待上传的图片落盘）；
  背包里服务器新增的物品改用**上传的真实图片**（此前一直显示旧 emoji）；
  背包图标恢复**彩色**（Qt 在禁用态会把 QLabel 的图片转成灰阶，改为自绘绕过）。
- 第134～135批：邮箱回归纯文本、物品必须上传图片、「游戏社区」新菜单（小三月日记移入）、
  单机模式不显示服务器新增物品、信用点/星琼图标改用自带资源。
- 第131～133批：连接与文件传输安全（挑战应答登录、设备绑定、受限会话、加密资源包 + 签名）、
  抽卡结果路由修正。

完整逐批说明见维护者的《修复说明.md》。

## 七、声明

仅供学习与交流使用，请勿用于商业用途；《崩坏：星穹铁道》相关素材与角色版权归米哈游所有。
