# -*- coding: utf-8 -*-
import logging
import math
import json
import os
import socket
import threading
import time
import shutil
import subprocess
import sys
import traceback
import weakref
import hashlib
import io

import psutil
import pygame
import webbrowser
import re
import random
import uuid
import urllib.request
from datetime import datetime, timedelta, timezone

from PyQt5 import sip
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWidgets import *
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile, QWebEngineSettings
from lunardate import LunarDate
from PyQt5.QtCore import QAbstractNativeEventFilter, QTimer
import ctypes
import html
from ctypes import wintypes
import urllib.error
from win10toast import ToastNotifier

def get_base_dir():
    """获取资源文件（只读）的根目录，打包后为 _MEIPASS，否则为项目根目录"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    else:
        return os.path.dirname(os.path.abspath(__file__))

def normalize_chat_history(history):
    """统一聊天历史字段：服务端存的历史用 msg_id 键，客户端内部统一用 id。
    不归一的话，重连后历史消息没有 id —— 右键撤回按钮不会出现（表现为"只能删除"）。
    """
    result = []
    for entry in (history or []):
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        if not item.get('id'):
            item['id'] = item.get('msg_id')
        if not item.get('sender'):
            item['sender'] = item.get('from_name') or '?'
        result.append(item)
    return result


def get_writable_dir():
    """获取可写数据目录（用户数据、服务器资源、缓存等），打包后为 exe 所在目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


# 时装覆盖目录：打包后内置资源只读，时装文件需写入可写目录并在加载时优先使用
ASSETS_OVERRIDE_DIR = "assets_override"

def get_assets_overlay_dir():
    """时装覆盖资源的存放目录（可写）"""
    return os.path.join(get_writable_dir(), ASSETS_OVERRIDE_DIR)

def asset_path(*parts):
    """解析 assets 下资源路径：优先使用时装覆盖目录中的文件，回退到内置资源"""
    p = os.path.join(get_assets_overlay_dir(), *parts)
    if os.path.exists(p):
        return p
    return os.path.join(get_base_dir(), "assets", *parts)

def cleanup_mei_children():
    """退出/重启前结束仍占用 _MEI 临时目录的子进程。

    PyInstaller onefile 退出时若 QtWebEngine 渲染进程、tools/AI.exe 等
    （都从 _MEIPASS 里运行）还没退出，会弹
    "Failed to remove temporary directory: ...\\_MEIxxxx" 警告。
    只按 exe 路径清理位于本进程 _MEIPASS 内的进程；重启/另开的新实例
    exe 不在 _MEI 内，不受影响。开发模式无 _MEIPASS，直接跳过。
    """
    try:
        if not getattr(sys, 'frozen', False) or not hasattr(sys, '_MEIPASS'):
            return
        mei = os.path.normcase(os.path.abspath(sys._MEIPASS))
        me = os.getpid()
        victims = []
        for proc in psutil.process_iter(['pid', 'exe', 'cmdline']):
            try:
                if proc.pid == me:
                    continue
                info = proc.info
                exe = info.get('exe') or ''
                cmd = ' '.join(info.get('cmdline') or [])
                # exe 在 _MEI 内，或命令行引用了 _MEI（exe 读不到时兜底）
                if (exe and os.path.normcase(exe).startswith(mei)) or \
                        (cmd and mei in os.path.normcase(cmd)):
                    victims.append(proc)
            except Exception:
                pass
        if not victims:
            return
        for proc in victims:
            try:
                proc.terminate()
            except Exception:
                pass
        # 关键：必须**同步等待**子进程真正退出——terminate 是异步的，
        # 否则旧进程立刻 sys.exit 时 PyInstaller 清理 _MEI 仍会失败（登出重启高发）
        try:
            _, alive = psutil.wait_procs(victims, timeout=3)
        except Exception:
            alive = []
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            psutil.wait_procs(alive, timeout=2)
        except Exception:
            pass
    except Exception:
        pass


def cleanup_stale_mei_dirs(max_age_seconds=900):
    """清理临时目录中残留的 PyInstaller 解压目录（_MEI*）。

    onefile 每次启动解压约 700MB，正常退出由引导器自动删除；但崩溃/被杀/
    解压失败会留下残留，累积会占满系统盘，导致之后启动时解压大文件
    （Qt5WebEngineCore.dll）失败（"Failed to extract ..." 弹窗，无法启动）。
    只删除：非本进程、无任何进程引用（环境变量/加载模块/打开文件/工作目录）、
    且修改时间超过 max_age_seconds 的目录。返回删除的目录数。"""
    if not getattr(sys, 'frozen', False) or not getattr(sys, '_MEIPASS', None):
        return 0
    try:
        import psutil
    except Exception:
        return 0
    try:
        meipass = os.path.abspath(sys._MEIPASS)
        temp_root = os.path.dirname(meipass)
        temp_nc = os.path.normcase(temp_root)
        mine = os.path.normcase(meipass)
        names = os.listdir(temp_root)
    except Exception:
        return 0

    # 收集所有进程引用到的 _MEI 目录（环境变量/工作目录/加载模块/打开的文件）。
    # 性能：memory_maps/open_files 很昂贵，先按进程名粗筛，只详查本程序相关进程，
    # 避免启动时对系统全部进程逐一枚举造成卡顿和 CPU 占用。
    in_use = set()
    name_keys = ('流萤', 'firefly', 'python')
    for p in psutil.process_iter(['pid', 'name']):
        try:
            pname = (p.info.get('name') or '').lower()
        except Exception:
            continue
        if not any(k in pname for k in name_keys):
            continue
        candidates = []
        try:
            candidates.append(p.cwd() or '')
        except Exception:
            pass
        try:
            for m in p.memory_maps():
                candidates.append(m[0])
        except Exception:
            pass
        try:
            for f in p.open_files():
                candidates.append(f[0])
        except Exception:
            pass
        try:
            home = p.environ().get('_PYI_APPLICATION_HOME_DIR')
            if home:
                candidates.append(home)
        except Exception:
            pass
        for s in candidates:
            s = os.path.normcase(os.path.abspath(s)) if s else ''
            if s.startswith(temp_nc + os.sep + '_mei'):
                in_use.add(s[len(temp_nc) + 1:].split(os.sep)[0])

    now = time.time()
    removed = 0
    for name in names:
        if not name.startswith('_MEI') or name.lower() in in_use:
            continue
        path = os.path.join(temp_root, name)
        if not os.path.isdir(path) or os.path.normcase(os.path.abspath(path)) == mine:
            continue
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed += 1
    if removed:
        try:
            print(f"[清理] 已清理 {removed} 个残留解压目录")
        except Exception:
            pass
    return removed

def _spawn_env():
    """返回用于启动新实例的环境变量：剔除 PyInstaller 注入的变量。

    否则新实例会直接寄生在旧进程的 _MEI 解压目录（跳过自己的解压），
    旧进程退出清理 _MEI 后，新实例会因资源/子进程加载失败而在 Qt 虚函数里
    抛异常，被 PyQt 直接 qFatal/abort（进程无日志消失）。
    """
    env = os.environ.copy()
    for var in ('_PYI_APPLICATION_HOME_DIR', '_PYI_PARENT_PROCESS_LEVEL',
                '_PYI_ARCHIVE_FILE', '_MEIPASS', '_MEIPASS2'):
        env.pop(var, None)
    return env


def _hard_exit(code=0):
    """立即退出进程：先结束 _MEI 内的子进程，再直接 os._exit。

    不使用 sys.exit（Qt 槽内抛 SystemExit 有风险），也不让 Qt 继续走退场流程——
    子进程已被结束，Qt 收尾时若访问已结束的渲染进程会**原生崩溃**（无 Python 日志）。
    退出后由 PyInstaller 引导器（父进程）清理 _MEI 临时目录。
    """
    try:
        cleanup_mei_children()
    except Exception:
        pass
    try:
        app = QApplication.instance()
        if app is not None:
            app.quit()
    except Exception:
        pass
    os._exit(code)


SIZE_MARKER_NAMES = ("Large", "small", "normal", "medium")

def get_size_marker_path(name):
    """尺寸标记文件的稳定存放路径（可写目录），避免写入当前工作目录"""
    return os.path.join(get_writable_dir(), name)

def list_size_markers():
    """返回当前存在的尺寸标记文件名列表"""
    return [n for n in SIZE_MARKER_NAMES if os.path.exists(get_size_marker_path(n))]

os.environ["QTWEBENGINE_DISABLE_SHADER_DISK_CACHE"] = "1"

def _log_exception(exc_info=None):
    """只把异常写入崩溃日志（不弹窗、不退出）。

    用于 Qt 虚函数/事件分发内的异常：这类异常若冒泡回 C++，PyQt 会直接
    qFatal/abort（表现为"进程直接消失且没有任何日志"），因此必须就地兜住。
    """
    try:
        exc_type, exc_value, exc_tb = exc_info or sys.exc_info()
        with open(os.path.join(get_writable_dir(), "firefly_crash.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now()} (已兜住，未中断) ===\n")
            f.write(''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    except Exception:
        pass


def global_exception_hook(exc_type, exc_value, exc_tb):
    """全局未捕获异常钩子 - 加固版，优先使用原有资源，失败时降级"""
    log_path = os.path.join(get_writable_dir(), "firefly_crash.log")

    # ========== 1. 记录原始异常到日志 ==========
    msg = f"{exc_type}: {exc_value}"
    try:
        msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now()} ===\n")
            f.write(msg)
    except Exception:
        pass

    # ========== 1.5 非 GUI 线程：绝不创建 Qt 窗口（跨线程建窗口会白屏卡死） ==========
    try:
        if threading.current_thread() is not threading.main_thread():
            # 改用原生 Win32 消息框，任何线程都可安全调用
            try:
                ctypes.windll.user32.MessageBoxW(
                    None, str(msg)[:2000], "流萤桌宠 - 严重错误", 0x10)
            except Exception:
                pass
            return
    except Exception:
        pass

    # ========== 2. 定义显示弹窗的核心函数 ==========
    def show_crash_dialog():
        """显示崩溃对话框，优先使用项目资源，失败则降级"""
        try:
            # 确保 QApplication 存在（若没有则临时创建）
            app = QApplication.instance()
            if app is None:
                app = QApplication([])
                app.setQuitOnLastWindowClosed(False)

            # 创建对话框
            dialog = QDialog()
            dialog.setWindowTitle("严重错误")
            dialog.setFixedSize(520, 220)
            dialog.setWindowFlags(
                Qt.Dialog |
                Qt.WindowCloseButtonHint |
                Qt.WindowStaysOnTopHint
            )

            # ---------- 尝试加载原有图标 ----------
            try:
                icon_path = asset_path("images", "firefly", "Sadness", "abandoned.ico")
                if os.path.exists(icon_path):
                    pixmap = QPixmap(icon_path)
                    if not pixmap.isNull():
                        dialog.setWindowIcon(QIcon(pixmap))
                    else:
                        dialog.setWindowIcon(QIcon())
                else:
                    dialog.setWindowIcon(QIcon())
            except Exception:
                pass

            # ---------- 应用原有样式表 ----------
            try:
                style = """
                    QDialog {
                        background-color: #1E1E1E;
                        border-radius: 10px;
                        border: 1px solid #444;
                    }
                    QLabel {
                        color: #CCCCCC;
                        font-size: 14px;
                    }
                    QPushButton {
                        background-color: #4CAF50;
                        color: white;
                        border: none;
                        border-radius: 8px;
                        padding: 8px 16px;
                        font-size: 14px;
                    }
                    QPushButton:hover {
                        background-color: #45A049;
                    }
                    QPushButton#open_log_btn {
                        background-color: #FF9800;
                    }
                    QPushButton#open_log_btn:hover {
                        background-color: #F57C00;
                    }
                """
                dialog.setStyleSheet(style)
            except Exception:
                dialog.setStyleSheet("""
                    QDialog { background-color: #1E1E1E; }
                    QLabel { color: #CCCCCC; }
                    QPushButton { background-color: #4CAF50; color: white; }
                """)

            # ---------- 布局 ----------
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.setSpacing(15)

            # ---------- 图标 ----------
            icon_label = QLabel()
            try:
                icon_path = asset_path("images", "firefly", "Sadness", "abandoned.ico")
                if os.path.exists(icon_path):
                    pixmap = QPixmap(icon_path)
                    if not pixmap.isNull():
                        pixmap = pixmap.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        icon_label.setPixmap(pixmap)
                    else:
                        icon_label.setText("⚠️")
                        icon_label.setStyleSheet("font-size: 24px;")
                else:
                    icon_label.setText("⚠️")
                    icon_label.setStyleSheet("font-size: 24px;")
            except Exception:
                icon_label.setText("⚠️")
                icon_label.setStyleSheet("font-size: 24px;")
            icon_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(icon_label, alignment=Qt.AlignCenter)

            # ---------- 标题 ----------
            title_label = QLabel("流萤桌宠发生了严重的崩溃！")
            title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #FF5555;")
            title_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(title_label)

            # ---------- 提示信息 ----------
            info_label = QLabel("如果要请求别人帮助，请提供完整日志而不是此截图。")
            info_label.setWordWrap(True)
            info_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(info_label)

            # ---------- 按钮 ----------
            btn_layout = QHBoxLayout()
            ok_btn = QPushButton("确定")
            ok_btn.clicked.connect(dialog.accept)
            open_log_btn = QPushButton("打开日志")
            open_log_btn.setObjectName("open_log_btn")
            open_log_btn.clicked.connect(
                lambda: os.startfile(log_path) if os.path.exists(log_path) else None
            )
            btn_layout.addStretch()
            btn_layout.addWidget(ok_btn)
            btn_layout.addWidget(open_log_btn)
            layout.addLayout(btn_layout)

            # ---------- 居中显示 ----------
            screen = QApplication.primaryScreen().availableGeometry()
            dialog.move(screen.center() - dialog.rect().center())
            dialog.raise_()
            dialog.activateWindow()
            dialog.exec_()

        except Exception as e:
            # 如果 Qt 弹窗完全失败，使用 Windows 原生消息框
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"程序发生严重崩溃！\n日志已保存至：{log_path}\n\n请将日志发送给开发者。",
                    "流萤桌宠 - 严重错误",
                    0x10 | 0x00040000
                )
            except:
                pass
            # 最后打印到控制台
            print("严重错误：无法显示崩溃对话框，原始异常：")
            traceback.print_exc()

    # ========== 3. 同步显示对话框（不依赖主事件循环） ==========
    try:
        show_crash_dialog()
    except Exception:
        # 极端情况：Qt 完全不可用，回退到原生消息框
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"程序发生严重崩溃！\n日志已保存至：{log_path}\n\n请将日志发送给开发者。",
                "流萤桌宠 - 严重错误",
                0x10 | 0x00040000
            )
        except:
            pass

    # 最终退出程序
    sys.exit(1)

# ========== 自定义 QApplication 捕获 Qt 事件异常 ==========
class MyApplication(QApplication):
    def notify(self, receiver, event):
        try:
            return super().notify(receiver, event)
        except Exception:
            # 事件处理中的异常：只写日志，绝不弹窗/退出——全局钩子里的 sys.exit
            # 若从 notify（虚函数）里抛出，PyQt 会直接 qFatal/abort，进程无日志消失
            _log_exception(sys.exc_info())
            return False

def show_resource_missing_and_exit(missing_items):
    """显示资源缺失/损坏弹窗并退出程序"""
    app = QApplication.instance()
    if app:
        msg_box = QMessageBox()
        msg_box.setWindowTitle("资源错误")
        msg_box.setIcon(QMessageBox.Critical)
        msg_box.setText("程序资源损坏或缺失，无法继续运行！")
        msg_box.setInformativeText("问题资源：\n" + "\n".join(missing_items))
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #CCCCCC;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg_box.move(screen_center - msg_box.rect().center())
        msg_box.exec_()
    sys.exit(1)

def check_essential_resources():
    """检查核心资源是否存在且有效（避免空文件或损坏文件）"""
    missing = []
    invalid = []

    # 检查必要的目录
    required_dirs = [
        os.path.join(get_base_dir(), "assets"),
        os.path.join(get_base_dir(), "assets", "images", "firefly", "actions", "Standby"),
        os.path.join(get_base_dir(), "assets", "images", "firefly", "actions", "Discomfort"),
        os.path.join(get_base_dir(), "assets", "font"),
        os.path.join(get_base_dir(), "assets", "images", "icon"),
        os.path.join(get_base_dir(), "assets", "images", "foods"),
        os.path.join(get_base_dir(), "mouse", "Firefly"),
        os.path.join(get_base_dir(), "mouse", "Sam"),
    ]
    for d in required_dirs:
        if not os.path.exists(d):
            missing.append(f"目录缺失: {d}")
        elif not os.path.isdir(d):
            invalid.append(f"路径不是目录: {d}")

    # 检查关键文件（存在性、大小、有效性）
    required_files = [
        (os.path.join(get_base_dir(), "assets", "font", "HarmonyOS_Sans_SC_Bold.ttf"), "字体文件", None),
        (os.path.join(get_base_dir(), "assets", "images", "icon", "Login_img.png"), "登录图片", "image"),
        (os.path.join(get_base_dir(), "assets", "images", "star_rail_ticket.png"), "星铁专票图片", "image"),
        (os.path.join(get_base_dir(), "assets", "images", "firefly", "Sadness", "abandoned.ico"), "图标文件", "image"),
        (os.path.join(get_base_dir(), "assets", "images", "foods", "Cake.png"), "蛋糕图片", "image"),
    ]

    for filepath, desc, file_type in required_files:
        if not os.path.exists(filepath):
            missing.append(f"文件缺失: {desc} ({filepath})")
            continue
        size = os.path.getsize(filepath)
        if size == 0:
            invalid.append(f"文件为空: {desc} ({filepath})")
            continue
        if file_type == "image":
            try:
                pixmap = QPixmap(filepath)
                if pixmap.isNull():
                    invalid.append(f"图片无法加载(损坏): {desc} ({filepath})")
            except Exception:
                invalid.append(f"图片加载异常: {desc} ({filepath})")

    # 检查动画 GIF 文件（至少一个有效）
    action_dirs = [
        (os.path.join(get_base_dir(), "assets", "images", "firefly", "actions", "Standby"), "Standby动画"),
        (os.path.join(get_base_dir(), "assets", "images", "firefly", "actions", "Discomfort"), "Discomfort动画"),
    ]
    for ad, name in action_dirs:
        if os.path.exists(ad):
            gif_files = [f for f in os.listdir(ad) if f.lower().endswith('.gif')]
            if not gif_files:
                invalid.append(f"{name}目录下没有GIF文件")
            else:
                valid_gif = False
                for gf in gif_files:
                    gif_path = os.path.join(ad, gf)
                    try:
                        movie = QMovie(gif_path)
                        if movie.frameCount() > 0:
                            valid_gif = True
                            break
                    except:
                        pass
                if not valid_gif:
                    invalid.append(f"{name}目录下的GIF文件均无效或损坏")

    if missing or invalid:
        all_issues = []
        if missing:
            all_issues.append("缺失项:")
            all_issues.extend(missing)
        if invalid:
            all_issues.append("\n损坏/无效项:")
            all_issues.extend(invalid)
        show_resource_missing_and_exit(all_issues)

sys.excepthook = global_exception_hook

def _thread_exception_hook(args):
    """子线程未捕获异常也交给同一钩子处理（内部会安全降级为原生消息框）"""
    global_exception_hook(args.exc_type, args.exc_value, args.exc_traceback)

try:
    threading.excepthook = _thread_exception_hook
except Exception:
    pass

# Windows 常量
WM_HOTKEY = 0x0312
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_TAB = 0x09

CLIENT_VERSION = "4.7.0"

def parse_version(ver_str):
    """同服务端定义"""
    if not ver_str:
        return None, None
    match = re.search(r'(\d+(?:\.\d+){1,2})', ver_str)
    if not match:
        return None, None
    num_part = match.group(1)
    num_tuple = tuple(map(int, num_part.split('.')))
    suffix = None
    if '-' in ver_str:
        suffix = ver_str.split('-', 1)[1].strip()
        if not suffix:
            suffix = None
    return num_tuple, suffix

def versions_compatible(client_ver, server_ver):
    """
    检查客户端版本与服务端版本是否兼容。
    仅比较数字部分（如 4.4.1），忽略后缀（如 -beta）。
    """
    c_num, _ = parse_version(client_ver)
    s_num, _ = parse_version(server_ver)
    if c_num is None or s_num is None:
        return False
    return c_num == s_num

def is_placeholder_player_name(name):
    """占位/已注销玩家名：不应出现在任何玩家名单（聊天对象/玩家列表）里。

    - "账号已注销#编号"：账号销毁后的显示名；
    - "开拓者-user-XXXXXX" / "开拓者-其它"：历史遗留的占位名
      （现在客户端与服务端都不允许注册/改名为这种）。
    """
    n = str(name or '').strip()
    if not n:
        return False
    if n.startswith('账号已注销'):
        return True
    if n.startswith('开拓者-'):
        return True
    return False


def fit_4_3(w, h):
    """返回不小于 (w, h) 的最近 4:3 尺寸。

    只放大不缩小（保证原内容不被挤压）：先按高度算宽度，不够宽就按宽度反推高度。
    菜单、登录/注册窗口、桌宠本体窗口不做处理。
    """
    try:
        w, h = int(w), int(h)
    except Exception:
        return w, h
    W = max(w, int(round(h * 4.0 / 3.0)))
    H = int(round(W * 3.0 / 4.0))
    if H < h:
        H = h
        W = int(round(H * 4.0 / 3.0))
    return W, H

def show_notification_async(title, message, icon_path=None, duration=3):
    """
    弹出系统通知。

    优先使用 **Qt 托盘气泡**（QSystemTrayIcon.showMessage，稳定、不依赖 win10toast）；
    托盘不可用或非 GUI 线程时退回 win10toast，且必须 **threaded=False**——
    win10toast 在 threaded=True 时会另起内部线程，异常发生在那个线程里，
    本函数外的 try/except 罩不住（本机实测报 classAtom / WPARAM 错误）。
    """
    # 1) Qt 托盘气泡（仅 GUI 线程可安全调用）
    try:
        if threading.current_thread() is threading.main_thread():
            app = QApplication.instance()
            firefly = getattr(app, 'firefly', None) if app is not None else None
            tray = getattr(firefly, 'tray_icon', None) if firefly is not None else None
            if tray is not None:
                tray.showMessage(title, message, QSystemTrayIcon.Information,
                                 max(1, int(duration)) * 1000)
                return
    except Exception as e:
        print(f"[通知] 托盘气泡失败，改用系统通知: {e}")

    # 2) win10toast 兜底：在本函数自己的线程里同步调用，异常可被捕获
    def _show():
        try:
            toaster = ToastNotifier()
            toaster.show_toast(
                title,
                message,
                icon_path=icon_path,
                duration=duration,
                threaded=False
            )
        except Exception as e:
            print(f"[通知] 弹出系统通知失败（已忽略）: {e}")
    threading.Thread(target=_show, daemon=True).start()

def create_volume_menu(parent):
    widget = QWidget()
    widget.setMinimumWidth(250)
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(12, 4, 12, 4)
    layout.setSpacing(8)

    label = QLabel("🔊")
    slider = QSlider(Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(int(parent.volume * 100))
    slider.setFixedWidth(160)

    value_label = QLabel(f"{int(parent.volume * 100)}%")
    value_label.setFixedWidth(45)
    value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    # 修改颜色为黑色，适配白色菜单背景
    value_label.setStyleSheet("color: #000000; font-size: 18px;")

    slider.valueChanged.connect(lambda v: parent.set_volume(v / 100.0))
    slider.valueChanged.connect(lambda v: value_label.setText(f"{v}%"))

    layout.addWidget(label)
    layout.addWidget(slider)
    layout.addWidget(value_label)

    action = QWidgetAction(parent)
    action.setDefaultWidget(widget)
    return action

def set_window_emoji_icon(window, emoji: str, size: int = 24):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    font = QFont("Segoe UI Emoji", size - 4)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, emoji)
    painter.end()
    window.setWindowIcon(QIcon(pixmap))

pluginsPath = 'plugins'
if os.path.exists(pluginsPath):
    QApplication.addLibraryPath(pluginsPath)

# ====================== 用户数据配置 ======================
USER_DATA_DIR = os.path.join(get_writable_dir(), "user_data")
USER_CONFIG = os.path.join(USER_DATA_DIR, "user_login.json")
CODES_FILE = os.path.join(USER_DATA_DIR, "redeem_codes.json")
if not os.path.exists(USER_DATA_DIR):
    os.makedirs(USER_DATA_DIR)

# ====================== 时装定义 ======================
AVAILABLE_CLOTHES = {
    "normal": {
        "name": "普通时装",
        "source_dir": "clothes/normal/assets",
        "description": "默认服装"
    },
    "26_Newyear": {
        "name": "柿柿如意",
        "source_dir": "clothes/new_year/assets",
        "description": "2026 春节签到获得"
    }
}

# ====================== 统一菜单样式 ======================
MENU_STYLE = """
    QMenu{
        border: 1px solid #dbdbdb;
        background-color: #ffffff;
        padding: 4px;
        border-radius: 6px;
    }
    QMenu::item {
        padding: 8px 24px;          /* 适当内边距，无外边距 */
        margin: 0px;                /* 取消外边距 */
        border-radius: 4px;
        background-color: transparent;
    }
    QMenu::item:selected{
        background-color: LightSkyBlue;
        color: #1E90FF;
    }
    QMenu::separator{
        height: 1px;
        background-color: #e9ecef;
        margin: 4px 0px;
    }
"""
def safe_load_json_with_recovery(filepath, default_data=None):
    """
    安全加载 JSON 文件。如果文件损坏，尝试从内容中恢复关键字段。
    若恢复失败，则使用默认数据创建并写入文件。
    返回解析后的数据字典。
    """
    if not os.path.exists(filepath):
        return None

    # 尝试多种编码读取
    encodings = ['utf-8-sig', 'utf-8', 'gbk', 'latin-1']
    raw_content = None
    for enc in encodings:
        try:
            with open(filepath, 'r', encoding=enc) as f:
                raw_content = f.read()
            break
        except:
            continue
    if raw_content is None:
        # 读取失败，备份并返回默认数据
        backup_path = filepath + '.corrupted'
        try:
            shutil.copy2(filepath, backup_path)
        except:
            pass
        return _create_default_user_data(filepath)

    # 1. 尝试正常 JSON 解析
    try:
        data = json.loads(raw_content)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. 如果解析失败，尝试恢复数据
    backup_path = filepath + '.corrupted'
    try:
        shutil.copy2(filepath, backup_path)
        print(f"[修复] 损坏文件已备份至 {backup_path}")
    except Exception:
        pass

    # 用正则提取常见字段（包括 password）
    recovered = {}
    string_patterns = {
        'uid': r'"uid":\s*"([^"]*)"',
        'username': r'"username":\s*"([^"]*)"',
        'userid': r'"userid":\s*"([^"]*)"',
        'password': r'"password":\s*"([^"]*)"',   # ← 关键新增
        'birthday': r'"birthday":\s*"([^"]*)"',
        'register_time': r'"register_time":\s*"([^"]*)"',
        'last_sign_date': r'"last_sign_date":\s*"([^"]*)"',
        'banned_until': r'"banned_until":\s*"([^"]*)"',
        'current_clothes': r'"current_clothes":\s*"([^"]*)"',
        'last_state': r'"last_state":\s*"([^"]*)"',
        'current_month': r'"current_month":\s*"([^"]*)"',
    }
    for key, pattern in string_patterns.items():
        match = re.search(pattern, raw_content)
        if match:
            recovered[key] = match.group(1)

    int_patterns = {
        'balance': r'"balance":\s*(\d+)',
        'credit': r'"credit":\s*(\d+)',
        'jade': r'"jade":\s*(\d+)',
        'star_rail_tickets': r'"star_rail_tickets":\s*(\d+)',
        'total_interactions': r'"total_interactions":\s*(\d+)',
        'total_open_count': r'"total_open_count":\s*(\d+)',
        'total_sign_days': r'"total_sign_days":\s*(\d+)',
        'monthly_credit_gained': r'"monthly_credit_gained":\s*(\d+)',
        'monthly_cake_gained': r'"monthly_cake_gained":\s*(\d+)',
    }
    for key, pattern in int_patterns.items():
        match = re.search(pattern, raw_content)
        if match:
            recovered[key] = int(match.group(1))

    bool_patterns = {
        'banned': r'"banned":\s*(true|false)',
        'allow_move': r'"allow_move":\s*(true|false)',
    }
    for key, pattern in bool_patterns.items():
        match = re.search(pattern, raw_content, re.IGNORECASE)
        if match:
            recovered[key] = match.group(1).lower() == 'true'

    # 如果恢复后没有任何字段，则返回默认数据
    if not recovered:
        return _create_default_user_data(filepath)

    # 构造默认数据（与 ensure_user_fields 一致）
    default = {
        "uid": "user-" + str(random.randint(0, 10**9 - 1)).zfill(9),
        "username": "用户",
        "userid": "user",
        "password": "",
        "birthday": "2000-01-01",
        "register_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "balance": 20,
        "credit": 0,
        "jade": 0,
        "star_rail_tickets": 0,
        "banned": False,
        "banned_until": None,
        "redeemed_codes": [],
        "redeem_history": [],
        "cursor_style": "None",
        "pet_size": "normal",
        "owned_clothes": ["normal"],
        "mailbox": [],
        "last_birthday_greet_year": None,
        "last_feed_time": None,
        "volume": 0.5,
        "allow_move": False,
        "total_interactions": 0,
        "total_open_count": 0,
        "total_sign_days": 0,
        "total_affection_up": 0,
        "total_sleep_count": 0,
        "total_chat_count": 0,
        "monthly_interactions": 0,
        "monthly_open_count": 0,
        "monthly_sign_days": 0,
        "monthly_affection_up": 0,
        "monthly_sleep_count": 0,
        "monthly_chat_count": 0,
        "monthly_credit_gained": 0,
        "monthly_cake_gained": 0,
        "monthly_ai_click_count": 0,
        "current_month": None,
        "current_clothes": "normal",
        "last_change_name_month": None,
        "change_name_count": 0,
        "monthly_jade_gained": 0,
        "total_jade_gained": 0,
        "monthly_ticket_gained": 0,
        "total_ticket_gained": 0,
        "birthday_reward_year": None,
        "last_achievement_month": None,
        "custom_items": {},
        "last_state": "Standby",
        "last_x": None,
        "last_y": None,
    }

    # 合并恢复的数据
    for key, value in recovered.items():
        if key in default:
            default[key] = value
        else:
            default[key] = value

    # 确保关键字段存在
    if not default.get('userid'):
        default['userid'] = os.path.basename(filepath)[:-5]
    if not default.get('username'):
        default['username'] = default['userid']
    if not default.get('uid'):
        default['uid'] = "user-" + str(random.randint(0, 10**9 - 1)).zfill(9)
    # 如果恢复后密码仍为空，且是 admin 账号，设置默认密码为 "admin"（方便登录）
    if default.get('userid') == 'admin' and not default.get('password'):
        default['password'] = 'admin'

    # 写入修复后的文件
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        print(f"[修复] 已恢复并写入 {filepath}")
        return default
    except Exception as e:
        print(f"[修复] 写入失败: {e}")
        return _create_default_user_data(filepath)

def _create_default_user_data(filepath):
    """根据文件路径创建默认用户数据（用于严重损坏时）"""
    userid = os.path.basename(filepath)[:-5]  # 去掉 .json
    # 对于 admin 账号，设置默认密码为 "admin"，以便登录
    default_password = "admin" if userid == "admin" else ""
    default = {
        "uid": "user-" + str(random.randint(0, 10**9 - 1)).zfill(9),
        "username": userid,
        "userid": userid,
        "password": default_password,
        "birthday": "2000-01-01",
        "register_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "balance": 20,
        "credit": 0,
        "jade": 0,
        "star_rail_tickets": 0,
        "banned": False,
        "banned_until": None,
        "redeemed_codes": [],
        "redeem_history": [],
        "cursor_style": "None",
        "pet_size": "normal",
        "owned_clothes": ["normal"],
        "mailbox": [],
        "last_birthday_greet_year": None,
        "last_feed_time": None,
        "volume": 0.5,
        "allow_move": False,
        "total_interactions": 0,
        "total_open_count": 0,
        "total_sign_days": 0,
        "total_affection_up": 0,
        "total_sleep_count": 0,
        "total_chat_count": 0,
        "monthly_interactions": 0,
        "monthly_open_count": 0,
        "monthly_sign_days": 0,
        "monthly_affection_up": 0,
        "monthly_sleep_count": 0,
        "monthly_chat_count": 0,
        "monthly_credit_gained": 0,
        "monthly_cake_gained": 0,
        "monthly_ai_click_count": 0,
        "current_month": None,
        "current_clothes": "normal",
        "last_change_name_month": None,
        "change_name_count": 0,
        "monthly_jade_gained": 0,
        "total_jade_gained": 0,
        "monthly_ticket_gained": 0,
        "total_ticket_gained": 0,
        "birthday_reward_year": None,
        "last_achievement_month": None,
        "custom_items": {},
        "last_state": "Standby",
        "last_x": None,
        "last_y": None,
    }
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        print(f"[修复] 已创建默认用户数据文件 {filepath}，密码: {default_password}")
    except Exception as e:
        print(f"[修复] 创建默认文件失败: {e}")
    return default

def ensure_user_fields(user_data, filepath=None):
    modified = False
    defaults = {
        "custom_items": {},
        "balance": 20,
        "jade": 0,
        "credit": 99999 if user_data.get("uid", "").lower().startswith("trailblazer") else 0,
        "last_sign_date": None,
        "banned": False,
        "banned_until": None,
        "redeemed_codes": [],
        "redeem_history": [],
        "cursor_style": "None",
        "pet_size": "normal",
        "owned_clothes": ["normal"],
        "last_state": "Standby",
        "mailbox": [],
        "last_x": None,
        "last_y": None,
        "star_rail_tickets": 0,
        "last_birthday_greet_year": None,
        "last_feed_time": None,
        "volume": 0.5,
        "allow_move": False,
        "total_interactions": 0,
        "total_open_count": 0,
        "total_sign_days": 0,
        "total_affection_up": 0,
        "total_sleep_count": 0,
        "total_chat_count": 0,
        "monthly_interactions": 0,
        "monthly_open_count": 0,
        "monthly_sign_days": 0,
        "monthly_affection_up": 0,
        "monthly_sleep_count": 0,
        "monthly_chat_count": 0,
        "monthly_credit_gained": 0,
        "monthly_cake_gained": 0,
        "monthly_ai_click_count": 0,
        "current_month": None,
        "current_clothes": "normal",
        "last_change_name_month": None,
        "change_name_count": 0,
        "monthly_jade_gained": 0,
        "total_jade_gained": 0,
        "monthly_ticket_gained": 0,
        "total_ticket_gained": 0,
        'birthday_reward_year': None,
        'last_achievement_month': None,
    }

    for key, default_val in defaults.items():
        if key not in user_data:
            user_data[key] = default_val
            modified = True
    if not isinstance(user_data.get("owned_clothes"), list):
        user_data["owned_clothes"] = ["normal"]
        modified = True
    if not isinstance(user_data.get("mailbox"), list):
        user_data["mailbox"] = []
        modified = True
    if modified and filepath:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(user_data, f, ensure_ascii=False, indent=2)
    return user_data

def load_all_codes():
    if not os.path.exists(CODES_FILE):
        default_codes = {
            "Firefly": {
                "reward": 10,
                "max_uses": 1,
                "used_count": 0,
                "created_by": "system",
                "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "target_user": None,
                "item_type": "cake",
                "expire_time": None,
                "revoked": False,
                "revoked_users": [],
                "revoked_by": None,
                "revoked_time": None,
                "clothes_id": None
            }
        }
        with open(CODES_FILE, "w", encoding="utf-8") as f:
            json.dump(default_codes, f, indent=2)
        return default_codes
    with open(CODES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        for code, info in data.items():
            if "items" not in info:
                itype = info.get("item_type", "cake")
                reward = info.get("reward", 0)
                cid = info.get("clothes_id")
                if itype == "cake":
                    info["items"] = [{"type": "cake", "amount": reward}]
                elif itype == "clothes":
                    info["items"] = [{"type": "clothes", "id": cid}]
                elif itype == "credit":
                    info["items"] = [{"type": "credit", "amount": reward}]
            info.setdefault("expire_time", None)
            info.setdefault("revoked", False)
            info.setdefault("revoked_users", [])
            info.setdefault("revoked_by", None)
            info.setdefault("revoked_time", None)
        return data

def save_all_codes(codes):
    with open(CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, indent=2)

def parse_duration(duration_str):
    pattern = re.compile(r'(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?')
    match = pattern.match(duration_str.upper())
    if not match:
        return None
    years = int(match.group(1)) if match.group(1) else 0
    months = int(match.group(2)) if match.group(2) else 0
    days = int(match.group(3)) if match.group(3) else 0
    total_days = years * 365 + months * 30 + days
    return timedelta(days=total_days)

def parse_expire_time(expire_str):
    if not expire_str:
        return None
    try:
        dt = datetime.strptime(expire_str, "%Y-%m-%d")
        return dt.isoformat()
    except:
        pass
    delta = parse_duration(expire_str)
    if delta is not None:
        expire_dt = datetime.now() + delta
        return expire_dt.isoformat()
    return None

def parse_time_spec(spec):
    """
    解析时间规格，支持：
    - 时长格式（单位可任意组合）：
        年：Y 或 yr
        月：M
        日：D 或 d
        时：H 或 hr
        分：m 或 min
        秒：s 或 sec
        示例：1Y2M3D4H5m6s 或 1yr 2mo ... (但mo未启用，可用M)
    - 纯数字：视为天数
    - 绝对日期：2026-12-31 或 2026-12-31 23:59:59
    返回 (timedelta, None) 或 (None, datetime) 或 (None, None)
    """
    if not spec:
        return None, None
    spec = spec.strip()

    # 1. 尝试绝对日期时间 (YYYY-MM-DD HH:MM:SS)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})$', spec)
    if match:
        y, m, d, h, min, sec = map(int, match.groups())
        try:
            dt = datetime(y, m, d, h, min, sec)
            return None, dt
        except:
            pass
    # 2. 绝对日期 (YYYY-MM-DD)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', spec)
    if match:
        y, m, d = map(int, match.groups())
        try:
            dt = datetime(y, m, d, 23, 59, 59)
            return None, dt
        except:
            pass

    # 3. 时长格式（支持大小写区分版和纯小写备选版）
    # 单位表：只要不与另一单位冲突就大小写都收（D/d=天、H/h=小时、Y/y=年、S/s=秒），
    # 并收下中文单位（天/日/小时/时/分/分钟/月/年/秒）。
    # 仍区分大小写的只有一对：'M'=月、'm'=分钟（两个都常用，不能混）。
    units = {
        'Y': 'years', 'y': 'years', 'yr': 'years', 'yrs': 'years', 'year': 'years', 'years': 'years', '年': 'years',
        'M': 'months', 'mo': 'months', 'mon': 'months', 'month': 'months', 'months': 'months', '月': 'months',
        'D': 'days', 'd': 'days', 'day': 'days', 'days': 'days', '天': 'days', '日': 'days',
        'H': 'hours', 'h': 'hours', 'hr': 'hours', 'hrs': 'hours', 'hour': 'hours', 'hours': 'hours',
        '小时': 'hours', '时': 'hours',
        'm': 'minutes', 'min': 'minutes', 'mins': 'minutes', 'minute': 'minutes', 'minutes': 'minutes',
        '分': 'minutes', '分钟': 'minutes',
        's': 'seconds', 'sec': 'seconds', 'secs': 'seconds', 'second': 'seconds', 'seconds': 'seconds', '秒': 'seconds',
    }
    # 按长度降序排列，避免 `yr` 被 `y` 误匹配
    unit_pattern = '|'.join(sorted(units.keys(), key=len, reverse=True))
    pattern = re.compile(r'(\d+)(' + unit_pattern + r')')
    matches = pattern.findall(spec)

    if not matches:
        # 纯数字作为天数
        if spec.isdigit():
            return timedelta(days=int(spec)), None
        return None, None

    years = months = days = hours = minutes = seconds = 0
    for num_str, unit in matches:
        num = int(num_str)
        kind = units.get(unit)
        if kind == 'years':
            years += num
        elif kind == 'months':
            months += num
        elif kind == 'days':
            days += num
        elif kind == 'hours':
            hours += num
        elif kind == 'minutes':
            minutes += num
        elif kind == 'seconds':
            seconds += num
        else:
            return None, None

    # 累加为 timedelta（1年=365天，1月=30天）
    total_days = years * 365 + months * 30 + days
    delta = timedelta(days=total_days, hours=hours, minutes=minutes, seconds=seconds)
    return delta, None

def resolve_expiry_dt(value):
    """把有效期字段解析成 datetime（解析不了返回 None）。

    兼容两类写法：
      · 绝对时间：ISO / '2026-12-31 23:59:59'（服务端下发的一律是这种）；
      · 相对写法：30D / 30d / 30天 / 2h / 30m（手填或旧存档里可能残留）。
    以前这两处只认绝对时间（datetime.fromisoformat），相对写法直接抛异常被 except 吞掉
    → 菜单/卡池显示不出剩余时间、到点也不会自动关闭。
    """
    if not value:
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except Exception:
            pass
        try:
            delta, until = parse_time_spec(v)
        except Exception:
            delta, until = None, None
        if until:
            return until
        if delta:
            return datetime.now() + delta
    return None

def clean_expired_mails(user_data):
    if "mailbox" not in user_data:
        return
    cutoff = datetime.now() - timedelta(days=180)
    new_mailbox = []
    for mail in user_data["mailbox"]:
        try:
            mail_time = datetime.fromisoformat(mail["timestamp"])
            if mail_time >= cutoff:
                new_mailbox.append(mail)
        except:
            new_mailbox.append(mail)
    user_data["mailbox"] = new_mailbox

def send_mail(self, target_uid, title, content, items):
    if not self.connected:
        return
    self._send({'type': 'send_mail', 'data': {'target_uid': target_uid, 'title': title, 'content': content, 'items': items}})

def claim_mail_items(user_data, mail):
    if mail["claimed"]:
        return False, []
    items_desc = []
    for item in mail["items"]:
        itype = item["type"]
        if itype == "cake":
            user_data["balance"] = user_data.get("balance", 0) + item["amount"]
            items_desc.append(f"橡木蛋糕卷 x{item['amount']}")
        elif itype == "credit":
            user_data["credit"] = user_data.get("credit", 0) + item["amount"]
            if user_data["credit"] > 99999999:
                user_data["credit"] = 99999999
            items_desc.append(f"信用点 x{item['amount']}")
        elif itype == "clothes":
            clothes_id = item["id"]
            if clothes_id in AVAILABLE_CLOTHES:
                owned = user_data.get("owned_clothes", ["normal"])
                if clothes_id not in owned:
                    owned.append(clothes_id)
                    user_data["owned_clothes"] = owned
                    items_desc.append(f"时装 [{AVAILABLE_CLOTHES[clothes_id]['name']}]")
        elif itype == "star_rail_ticket":
            user_data["star_rail_tickets"] = user_data.get("star_rail_tickets", 0) + item["amount"]
            # 新增统计（邮件领取计入专票统计）
            user_data["monthly_ticket_gained"] = user_data.get("monthly_ticket_gained", 0) + item["amount"]
            user_data["total_ticket_gained"] = user_data.get("total_ticket_gained", 0) + item["amount"]
            items_desc.append(f"星铁专票 x{item['amount']}")
        elif itype == "jade":
            user_data["jade"] = user_data.get("jade", 0) + item["amount"]
            # 新增统计
            user_data["monthly_jade_gained"] = user_data.get("monthly_jade_gained", 0) + item["amount"]
            user_data["total_jade_gained"] = user_data.get("total_jade_gained", 0) + item["amount"]
            items_desc.append(f"星琼 x{item['amount']}")
        # ★ 新增 custom 处理
        elif itype == "custom":
            custom_items = user_data.setdefault('custom_items', {})
            custom_items[item['id']] = custom_items.get(item['id'], 0) + item['amount']
            items_desc.append(f"{item.get('name', item['id'])} x{item['amount']}")
    mail["claimed"] = True
    history = user_data.get("redeem_history", [])
    history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 邮件领取: {', '.join(items_desc) if items_desc else '空邮件'}")
    user_data["redeem_history"] = history
    return True, items_desc

class ConnectionThread(QThread):
    connection_success = pyqtSignal(dict)  # 改为传递整个 data 字典
    connection_failed = pyqtSignal(str)
    connection_closed = pyqtSignal()
    message_received = pyqtSignal(str)

    def __init__(self, host, port, userid, uid, name, country, version):
        super().__init__()
        self.host = host
        self.port = port
        self.userid = userid
        self.uid = uid
        self.name = name
        self.country = country
        self.version = version
        self.sock = None
        self._running = True
        # 登录安全相关（由上层注入）：本地保存的口令（只用于算 SCRAM 应答，不上线）、
        # 本机绑定 id、服务端在当前连接下发的 salt/迭代数
        self.password = getattr(self, 'password', '') or ''
        self.bind_id = getattr(self, 'bind_id', '') or ''
        self.auth_salt = ''
        self.auth_iterations = 120000
        self.server_bound = False
        self.login_ok_early = None

    def run(self):
        """子线程入口，执行所有网络操作"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(3)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)

            # ---------- 登录前握手（hello/nonce）----------
            # 新服务端支持：回 salt/nonce → 已绑定账号用 SCRAM 应答（不在线上传明文口令）；
            # 老服务端不支持：2.5 秒内收不到 hello_ok 就按老格式直接登录（兼容）。
            _auth = None
            _salt = ''
            try:
                self._send({'type': 'hello', 'data': {'uid': self.uid, 'userid': self.userid,
                                                       'version': self.version}})
                self.sock.settimeout(2.5)
                _buf = b''
                while b'\n' not in _buf:
                    _d = self.sock.recv(65536)
                    if not _d:
                        break
                    _buf += _d
                self.sock.settimeout(None)
                if b'\n' in _buf:
                    _line, _rest = _buf.split(b'\n', 1)
                    _msg = json.loads(_line.decode('utf-8', 'ignore') or '{}')
                    if _msg.get('type') == 'hello_ok':
                        _hd = _msg.get('data') or {}
                        _salt = str(_hd.get('salt') or '')
                        _nonce = str(_hd.get('nonce') or '')
                        self.auth_salt = _salt
                        self.auth_iterations = int(_hd.get('iterations') or 120000)
                        self.server_bound = bool(_hd.get('bound'))
                        try:
                            _ff2 = getattr(self, 'firefly', None)
                            if _ff2 is not None:      # 让上层也能拿到（/bind 时算校验值用）
                                _ff2.auth_salt = _salt
                                _ff2.auth_iterations = self.auth_iterations
                        except Exception:
                            pass
                        if _salt and _nonce and self.password:
                            # StoredKey = PBKDF2(口令, salt)；只把 HMAC(StoredKey, nonce) 发出去
                            try:
                                _sk = hashlib.pbkdf2_hmac(
                                    'sha256', str(self.password).encode('utf-8'),
                                    bytes.fromhex(_salt), self.auth_iterations)
                                import hmac as _hmac
                                _auth = {'proof': _hmac.new(
                                    _sk, _nonce.encode('utf-8'), hashlib.sha256).hexdigest()}
                            except Exception as _ae:
                                print(f"[握手] 计算应答失败: {_ae}")
                    elif _msg.get('type') == 'login_ok':
                        # 老服务端把 hello 当成了别的消息却直接放行登录：交给上层处理
                        self.login_ok_early = _msg.get('data') or {}
            except socket.timeout:
                pass
            except Exception as _he:
                print(f"[握手] 跳过（{_he}）")
            finally:
                try:
                    self.sock.settimeout(None)
                except Exception:
                    pass

            # 发送登录消息
            login_msg = {
                'type': 'login',
                'data': {
                    'userid': self.userid,
                    'uid': self.uid,
                    'name': self.name,
                    'country': self.country,
                    'version': self.version,
                    'bind_id': getattr(self, 'bind_id', '') or ''
                }
            }
            if _auth:
                login_msg['data']['auth'] = _auth
            self._send(login_msg)

            # 开始监听（持续）
            self._listen()
        except socket.timeout:
            self.connection_failed.emit("连接超时，请检查服务器地址和网络")
        except ConnectionRefusedError:
            self.connection_failed.emit("服务器拒绝连接（可能未启动或端口错误）")
        except Exception as e:
            self.connection_failed.emit(f"连接失败：{str(e)}")
        finally:
            if self.sock:
                self.sock.close()
            self.connection_closed.emit()

    def _send(self, obj):
        if not self.sock:
            return False
        try:
            data = (json.dumps(obj, ensure_ascii=False) + '\n').encode()
            self.sock.sendall(data)
            return True
        except Exception as e:
            # 套接字已失效（例如服务端被直接关掉/网络中断）：按"连接断开"处理，
            # 交给断开重连流程，而不是弹「连接失败」对话框
            print(f"[发送失败，按断开处理] {e}")
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            try:
                self.connection_closed.emit()
            except Exception:
                pass
            return False

    def _listen(self):
        """持续接收服务端消息"""
        buffer = bytearray()
        while self._running:
            try:
                data = self.sock.recv(1024)
                if not data:
                    # 正常关闭，处理剩余缓冲区
                    self._process_buffer(buffer)
                    break
                buffer.extend(data)
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        line_str = line.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                    if line_str:
                        self._process_line(line_str)
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                # ★ 异常断开，先处理缓冲区中剩余数据
                self._process_buffer(buffer)
                print(f"[ConnectionThread] 连接异常断开: {e}")
                break
            except Exception as e:
                print(f"[ConnectionThread] 监听异常: {e}")
                self._process_buffer(buffer)
                break
        self._running = False

    def _process_buffer(self, buffer):
        """处理缓冲区中剩余的完整行"""
        lines = buffer.split(b'\n')
        for line in lines:
            if not line:
                continue
            try:
                line_str = line.decode('utf-8')
                if line_str:
                    self._process_line(line_str)
            except UnicodeDecodeError:
                continue

    def _process_line(self, line):
        try:
            msg = json.loads(line)
            mtype = msg['type']
            data = msg.get('data', {})
            if mtype == 'login_ok':
                self.connection_success.emit(data)
            elif mtype == 'login_fail':
                reason = data.get('reason', '登录失败')
                self.connection_failed.emit(reason)
                self._running = False  # 登录失败才停止
            else:
                # 所有其他消息（聊天、列表、邮件、等）转发给上层
                print(f"[DEBUG] Forwarding message type: {mtype}")
                self.message_received.emit(json.dumps(msg))
        except Exception as e:
            print(f"[ConnectionThread] 处理消息异常: {e}", file=sys.stderr)
            traceback.print_exc()

    def disconnect(self):
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass

# ==================== 资源包加密与完整性（防篡改 / 防抓包复用） ====================
PACK_MAGIC = b'FLYENC1'
PACK_KEY_SALT = b'FireflyPack-v1'


def pack_key(menu_id, secret=''):
    """资源包密钥：与服务器同一算法 = sha256(盐 + 服务器私密 secret + 菜单 id)"""
    if isinstance(secret, str):
        secret = secret.encode('utf-8')
    return hashlib.sha256(PACK_KEY_SALT + b'|' + (secret or b'') + b'|' +
                          str(menu_id or '').encode('utf-8')).digest()


def pack_decrypt(blob, key):
    """解密服务器加密下发的资源包；不是本程序加密的（旧服务器/普通 zip）原样返回"""
    if not blob or not blob.startswith(PACK_MAGIC):
        return blob
    nonce = blob[len(PACK_MAGIC):len(PACK_MAGIC) + 8]
    data = blob[len(PACK_MAGIC) + 8:]
    n = len(data)
    if n == 0:
        return b''
    blocks = (n + 31) // 32
    ks = b''.join(hashlib.sha256(key + nonce + i.to_bytes(8, 'big')).digest()
                  for i in range(blocks))[:n]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(ks, 'big')).to_bytes(n, 'big')


def pack_manifest_sig(files, key):
    """清单签名校验（和服务端 pack_manifest_sig 一致；中间人改包会被发现）"""
    import hmac as _hmac
    payload = json.dumps(files, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return _hmac.new(key, payload, hashlib.sha256).hexdigest()


def safe_extract_zip(zip_bytes, extract_to):
    """内存解压：拒绝绝对路径/../ 之类的条目（防恶意包写出目录）"""
    import zipfile as _zip
    os.makedirs(extract_to, exist_ok=True)
    base = os.path.abspath(extract_to)
    n = 0
    with _zip.ZipFile(io.BytesIO(zip_bytes), 'r') as z:
        for info in z.infolist():
            name = str(info.filename).replace(chr(92), '/').lstrip('/')
            if not name or name.endswith('/'):
                continue
            parts = [p for p in name.split('/') if p not in ('', '.')]
            if any(p == '..' for p in parts):
                print(f"[资源包] 跳过可疑条目: {name}")
                continue
            dest = os.path.join(base, *parts)
            if not os.path.abspath(dest).startswith(base):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(z.read(info))
            n += 1
    return n


def dir_file_hashes(directory):
    """目录内所有文件的相对路径 → sha256（用于本地清单与校验）"""
    out = {}
    base = os.path.abspath(directory)
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for fn in names:
            if fn == '.integrity.json':
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, base).replace(chr(92), '/')
            try:
                with open(p, 'rb') as f:
                    out[rel] = hashlib.sha256(f.read()).hexdigest()
            except Exception:
                pass
    return out


class _GachaPackSync(QThread):
    """后台静默同步资源包：不弹 Qt 窗口，进度通过页面内提示展示。"""

    progress = pyqtSignal(int)           # 0-100，-1 表示总大小未知
    finished_ok = pyqtSignal(bool, str)  # 是否成功, 说明

    def __init__(self, url, zip_path, extract_to, parent=None, key=None):
        super().__init__(parent)
        self.url = url
        self.zip_path = zip_path
        self.extract_to = extract_to
        self.key = key            # 资源包密钥（服务器加密下发时用它解密）

    def run(self):
        try:
            os.makedirs(os.path.dirname(self.zip_path), exist_ok=True)
            req = urllib.request.Request(self.url, headers={'User-Agent': 'FireflyPet'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                try:
                    total = int(resp.headers.get('Content-Length') or 0)
                except Exception:
                    total = 0
                got = 0
                with open(self.zip_path, 'wb') as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        self.progress.emit(int(got * 100 / total) if total else -1)
            # 服务器加密下发的包：先解密；解密不了就按普通 zip 处理（兼容旧服务器）
            try:
                with open(self.zip_path, 'rb') as f:
                    blob = f.read()
            except Exception as e:
                self.finished_ok.emit(False, f'读取资源包失败：{e}')
                return
            if self.key and blob.startswith(PACK_MAGIC):
                plain = pack_decrypt(blob, self.key)
                if not plain.startswith(b'PK'):
                    self.finished_ok.emit(False, '资源包解密失败（密钥不匹配或文件被改动）')
                    return
            else:
                plain = blob
            if os.path.isdir(self.extract_to):
                shutil.rmtree(self.extract_to, ignore_errors=True)
            os.makedirs(self.extract_to, exist_ok=True)
            try:
                safe_extract_zip(plain, self.extract_to)
            except Exception as e:
                self.finished_ok.emit(False, f'资源包解压失败：{e}')
                return
            # 加密的 .zip 保留下来当"修复源"（本地被别人改动时用它重解；它本身是密文，拷走没用）
            if not os.path.exists(os.path.join(self.extract_to, 'index.html')):
                self.finished_ok.emit(False, '资源包缺少 index.html')
                return
            try:
                with open(os.path.join(self.extract_to, '.integrity.json'), 'w',
                          encoding='utf-8') as f:
                    json.dump({'files': dir_file_hashes(self.extract_to), 'src': 'pack'},
                              f, ensure_ascii=False)
            except Exception:
                pass
            self.finished_ok.emit(True, '卡池资源已同步完成')
        except Exception as e:
            self.finished_ok.emit(False, f'同步失败：{e}')


class GachaBridge(QObject):
    def __init__(self, firefly):
        super().__init__(firefly)
        self.firefly = firefly
        self._webview = None
        print(f"[GachaBridge] 初始化 | 主程序ID:{id(self.firefly)}")
        print(f"联机状态:{self.firefly.mp_client.connected}")

    def set_webview(self, webview):
        self._webview = webview

    def shutdown(self):
        self._webview = None
        print("[GachaBridge] webview引用清空")

    @pyqtSlot(str, result=str)
    def getPoolInfo(self, pool_id):
        """卡池详情：该池可抽到的全部物品（星级/权重/概率）+ 保底规则。
        客户端持有服务端下发的完整配置（含物品池），这里按池 id 取给页面。"""
        try:
            cfg = dict(getattr(self.firefly, 'gacha_config', {}) or {})
            pool = None
            for _p in (cfg.get('pools') or []):
                if isinstance(_p, dict) and _p.get('id') == pool_id:
                    pool = _p
                    break
            if pool is None and (cfg.get('pools') or []):
                pool = cfg['pools'][0]
            if pool is None:
                return json.dumps({'name': '', 'items': {}, 'rules': {}}, ensure_ascii=False)

            def _disp(iid):
                """物品显示昵称：自定义物品用配置里的名字，内置物品/时装用中文名"""
                for _it in (getattr(self.firefly, 'server_custom_items', None) or []):
                    if isinstance(_it, dict) and _it.get('id') == iid:
                        return _it.get('name') or iid
                _map = {
                    'star_rail_ticket': '星铁专票', 'star_rail_tickets': '星铁专票',
                    'ticket': '星铁专票', 'tickets': '星铁专票',
                    'jade': '星琼', 'credit': '信用点',
                    'cake': '橡木蛋糕卷', 'none': '谢谢参与',
                }
                if iid in _map:
                    return _map[iid]
                try:
                    _c = AVAILABLE_CLOTHES.get(iid)
                    if _c:
                        return _c.get('name') or iid
                except Exception:
                    pass
                return iid

            def _rows(key):
                out = []
                for _it in (pool.get(key) or []):
                    if not isinstance(_it, dict):
                        continue
                    _d = dict(_it)
                    _d['display'] = _disp(_it.get('id'))
                    out.append(_d)
                return out

            info = {
                'name': pool.get('name') or pool.get('id') or '',
                'up_name': pool.get('up_name') or '',
                'rules': dict(pool.get('rules') or {}),
                'items': {
                    '5': _rows('five_star_items'),
                    '4': _rows('four_star_items'),
                    '3': _rows('three_star_items'),
                },
            }
            return json.dumps(info, ensure_ascii=False)
        except Exception:
            return json.dumps({'name': '', 'items': {}, 'rules': {}}, ensure_ascii=False)

    # 在 GachaBridge 类中
    @pyqtSlot(result=str)
    def getHistory(self):
        """抽卡记录：返回该玩家 30 天内**所有卡池**的记录，最近的排在最前。

        记录按玩家保存（服务端 gacha_history），所以天然汇总了所有 UP 池；
        这里统一补上 pool 名（老记录没有该字段就用当前活动卡池名兜底）并排序。
        """
        history = self.firefly.current_user.get("gacha_history", [])
        history = [h for h in (history or []) if isinstance(h, dict)]

        def _ts(h):
            try:
                return datetime.strptime(str(h.get('time') or ''), '%Y-%m-%d %H:%M:%S').timestamp()
            except Exception:
                try:
                    return datetime.fromisoformat(
                        str(h.get('time') or '').replace('T', ' ')).timestamp()
                except Exception:
                    return 0.0

        cutoff = (datetime.now() - timedelta(days=30)).timestamp()
        history = [h for h in history if _ts(h) >= cutoff]
        history.sort(key=_ts, reverse=True)      # 最近的在上
        return json.dumps({"history": history}, ensure_ascii=False)

    @pyqtSlot(result=str)
    @pyqtSlot(str, result=str)
    def getUserInfo(self, pool_id=''):
        inv = self.firefly.online_inventory if self.firefly.online_inventory else self.firefly.current_user
        inv = inv if isinstance(inv, dict) else {}
        user_data = self.firefly.current_user or {}
        _pity_map = getattr(self.firefly, 'gacha_pity', None)
        _info = (_pity_map.get(pool_id) if isinstance(_pity_map, dict) and pool_id else None) or {}
        res = {
            "tickets": int(inv.get("star_rail_tickets", 0) or 0),
            "jade": int(inv.get("jade", 0) or 0),
            "pity": _info.get("pity", user_data.get("gacha_pity_5", 0)),
            "guaranteed": _info.get("guaranteed", user_data.get("gacha_guaranteed_5", False)),
            "uid": user_data.get("uid", "unknown"),
            "pool_id": pool_id or '',
        }
        return json.dumps(res, ensure_ascii=False)

    @pyqtSlot(int, result=str)
    def exchange(self, amount):
        mp = self.firefly.mp_client
        if amount <= 0:
            return json.dumps({"success": False, "reason": "兑换数量需大于0"}, ensure_ascii=False)
        if not mp or mp.conn_thread is None or not mp.connected:
            return json.dumps({"success": False, "reason": "未连接服务器"}, ensure_ascii=False)
        mp._send({"type": "exchange", "data": {"amount": amount}})
        return json.dumps({"success": True, "pending": True})

    @pyqtSlot(result=str)
    def getInventory(self):
        inv = self.firefly.online_inventory or self.firefly.current_user or {}
        inv.setdefault("star_rail_tickets", 0)
        inv.setdefault("jade", 0)
        return json.dumps(inv, ensure_ascii=False)

    @pyqtSlot(result=bool)
    def canEditGacha(self):
        """是否允许在页面内直接编辑卡池（联机 + 管理员）"""
        try:
            mp = self.firefly.mp_client
            if not mp or not mp.connected:
                return False
            perm = (self.firefly.current_user or {}).get('permission') or ''
            return perm == 'admin'
        except Exception:
            return False

    @pyqtSlot(str)
    def submitGachaConfig(self, payload_json):
        """页面内编辑后保存卡池配置（服务端校验权限、落盘并广播）"""
        try:
            payload = json.loads(payload_json or '{}')
        except Exception as e:
            self._push_edit_result(False, f'数据解析失败: {e}')
            return
        mp = self.firefly.mp_client
        if not mp or not mp.connected:
            self._push_edit_result(False, '未连接服务器')
            return
        mp._send({'type': 'gacha_config_save', 'data': {'config': payload.get('config') or {}}})

    @pyqtSlot(str, str, str)
    def uploadPoolImage(self, pool_id, kind, b64):
        """页面上传卡池图片（封面/右上角物品图标）"""
        mp = self.firefly.mp_client
        if not mp or not mp.connected:
            self.firefly.on_gacha_image_result({'success': False, 'pool_id': pool_id,
                                                'kind': kind, 'reason': '未连接服务器'})
            return
        mp._send({'type': 'gacha_image_upload',
                  'data': {'pool_id': pool_id, 'kind': kind,
                           'menu_id': getattr(self.firefly, '_current_gacha_menu_id', '') or 'm11',
                           'b64': b64}})

    def _push_edit_result(self, ok, reason):
        views = getattr(self.firefly, '_gacha_webviews', None) or {}
        js = ('window.onConfigSaved && window.onConfigSaved(' +
              ('true' if ok else 'false') + ',' + json.dumps(reason or '', ensure_ascii=False) + ')')
        for _mid, _v in list(views.items()):
            try:
                if _v is not None:
                    _v.page().runJavaScript(js)
            except Exception:
                pass

    @pyqtSlot(str, result=str)
    def getPoolPity(self, pool_id):
        """返回指定卡池的保底进度（供页面切换卡池时显示）"""
        try:
            pid = pool_id or 'default'
            info = (getattr(self.firefly, 'gacha_pity', None) or {}).get(pid)
            if not info and pid in ('', 'default'):
                ud = self.firefly.current_user or {}
                info = {'pity': ud.get('gacha_pity_5', 0),
                        'guaranteed': ud.get('gacha_guaranteed_5', False)}
            info = info or {}
            return json.dumps({'pity': int(info.get('pity') or 0),
                               'guaranteed': bool(info.get('guaranteed'))},
                              ensure_ascii=False)
        except Exception:
            return json.dumps({'pity': 0, 'guaranteed': False})

    @pyqtSlot(int)
    @pyqtSlot(str, int)
    def requestGacha(self, *args):
        # 兼容两种调用：新版页面 (pool_id, count)，旧版页面 (count)
        if len(args) >= 2:
            pool_id, count = args[0], args[1]
        else:
            pool_id, count = '', args[0] if args else 1
        print(f"[GachaBridge] 收到抽卡 pool={pool_id} count={count}")
        mp = self.firefly.mp_client
        if mp and mp.conn_thread and mp.connected:
            mp._send({"type": "gacha", "data": {"count": count, "pool_id": pool_id}})
        else:
            self._send_result({"success": False, "reason": "离线无法跃迁"})

    def _send_result(self, data):
        if not self._webview:
            print("[GachaBridge] 窗口已销毁，跳过推送")
            return
        page = self._webview.page()
        if not page:
            return
        js_code = f'window.onGachaResult({json.dumps(data, ensure_ascii=False)})'
        page.runJavaScript(js_code)

    def on_gacha_result(self, data):
        print("[GachaBridge] 收到服务端原始抽卡数据：", data)
        inv = self.firefly.online_inventory if self.firefly.online_inventory else self.firefly.current_user
        inv = inv if isinstance(inv, dict) else {}
        user_data = self.firefly.current_user or {}
        # ★ 服务端返回的余额是权威值：用它刷新本地缓存，绝不能反过来用旧缓存覆盖它
        #   （旧逻辑会把抽卡前的余额写回去，页面显示 10 票而服务端已经是 0 → "余额不足"）
        if "tickets" in data:
            inv["star_rail_tickets"] = int(data.get("tickets") or 0)
        if "jade" in data:
            inv["jade"] = int(data.get("jade") or 0)
        data["tickets"] = int(inv.get("star_rail_tickets", 0) or 0)
        data["jade"] = int(inv.get("jade", 0) or 0)
        # 保底：优先用服务端按卡池返回的值，其次用本地缓存
        _pid = data.get("pool_id") or ''
        _pity_map = getattr(self.firefly, 'gacha_pity', None)
        if not isinstance(_pity_map, dict):
            _pity_map = {}
        if "pity" not in data or data.get("pity") is None:
            _info = _pity_map.get(_pid) or {}
            data["pity"] = _info.get("pity", user_data.get("gacha_pity_5", 0))
            data["guaranteed"] = _info.get("guaranteed",
                                           user_data.get("gacha_guaranteed_5", False))
        # 回写本地保底缓存，供切换卡池时显示
        try:
            _pity_map[_pid or 'default'] = {"pity": int(data.get("pity") or 0),
                                            "guaranteed": bool(data.get("guaranteed"))}
            self.firefly.gacha_pity = _pity_map
        except Exception:
            pass
        print("[GachaBridge] 补齐资源后下发前端完整数据：", data)

        # ★ 抽到的东西要立刻进本地背包（时装/自定义物品/信用点/蛋糕），
        #   否则背包和时装面板要重登才看得到（服务端其实已经发放）。
        #   注意：票/星琼不能在这里再加一次 —— 上面已经用服务端的权威余额覆盖过，
        #   结果里的 jade/star_rail_ticket 条目已经包含在那个余额里了。
        try:
            _inv_changed = False
            for _it in (data.get("results") or []):
                _t = (_it or {}).get("type")
                _iid = (_it or {}).get("id")
                try:
                    _amt = int((_it or {}).get("amount") or 0)
                except Exception:
                    _amt = 0
                if _t == "custom" and _iid == "none":
                    continue                      # 谢谢参与：不入包
                if _t == "clothes":
                    _owned = inv.setdefault("owned_clothes", ["normal"])
                    if _iid and _iid not in _owned:
                        _owned.append(_iid)
                        _inv_changed = True
                elif _t == "custom":
                    if _iid:
                        _ci = inv.setdefault("custom_items", {})
                        _ci[_iid] = _ci.get(_iid, 0) + _amt
                        _inv_changed = True
                elif _t == "credit":
                    inv["credit"] = inv.get("credit", 0) + _amt
                    _inv_changed = True
                elif _t == "cake":
                    inv["balance"] = inv.get("balance", 0) + _amt
                    _inv_changed = True
            if _inv_changed:
                if inv is not user_data:
                    for _k in ("owned_clothes", "custom_items", "credit", "balance"):
                        if _k in inv:
                            user_data[_k] = inv[_k]
                self.firefly.notify_inventory_updated()
                self.firefly.apply_current_clothes()
                print("[GachaBridge] 已同步本地背包（时装/物品/信用点）")
        except Exception as _e:
            print(f"[GachaBridge] 本地背包同步失败: {_e}")

        # ----- 新增：更新本地 gacha_history -----
        if "results" in data and data["results"]:
            now = datetime.now()
            time_str = now.strftime("%Y-%m-%d %H:%M:%S")
            history = user_data.get("gacha_history", [])
            # 记录来自哪个卡池（页面「抽卡记录」要汇总显示所有卡池的记录）
            _pid = data.get("pool_id") or ""
            _pname = ""
            try:
                _pools = (getattr(self.firefly, "gacha_config", {}) or {}).get("pools") or []
                _pobj = next((p for p in _pools
                              if isinstance(p, dict) and p.get("id") == _pid), None)
                if _pobj:
                    _pname = _pobj.get("name") or _pid
            except Exception:
                _pname = ""
            _base_ms = int(now.timestamp() * 1000)
            for _idx, item in enumerate(data["results"]):
                name = item.get("name", "未知")
                if item.get("type") == "custom" and item.get("id") == "none":
                    name = "谢谢参与"
                history.append({
                    "name": name,
                    "rarity": item.get("rarity", 3),
                    "time": time_str,
                    "isUp": item.get("isUp", False),
                    "isGuaranteed": item.get("isGuaranteed", False),
                    "pool": _pname,
                    "pool_id": _pid,
                    "id": "%d-%d" % (_base_ms, _idx),
                })
            # 过滤 30 天
            cutoff = datetime.now() - timedelta(days=30)
            filtered = []
            for h in history:
                try:
                    if datetime.strptime(h["time"], "%Y-%m-%d %H:%M:%S") >= cutoff:
                        filtered.append(h)
                except:
                    filtered.append(h)
            user_data["gacha_history"] = filtered
            self.firefly._save_current_user()
            # 同步更新服务端（如果联机，服务端会保存，但本地也要存）
            if self.firefly.mp_client.connected:
                # 可选：向服务端发送更新，但服务端 gacha 分支已保存，无需额外操作
                pass

        # 保存保底数据（仅默认卡池写旧字段，避免多卡池互相污染）
        if not _pid or _pid == 'default':
            if "pity" in data:
                user_data["gacha_pity_5"] = data["pity"]
            if "guaranteed" in data:
                user_data["gacha_guaranteed_5"] = data["guaranteed"]
        if self.firefly.current_user:
            self.firefly._save_current_user()
        # 抽卡/兑换都会改变余额，顺带刷新所有已打开的卡池页面
        try:
            self.firefly.push_gacha_data()
        except Exception:
            pass

        # 下发到前端
        self._send_result(data)

class MultiplayerClient(QObject):
    login_success = pyqtSignal(dict)  # 传递完整登录数据
    message_received = pyqtSignal(dict)
    online_list_updated = pyqtSignal(list)
    mentioned = pyqtSignal(str)
    mentioned_all = pyqtSignal(str)
    connection_changed = pyqtSignal(bool)
    login_failed = pyqtSignal(str)
    permission_changed = pyqtSignal()
    command_result_signal = pyqtSignal(bool, str)
    sign_result = pyqtSignal(bool, str)
    redeem_result_signal = pyqtSignal(bool, str)
    sign_result_signal = pyqtSignal(bool, str)
    claim_mail_result_signal = pyqtSignal(bool, str)
    mails_ready = pyqtSignal(list)
    chat_history_ready = pyqtSignal(list)
    all_players_ready = pyqtSignal(list)
    pm_history_ready = pyqtSignal(dict)
    banned_by_server = pyqtSignal(str)
    server_shutdown = pyqtSignal(str)

    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        self.connected = False
        self.server_uid = None
        self.server_permission = 'user'
        self.conn_thread = None   # 连接线程
        self.stats = None
        self._last_ping_sent = 0
        self._latest_latency_ms = -1


    def request_gacha(self, count):
        if self.connected:
            self._send({'type': 'gacha', 'data': {'count': count}})

    def ping(self):
        if not self.connected or not self.conn_thread:
            return
        self._last_ping_sent = time.time()
        print(f"[PING] 发送请求，时间 {self._last_ping_sent}")  # 新增
        self._send({'type': 'get_player_list', 'data': {}})

    def connect_to_server(self, host, port, userid, uid, name, country=''):
        self.server_host = host
        self.server_port = port
        # 登录安全：本地保存的口令（只用于算 SCRAM 应答，绝不上线）+ 本机绑定 id
        _pwd_in, _bid_in = '', ''
        try:
            _ff = getattr(self, 'firefly', None)
            if _ff is not None:
                _pwd_in = str((getattr(_ff, 'current_user', None) or {}).get('password') or '')
                try:
                    _bid_in = str(_ff.load_bind_id() or '')
                except Exception:
                    _bid_in = ''
        except Exception as _e:
            print(f"[安全] 读取登录凭证失败: {_e}")
        self.conn_thread = ConnectionThread(host, port, userid, uid, name, country, CLIENT_VERSION)
        self.conn_thread.password = _pwd_in
        self.conn_thread.bind_id = _bid_in
        self.conn_thread.connection_success.connect(self._on_connection_success)
        self.conn_thread.connection_failed.connect(self._on_connection_failed)
        self.conn_thread.connection_closed.connect(self._on_connection_closed)
        self.conn_thread.message_received.connect(self._on_message_received)  # 新增
        self.conn_thread.start()

    def _send(self, obj):
        if self.conn_thread:
            self.conn_thread._send(obj)

    def _on_message_received(self, msg_json):
        try:
            msg = json.loads(msg_json)
        except:
            return
        self._process_message(msg)  # 直接传递字典

    def _on_connection_success(self, data):
        server_uid = data['server_uid']
        permission = data['permission']
        inventory = data.get('inventory')
        server_version = data.get('server_version')
        stats = data.get('stats', {})
        ai_enabled = data.get('ai_enabled', True)
        muted_until = data.get('muted_until')
        server_timezone_offset = data.get('server_timezone_offset', 0)
        chat_enabled = data.get('chat_enabled', True)
        server_display_name = data.get('server_display_name', '流萤服务器')
        require_rename = data.get('require_rename', False)
        server_name = data.get('server_name', '')
        uid = data.get('uid', '')
        chat_history = data.get('chat_history', [])
        # ---------- 新增：保存 Web 端口（用于资源下载） ----------
        web_port = data.get('web_port', 8080)  # 从服务端获取，默认 8080
        self.server_web_port = web_port
        # 资源包密钥种子：服务端登录时下发，用来解密资源包 / 校验清单签名（不落盘）
        try:
            self.pack_secret = str(data.get('pack_secret') or '')
        except Exception:
            self.pack_secret = ''
        # ---------- 设备绑定 / 受限会话（连接安全） ----------
        try:
            _bid = str(data.get('bind_id') or '')
            if _bid:
                self.save_bind_id(_bid)
        except Exception as _e:
            print(f"[安全] 处理 bind_id 失败: {_e}")
        try:
            if data.get('restricted'):
                _reason = str(data.get('restricted_reason') or
                              '本账号尚未绑定设备，当前为受限会话。')
                print(f"[安全] 受限会话：{_reason}")
                QTimer.singleShot(600, lambda: self.show_centered_message(
                    "受限会话（设备未绑定）", _reason + "\n\n绑定方法：让管理员在服务器控制台执行"
                    " /bindcode <你的玩家名>，然后在聊天框输入 /bind <绑定码>。",
                    QMessageBox.Warning))
        except Exception as _e2:
            print(f"[安全] 受限提示失败: {_e2}")

        # 更新客户端状态
        self.connected = True
        self.connection_changed.emit(True)
        self.server_uid = server_uid
        self.server_permission = permission
        self.stats = stats

        self.firefly.online_uid = uid
        self.firefly.ai_enabled = ai_enabled
        self.firefly.on_mute_status({'muted_until': muted_until,
                                     'muted_perm': data.get('muted_perm'),
                                     'pm_ban': data.get('pm_ban')})
        self.firefly.server_timezone_offset = server_timezone_offset
        self.firefly.chat_enabled = chat_enabled
        self.firefly.on_chat_status(chat_enabled)
        self.firefly.server_display_name = server_display_name
        self.firefly.online_name = server_name

        # 保存 Web 信息到 Firefly（用于资源下载）  ← 修复点
        self.firefly.server_host = self.server_host  # 使用保存的 host
        self.firefly.server_web_port = web_port

        self.firefly.chat_history = normalize_chat_history(chat_history)
        self.firefly.online_inventory = inventory
        if inventory is not None:
            self.firefly.online_signed_today = inventory.get('last_sign_date') == datetime.now().strftime('%Y-%m-%d')
        else:
            self.firefly.online_signed_today = False
        self.login_success.emit(data)

    def _reset_server_state(self):
        """掉线后清掉服务端下发的东西（商品清单/物品表）。

        单机模式绝不能显示服务器商品或服务器新增物品（用户要求）：
        以前只在"获取商店数据"时按 is_online 判断，一旦标志过期/残留就会漏出来，
        现在掉线即清空，单机下商店只剩原版两件。
        """
        try:
            ff = self.firefly
            if ff is not None:
                ff.shop_goods = []
                ff._shop_goods_seen = False
                ff.server_custom_items = []
        except Exception:
            pass

    def _on_connection_failed(self, reason):
        self.connected = False
        self._reset_server_state()
        self.login_failed.emit(reason)

    def _on_connection_closed(self):
        self.connected = False
        self._reset_server_state()
        self.connection_changed.emit(False)


    def send_chat(self, text):
        if not self.connected or not self.conn_thread:
            return
        msg = {'type': 'chat', 'data': {'text': text}}
        self.conn_thread._send(msg)

    def _server_bind_key(self):
        """本机绑定信息按"哪个服务器"分开保存的键（哈希，不落 ip/端口）"""
        try:
            uid = getattr(self, 'server_uid', '') or ''
            if uid:
                return 'h' + hashlib.sha256(('FireflySrv|' + str(uid)).encode('utf-8')).hexdigest()[:16]
            raw = '%s:%s' % (getattr(self, 'server_host', '') or '', getattr(self, 'server_web_port', '') or '')
            if raw != ':':
                return 'h' + hashlib.sha256(('FireflySrv|' + raw).encode('utf-8')).hexdigest()[:16]
        except Exception:
            pass
        return 'hunknown'

    def _bind_store_path(self):
        return os.path.join(get_writable_dir(), 'device_bind.json')

    def load_bind_id(self):
        try:
            with open(self._bind_store_path(), 'r', encoding='utf-8') as f:
                d = json.load(f)
            return str((d or {}).get(self._server_bind_key()) or '')
        except Exception:
            return ''

    def save_bind_id(self, bid):
        try:
            p = self._bind_store_path()
            d = {}
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    d = json.load(f) or {}
            if not isinstance(d, dict):
                d = {}
            d[self._server_bind_key()] = str(bid or '')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False)
            print("[安全] 已保存本机绑定 id")
        except Exception as e:
            print(f"[安全] 保存绑定 id 失败: {e}")

    def bind_with_code(self, code, silent=False):
        """用绑定码绑定本机（顺带用本地保存的口令设置登录校验；明文口令不上线）"""
        if not self.connected or not self.conn_thread:
            if not silent:
                self.show_centered_message("绑定失败", "未连接服务器", QMessageBox.Warning)
            return False
        payload = {'code': str(code or '').strip()}
        try:
            pwd = ''
            try:
                pwd = str((self.current_user or {}).get('password') or '')
            except Exception:
                pwd = ''
            salt = str(getattr(self.conn_thread, 'auth_salt', '') or '')
            if pwd and salt:
                # 只发 salt + StoredKey（服务端也只存这两样）
                iters = int(getattr(self.conn_thread, 'auth_iterations', 120000) or 120000)
                sk = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), bytes.fromhex(salt), iters)
                payload['salt'] = salt
                payload['key'] = sk.hex()
        except Exception as e:
            print(f"[绑定] 生成本地校验值失败(仅绑定设备): {e}")
        self.conn_thread._send({'type': 'bind_device', 'data': payload})
        return True

    def send_command(self, cmd):
        if not self.connected or not self.conn_thread:
            return
        # /bind <绑定码>：本地算好 SCRAM 校验值再发给服务端（不把明文口令发出去）
        _c = str(cmd or '').strip()
        if _c.lower().startswith('/bind') and not _c.lower().startswith('/bindcode'):
            _parts = _c.split()
            if len(_parts) >= 2:
                self.bind_with_code(_parts[1])
            else:
                self.show_centered_message("绑定设备",
                                           "用法：/bind <绑定码>\n绑定码请在服务器控制台执行 /bindcode <玩家> 获取（10 分钟有效）",
                                           QMessageBox.Information)
            return
        self.conn_thread._send({'type': 'command', 'data': {'command': cmd}})

    def request_player_list(self):
        if not self.connected or not self.conn_thread:
            return
        self.conn_thread._send({'type': 'get_player_list', 'data': {}})

    def disconnect(self):
        if self.conn_thread:
            self.conn_thread.disconnect()
            self.conn_thread = None
        self.connected = False
        self._reset_server_state()
        self.connection_changed.emit(False)

    def _process_message(self, msg):
        """
        处理从服务端接收到的消息（字典格式），并转发给 UI 层。
        """
        try:
            mtype = msg['type']
            data = msg.get('data', {})
        except Exception:
            return

        # ---------- 各类消息处理 ----------
        if mtype == 'player_list':
            if self._last_ping_sent > 0:
                latency = (time.time() - self._last_ping_sent) * 1000
                self._latest_latency_ms = int(latency)
                print(f"[PING] 收到响应，延迟 {self._latest_latency_ms}ms，时间 {time.time()}")  # 新增
                self._last_ping_sent = 0
            self.online_list_updated.emit(data.get('players', []))
        elif mtype == 'gacha_result':
            # 修复：self.fire → self.firefly，不会再报属性错误
            if self.firefly and hasattr(self.firefly, '_current_gacha_bridge') and self.firefly._current_gacha_bridge:
                self.firefly._current_gacha_bridge.on_gacha_result(data)
        elif mtype == 'exchange_result':
            try:
                # 兑换结果里带权威余额：先刷新本地缓存，页面才不会显示旧票数
                if data.get('success'):
                    _inv = self.firefly.online_inventory
                    if not isinstance(_inv, dict):
                        _inv = self.firefly.current_user if isinstance(self.firefly.current_user, dict) else {}
                    if data.get('tickets') is not None:
                        _inv['star_rail_tickets'] = int(data.get('tickets') or 0)
                    if data.get('jade') is not None:
                        _inv['jade'] = int(data.get('jade') or 0)
                    self.firefly.push_gacha_data()
            except Exception as _e:
                print(f"[兑换] 同步余额失败: {_e}")
            # 兑换结果走专用回调 —— 以前这里误用 _send_result（那等于调用页面 onGachaResult），
            # 页面看到没有 results 就弹"抽卡结果为空，请联系管理员"（用户实测）
            try:
                _br = getattr(self.firefly, '_current_gacha_bridge', None)
                if _br is not None:
                    _views = getattr(self.firefly, '_gacha_webviews', None) or {}
                    _js = ('window.onExchangeResult && window.onExchangeResult(' +
                           json.dumps(data, ensure_ascii=False) + ')')
                    for _mid2, _v2 in list(_views.items()):
                        try:
                            if _v2 is not None:
                                _v2.page().runJavaScript(_js)
                        except Exception:
                            pass
            except Exception as _ee:
                print(f"[兑换] 推送页面失败: {_ee}")
        elif mtype == 'buy_shop_result':
            try:
                if data.get('success'):
                    inv = data.get('inventory')
                    if inv is not None:
                        self.firefly.online_inventory = inv
                    self.firefly.notify_inventory_updated()
                    self.firefly.apply_current_clothes()
                    self.firefly.show_centered_message(
                        "购买成功",
                        f"{data.get('name', '商品')} × {data.get('qty', 1)}",
                        QMessageBox.Information)
                else:
                    self.firefly.show_centered_message(
                        "购买失败", data.get('reason') or '未知错误', QMessageBox.Warning)
            except Exception as e:
                print(f"[购买结果] 处理异常: {e}")
        elif mtype == 'buy_item_result':
            try:
                if data.get('success'):
                    inv = data.get('inventory')
                    if inv is not None:
                        self.firefly.online_inventory = inv
                    self.firefly.notify_inventory_updated()
                    self.firefly.push_gacha_data()
                    self.firefly.apply_current_clothes()
                    name = data.get('id', '物品')
                    bought = None
                    for _it in (self.firefly.server_custom_items or []):
                        if _it.get('id') == data.get('id'):
                            bought = _it
                            name = _it.get('name') or name
                            break
                    show_notification_async("购买成功", f"{name} x{data.get('qty', 1)}", duration=3)
                    # 买的是时装：登记资源后立即穿上
                    if bought and (bought.get('type') or 'item') == 'clothes':
                        try:
                            self.firefly.sync_custom_items()
                            self.firefly.apply_clothes(bought.get('id'), silent=True)
                        except Exception as _e:
                            print(f"[购买时装] 自动穿戴失败: {_e}")
                else:
                    self.firefly.show_centered_message(
                        "购买失败", data.get('reason') or '未知错误')
            except Exception as e:
                print(f"[购买结果] 处理异常: {e}")
        elif mtype == 'mute_status':
            print(f"[调试] 收到 mute_status 消息，data={data}")
            try:
                if self.firefly:
                    self.firefly.on_mute_status(data)
                else:
                    print("[错误] firefly 为 None，无法处理禁言状态")
            except Exception as e:
                print(f"[调试] 处理 mute_status 异常: {e}")
                import traceback
                traceback.print_exc()
        elif mtype == 'birthday_mail_result':
            success = data.get('success', False)
            message = data.get('message', '')
            if success:
                self.firefly.current_user["last_birthday_greet_year"] = datetime.now().year
                self.firefly._save_current_user()
                # 可选：刷新邮箱
                self.firefly.mp_client._send({'type': 'get_mails'})
                show_notification_async("流萤的祝福", "生日邮件已发送至邮箱！", duration=3)
            else:
                print(f"生日邮件发送失败：{message}")
        elif mtype == 'chat_status':
            enabled = data.get('enabled', True)
            self.firefly.on_chat_status(enabled)
        elif mtype == 'ai_status':
            print("[DEBUG] Processing ai_status message")
            self.firefly.on_ai_status(data.get('enabled', True))
        elif mtype == 'chat_broadcast':
            # 直接转发给上层
            self.message_received.emit(msg)  # 注意：这里直接发射字典，不再是字符串
        elif mtype == 'rename_info':
            remaining = data.get('remaining', 1)
            next_time = data.get('next_time', "")
            dlg = getattr(self.firefly, '_pending_rename_dialog', None) if self.firefly else None
            if dlg is not None:
                dlg.set_remaining_changes(remaining, next_time)
        elif mtype == 'mention':
            from_name = data.get('from_name', '')
            if not from_name:
                return
            if data.get('all'):
                self.mentioned_all.emit(from_name)
            else:
                self.mentioned.emit(from_name)
        elif mtype == 'system_message':
            text = data.get('text', '')
            if '封禁' in text:
                self.banned_by_server.emit(text)
            # 组装成类似聊天广播的结构，方便统一处理
            fake_msg = {
                'type': 'chat_broadcast',
                'data': {
                    'from_name': '系统',
                    'text': text,
                    'is_system': True
                }
            }
            self.message_received.emit(fake_msg)
        elif mtype == 'login_fail':
            reason = data.get('reason', '未知原因')
            self.login_failed.emit(reason)
            self.disconnect()
        elif mtype == 'permission_changed':
            new_permission = data.get('permission', 'user')
            new_server_uid = data.get('server_uid', self.server_uid)
            new_uid = data.get('uid', '')

            # 更新权限相关变量
            self.server_permission = new_permission
            self.server_uid = new_server_uid

            self.firefly.is_admin = (new_permission == 'admin')
            self.firefly.server_permission = new_permission

            # ---------- 关键修复：确保 UID 前缀正确 ----------
            if new_uid:
                # 服务端显式下发了新 UID，直接使用
                uid_to_set = new_uid
            else:
                # 服务端未下发，根据当前 UID 自动修正前缀
                current_uid = self.firefly.online_uid or self.firefly.current_user.get('uid', '')
                if self.firefly.is_admin and current_uid.startswith('user-'):
                    uid_to_set = current_uid.replace('user-', 'admin-', 1)
                elif not self.firefly.is_admin and current_uid.startswith('admin-'):
                    uid_to_set = current_uid.replace('admin-', 'user-', 1)
                else:
                    uid_to_set = current_uid  # 保持原样

            if uid_to_set:
                self.firefly.online_uid = uid_to_set
                self.firefly.current_user['uid'] = uid_to_set

            # 刷新右键菜单（托盘菜单会同步更新）
            self.firefly.create_menu()
            if self.firefly.tray_icon:
                self.firefly.tray_icon.setContextMenu(self.firefly.create_menu())

            # 其他原有逻辑（如有）
            self.firefly.on_permission_changed()
        elif mtype == 'chat_history_data':
            self.firefly.chat_history = normalize_chat_history(data.get('history', []))
            if self.firefly._refresh_chat_pending:
                self.firefly._refresh_chat_pending = False
                self.firefly.refresh_chat_window()
        elif mtype == 'mails_data':
            self.mails_ready.emit(data.get('mails', []))
        elif mtype == 'claim_mail_result':
            self.claim_mail_result_signal.emit(data.get('success', False), data.get('reason', ''))
        elif mtype == 'redeem_result':
            self.redeem_result_signal.emit(data.get('success', False), data.get('message', ''))
        elif mtype == 'command_result':
            self.command_result_signal.emit(data.get('success', False), data.get('message', ''))
        elif mtype == 'check_name_result':
            available = data.get('available', False)
            reason = data.get('reason', '')
            # 通知 Firefly 或 ChangeNameDialog
            if self.firefly:
                self.firefly._on_check_name_result(available, reason)
        elif mtype == 'stats_data':
            stats = data.get('stats', {})
            self.stats = stats
            if self.firefly and self.firefly.current_user:
                for key, value in stats.items():
                    if key in self.firefly.current_user:
                        self.firefly.current_user[key] = value
                self.firefly._save_current_user()
            self.firefly.online_stats = stats
        elif mtype == 'sign_result':
            self.sign_result_signal.emit(data.get('success', False), data.get('message', ''))
        elif mtype == 'inventory_update':
            inv = data.get('inventory', {})
            old_jade = self.firefly.online_inventory.get('jade', 0) if self.firefly.online_inventory else 0
            self.firefly.online_inventory = inv
            new_jade = inv.get('jade', 0)
            if new_jade > old_jade:
                # 增加星琼获取统计
                self.firefly.current_user["monthly_jade_gained"] = self.firefly.current_user.get("monthly_jade_gained",
                                                                                                 0) + (
                                                                               new_jade - old_jade)
                self.firefly.current_user["total_jade_gained"] = self.firefly.current_user.get("total_jade_gained",
                                                                                               0) + (
                                                                             new_jade - old_jade)
                self.firefly._save_current_user()
            today = datetime.now().strftime('%Y-%m-%d')
            self.firefly.online_signed_today = (inv.get('last_sign_date') == today)
            self.firefly.update_bag_display()
            # 处理信用点变化统计
            if hasattr(self.firefly, '_before_redeem_credit') and self.firefly._before_redeem_credit is not None:
                old_credit = self.firefly._before_redeem_credit
                new_credit = inv.get('credit', 0)
                diff = new_credit - old_credit
                if diff > 0:
                    self.firefly.check_month_reset()
                    self.firefly.current_user["monthly_credit_gained"] += diff
                    self.firefly._save_current_user()
                self.firefly._before_redeem_credit = None
        elif mtype == 'common_mute_status':
            enabled = data.get('enabled', False)
            self.firefly.on_common_mute_status(enabled)
        elif mtype == 'kick':
            # 直接转发踢出消息
            self.message_received.emit(msg)
        elif mtype == 'recall_notification':
            # 确保撤回通知直接转发，供 UI 处理
            self.message_received.emit(msg)
        elif mtype == 'pm_ack':
            if self.firefly:
                self.firefly.on_pm_ack(data)
        elif mtype == 'pm_recv':
            if self.firefly:
                self.firefly.on_pm_recv(data)
        elif mtype == 'friends_data':
            if self.firefly:
                self.firefly.on_friends_data(data)
        elif mtype == 'friend_remark_result':
            if self.firefly:
                self.firefly.on_friend_remark_result(data)
        elif mtype == 'all_players':
            self.all_players_ready.emit(data.get('players', []) or [])
        elif mtype == 'pm_history_data':
            self.pm_history_ready.emit(data)
        elif mtype == 'block_result':
            if self.firefly:
                self.firefly.on_block_result(data)
        elif mtype == 'friend_delete_result':
            if self.firefly:
                self.firefly.on_friend_delete_result(data)
        elif mtype == 'chat_prefs_data':
            if self.firefly:
                self.firefly.on_chat_prefs_data(data)
        else:
            # 其他未知消息，直接转发
            self.message_received.emit(msg)
        # 强制刷新聊天窗口（如果打开）
        if self.firefly.chat_window and self.firefly.chat_window.isVisible():
            self.firefly.chat_window.refresh_all_messages()


# ====================== 邮箱窗口 ======================
class MailboxWindow(QDialog):
    def __init__(self, firefly, parent=None, online_mails=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "📬")
        self.firefly = firefly
        self.online_mails = online_mails

        if online_mails is not None:
            self.mails = online_mails
            self.local_read_mails = self.firefly.current_user.get("read_mails", [])
            self.deleted_mails = self.firefly.current_user.get("deleted_mails", [])
            self.firefly.mp_client.mails_ready.connect(self.on_mails_updated)
        else:
            self.mails = firefly.current_user.get("mailbox", [])
            self.local_read_mails = []
            self.deleted_mails = []

        self.setWindowTitle("邮箱")
        self.setFixedSize(*fit_4_3(820, 550))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QListWidget { background-color: #2D2D2D; border: 1px solid #555555; border-radius: 8px; color: #CCCCCC; font-size: 13px; padding: 5px; }
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px; }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; }
        """)
        self.draggable = False
        self.offset = None
        self.init_ui()
        self.load_mails()
        self.apply_rounded_mask()
        if self.mail_list.count() > 0:
            self.mail_list.setCurrentRow(0)
            self.show_detail_for_current()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    # ---------- 鼠标事件 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.pos().y() <= 30:
            self.draggable = True
            self.offset = event.pos()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.draggable:
            self.move(event.globalPos() - self.offset)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = False
        else:
            super().mouseReleaseEvent(event)

    # ---------- 圆角遮罩 ----------
    def create_rounded_mask(self):
        radius = 15
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        return QRegion(path.toFillPolygon().toPolygon())

    def apply_rounded_mask(self):
        self.setMask(self.create_rounded_mask())

    # ---------- UI 初始化 ----------
    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(10)

        title = QLabel("📬 邮箱")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFFFFF;")
        main_layout.addWidget(title)

        content_layout = QHBoxLayout()
        content_layout.setSpacing(10)

        self.mail_list = QListWidget()
        self.mail_list.itemClicked.connect(self.on_mail_clicked)
        content_layout.addWidget(self.mail_list, 2)

        right_frame = QFrame()
        right_frame.setStyleSheet("""
            QFrame { background-color: #2D2D2D; border: 1px solid #555555; border-radius: 8px; }
            QTextEdit { background-color: #2D2D2D; border: none; color: #CCCCCC; font-size: 13px; padding: 10px; }
        """)
        right_layout_inner = QVBoxLayout(right_frame)
        right_layout_inner.setContentsMargins(0, 0, 0, 0)
        right_layout_inner.setSpacing(0)

        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        right_layout_inner.addWidget(self.detail_text)

        self.ticket_label = QLabel()
        self.ticket_label.setStyleSheet("color: #FFD700; font-size: 14px; margin: 8px;")
        self.ticket_label.setToolTip("来自列车的祝福")
        self.ticket_label.hide()
        right_layout_inner.addWidget(self.ticket_label)

        self.claim_btn = QPushButton("领取")
        self.claim_btn.setObjectName("claim_btn")
        self.claim_btn.setStyleSheet("""
            QPushButton#claim_btn { background-color: #FF9800; color: white; border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px; margin: 8px; }
            QPushButton#claim_btn:hover { background-color: #F57C00; }
            QPushButton#claim_btn:disabled { background-color: #666666; }
        """)
        self.claim_btn.clicked.connect(self.claim_current_mail)
        self.claim_btn.setEnabled(False)
        right_layout_inner.addWidget(self.claim_btn, alignment=Qt.AlignRight)

        content_layout.addWidget(right_frame, 3)
        main_layout.addLayout(content_layout)

        btn_layout = QHBoxLayout()
        self.claim_all_btn = QPushButton("一键领取")
        self.claim_all_btn.clicked.connect(self.claim_all)

        self.read_all_btn = QPushButton("全部已读")
        self.read_all_btn.clicked.connect(self.read_all)

        # 删除已读按钮 - 红色
        self.delete_read_btn = QPushButton("删除已读")
        self.delete_read_btn.setStyleSheet("""
            QPushButton {
                background-color: #D32F2F;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #F44336;
            }
            QPushButton:disabled {
                background-color: #666666;
            }
        """)
        self.delete_read_btn.clicked.connect(self.delete_read_mails)
        self.delete_read_btn.setEnabled(True)
        if self.online_mails is not None:
            self.delete_read_btn.setToolTip("已删除的邮件将不再显示（本地隐藏）")
        else:
            self.delete_read_btn.setToolTip("")

        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)

        btn_layout.addWidget(self.claim_all_btn)
        btn_layout.addWidget(self.read_all_btn)
        btn_layout.addWidget(self.delete_read_btn)
        btn_layout.addWidget(self.close_btn)
        main_layout.addLayout(btn_layout)

        self.setLayout(main_layout)

    # ---------- 联机模式邮件更新 ----------
    def on_mails_updated(self, mails):
        """收到服务端最新邮件列表时刷新，并自动标记已领取的邮件为已读"""
        # 过滤已删除的邮件
        filtered = [m for m in mails if m['id'] not in self.deleted_mails]
        # 自动将已领取的邮件标记为已读
        for mail in filtered:
            if mail.get("claimed", False):
                mail["read"] = True
                if mail["id"] not in self.local_read_mails:
                    self.local_read_mails.append(mail["id"])
        self.mails = filtered
        self.load_mails()
        self.show_detail_for_current()
        # 保存已读记录
        if self.online_mails is not None:
            self.firefly.current_user["read_mails"] = self.local_read_mails
            self.firefly._save_current_user()

    # ---------- 加载邮件列表 ----------
    def load_mails(self):
        # 联机模式下过滤已删除的邮件
        if self.online_mails is not None:
            self.mails = [m for m in self.mails if m['id'] not in self.deleted_mails]

        self.mail_list.blockSignals(True)
        self.mail_list.clear()
        for i, mail in enumerate(reversed(self.mails)):
            title = mail["title"]

            if self.online_mails is not None and mail["id"] in self.local_read_mails:
                mail["read"] = True

            if not mail.get("read"):
                title = "● " + title
            if mail.get("claimed"):
                title += " [已领取]"

            item = QListWidgetItem(title)
            if mail.get("claimed"):
                item.setForeground(QColor(128, 128, 128))
                # 保留可选中状态（不取消 ItemIsSelectable）
            item.setData(Qt.UserRole, len(self.mails) - 1 - i)
            self.mail_list.addItem(item)
        self.mail_list.blockSignals(False)

    def get_mail_by_item(self, item):
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        if index is None or index < 0 or index >= len(self.mails):
            return None
        return self.mails[index]

    # ---------- 点击邮件 ----------
    def on_mail_clicked(self, item):
        # 直接显示详情，不论是否已领取
        self.show_detail_for_item(item)

    def show_detail_for_current(self):
        item = self.mail_list.currentItem()
        self.show_detail_for_item(item)

    def show_detail_for_item(self, item):
        mail = self.get_mail_by_item(item)
        if mail is None:
            self.detail_text.clear()
            self.ticket_label.hide()
            self.claim_btn.setEnabled(False)
            self.claim_btn.setText("领取")
            return

        # 联机模式：记录已读状态
        if self.online_mails is not None and not mail.get("read"):
            mail["read"] = True
            if mail["id"] not in self.local_read_mails:
                self.local_read_mails.append(mail["id"])
                self.firefly.current_user["read_mails"] = self.local_read_mails
                self.firefly._save_current_user()
            # 更新列表项文字
            self.mail_list.blockSignals(True)
            row = self.mail_list.row(item)
            title = mail["title"]
            if mail.get("claimed"):
                title += " [已领取]"
            item.setText(title)
            self.mail_list.blockSignals(False)

        # 构建详情（与原逻辑相同）
        items_html = []
        has_ticket = False

        # 用户要求：邮箱里不显示图片/emoji，物品一律纯文本「名称 x数量」

        for it in mail["items"]:
            if it["type"] == "cake":
                items_html.append(f"橡木蛋糕卷 x{it['amount']}")
            elif it["type"] == "credit":
                items_html.append(f"信用点 x{it['amount']}")
            elif it["type"] == "clothes":
                clothes_info = AVAILABLE_CLOTHES.get(it["id"], {})
                name = clothes_info.get('name') or self.firefly.custom_item_name(it.get('id'))
                items_html.append(f'时装 [<font color="#FFD700">{name}</font>]')
            elif it["type"] == "star_rail_ticket":
                has_ticket = True
                items_html.append(f'<font color="#FFD700">星铁专票 x{it["amount"]}</font>')
            elif it["type"] == "jade":
                items_html.append(f'<font color="#FFD700">星琼 x{it["amount"]}</font>')
            elif it["type"] == "custom":
                # 自定义物品/时装：以前这里没有分支 → 整条被跳过 → 只显示"物品：无"
                _iid = str(it.get('id') or '')
                if _iid == 'none':
                    items_html.append('谢谢参与')
                    continue
                _nm = it.get('name') or self.firefly.custom_item_name(_iid) or '未知物品'
                items_html.append(f'<font color="#FFD700">{_nm} x{it["amount"]}</font>')

        items_text = ', '.join(items_html) if items_html else "无"
        status = "已领取" if mail.get("claimed") else "未领取"
        detail_html = f"""
        <div style="color: #CCCCCC; font-size: 13px;">
        发件人：{mail['sender']}<br>
        时间：{mail['timestamp'][:19]}<br>
        状态：{status}<br>
        标题：{mail['title']}<br>
        ──────────────────<br>
        {mail['content']}<br>
        ──────────────────<br>
        物品：{items_text}
        </div>
        """
        self.detail_text.setHtml(detail_html)

        if has_ticket:
            self.ticket_label.setText("星铁专票")
            self.ticket_label.show()
        else:
            self.ticket_label.hide()

        # ★ 修改：根据领取状态显示按钮文字
        if not mail.get("items"):
            self.claim_btn.setEnabled(False)
            self.claim_btn.setText("无需领取")
        else:
            if mail.get("claimed"):
                self.claim_btn.setEnabled(False)
                self.claim_btn.setText("已领取")
            else:
                self.claim_btn.setEnabled(True)
                self.claim_btn.setText("领取")

    # ---------- 领取当前邮件 ----------
    def claim_current_mail(self):
        item = self.mail_list.currentItem()
        mail = self.get_mail_by_item(item)
        if mail is None or mail.get("claimed"):
            return

        if not mail.get("items"):
            QMessageBox.information(self, "提示", "该邮件无需领取。")
            return

        if self.online_mails is not None:
            self.firefly.mp_client._send({'type': 'claim_mail', 'data': {'mail_id': mail['id']}})
            # 等待服务端更新，通过 on_mails_updated 自动标记已读
        else:
            success, desc = claim_mail_items(self.firefly.current_user, mail)
            if success:
                # 标记已读
                mail["read"] = True
                for item in mail['items']:
                    if item['type'] == 'credit':
                        self.firefly.check_month_reset()
                        self.firefly.current_user["monthly_credit_gained"] += item['amount']
                self.firefly._save_current_user()
                self.firefly.update_bag_display()
                self.load_mails()
                self.show_detail_for_item(item)
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "成功", f"物品已领取！获得 {', '.join(desc)}"))

    # ---------- 一键领取 ----------
    def claim_all(self):
        if self.online_mails is not None:
            _todo = [m for m in self.mails if not m.get("claimed") and m.get("items")]
            if not _todo:
                # 都领完了就别再说"已发送领取请求"（用户实测的困惑点）
                QMessageBox.information(self, "提示", "没有可领取的邮件（都已领取）")
                return
            for mail in _todo:
                self.firefly.mp_client._send({'type': 'claim_mail', 'data': {'mail_id': mail['id']}})
            # 请求刷新，等待 on_mails_updated 自动标记已读
            self.firefly.mp_client._send({'type': 'get_mails'})
            QMessageBox.information(self, "提示",
                                    f"已发送 {len(_todo)} 封邮件的领取请求，稍候会自动刷新。")
        else:
            for mail in self.mails:
                if not mail.get("claimed") and mail.get("items"):
                    claim_mail_items(self.firefly.current_user, mail)
                    mail["read"] = True  # 标记已读
            self.firefly._save_current_user()
            self.firefly.update_bag_display()
            self.load_mails()
            self.show_detail_for_current()
            QTimer.singleShot(0, lambda: QMessageBox.information(self, "提示", "已领取所有未领取邮件"))

    # ---------- 全部已读 ----------
    def read_all(self):
        for mail in self.mails:
            mail["read"] = True
            if self.online_mails is not None and mail["id"] not in self.local_read_mails:
                self.local_read_mails.append(mail["id"])
        if self.online_mails is None:
            self.firefly._save_current_user()
        else:
            self.firefly.current_user["read_mails"] = self.local_read_mails
            self.firefly._save_current_user()
        self.load_mails()
        self.show_detail_for_current()

    # ---------- 删除已读 ----------
    def delete_read_mails(self):
        if self.online_mails is not None:
            # 联机模式：本地隐藏，不再恢复
            to_delete = [mail for mail in self.mails if mail.get("read")]
            if not to_delete:
                QMessageBox.information(self, "提示", "没有已读邮件可删除。")
                return
            reply = QMessageBox.question(self, "确认删除", f"确定要删除 {len(to_delete)} 封已读邮件吗？（已删除的邮件将不再显示）",
                                         QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.No:
                return
            for mail in to_delete:
                if mail['id'] not in self.deleted_mails:
                    self.deleted_mails.append(mail['id'])
                self.mails.remove(mail)
            self.firefly.current_user["deleted_mails"] = self.deleted_mails
            self.firefly._save_current_user()
            self.load_mails()
            self.detail_text.clear()
            self.ticket_label.hide()
            self.claim_btn.setEnabled(False)
            self.claim_btn.setText("领取")
            QMessageBox.information(self, "成功", f"已隐藏 {len(to_delete)} 封已读邮件。")
        else:
            # 单机模式：真实删除
            to_delete = [mail for mail in self.mails if mail.get("read")]
            if not to_delete:
                QMessageBox.information(self, "提示", "没有已读邮件可删除。")
                return
            reply = QMessageBox.question(self, "确认删除", f"确定要删除 {len(to_delete)} 封已读邮件吗？",
                                         QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.No:
                return
            for mail in to_delete:
                self.mails.remove(mail)
            self.firefly.current_user["mailbox"] = self.mails
            self.firefly._save_current_user()
            self.load_mails()
            self.detail_text.clear()
            self.ticket_label.hide()
            self.claim_btn.setEnabled(False)
            self.claim_btn.setText("领取")
            QMessageBox.information(self, "成功", f"已删除 {len(to_delete)} 封已读邮件。")

    # ---------- 关闭窗口 ----------
    def closeEvent(self, event):
        if self.online_mails is not None:
            self.firefly.current_user["read_mails"] = self.local_read_mails
            self.firefly.current_user["deleted_mails"] = self.deleted_mails
            self.firefly._save_current_user()
            try:
                self.firefly.mp_client.mails_ready.disconnect(self.on_mails_updated)
            except:
                pass
        event.accept()


# ====================== 兑换码输入对话框 ======================
# 兑换码对话框的补全规格：单机/联机按实际可用命令提供
REDEEM_COMMON_SPECS = [
    ('/help', False, []),
    ('/move', False, []),
]
REDEEM_OFFLINE_ADMIN_SPECS = [
    ('/give', True, ['target', ('cake', 'credit', 'ticket', 'jade', 'ruyi'), '数量']),
    ('/set', True, ['set_target', ('cake', 'credit', 'ticket', 'jade', 'ruyi'), '数量']),
    ('/clear', True, ['target', ('cake', 'credit', '*')]),
    ('/revoke', True, ['target', '物品:数量...']),
]
REDEEM_ONLINE_ADMIN_SPECS = [
    ('/op', True, ['user']),
    ('/deop', True, ['user']),
    ('/ban', True, ['ban_target', 'ban_value']),
    ('/unban', True, ['ban_target']),
    ('/kick', True, ['online_user']),
    ('/mute', True, ['user', ('server', 'global'), 'duration']),
    ('/unmute', True, ['user', ('server', 'global')]),
    ('/recall', True, ['消息ID']),
    ('/redeemstats', True, []),
    ('/resetname', True, ['user']),
    ('/maxplayers', True, ['人数']),
    ('/access', True, [('whitelist', 'blacklist', 'none'), ('on', 'off')]),
    ('/whitelist', True, [('on', 'off', 'add', 'remove', 'list')]),
    ('/blacklist', True, [('on', 'off', 'add', 'remove', 'list')]),
    ('/function', True, [('AI', 'Chat'), ('on', 'off')]),
]


class RedeemCodeDialog(QDialog):
    code_submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🎟️")
        self.setWindowTitle("使用兑换码")
        self.setFixedSize(350, 150)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit {
                border: 1px solid #555555; border-radius: 8px; padding: 8px;
                background-color: #2D2D2D; color: #888888; font-size: 14px;
            }
            QLineEdit:focus { border-color: #4CAF50; }
            QPushButton {
                background-color: #4CAF50; color: white; border: none;
                border-radius: 8px; padding: 8px 16px; font-size: 14px;
            }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        label = QLabel("请输入兑换码或命令：")
        layout.addWidget(label)

        self.code_edit = QLineEdit()
        # ---------- 动态命令补全（Minecraft 风格：灰色提示 + Tab 补齐 + 参数候选） ----------
        # 仅管理员可用：有管理员权限输入 / 自动有提示词，没有则不挂载补全器；
        # 单机/联机按实际可用的命令给提示。
        firefly = parent
        is_admin = False
        is_online = False
        if firefly:
            if hasattr(firefly, 'mp_client') and firefly.mp_client.connected:
                is_online = True
                is_admin = (firefly.mp_client.server_permission == 'admin')
            else:
                uid = firefly.current_user.get("uid", "") if hasattr(firefly, 'current_user') else ""
                is_admin = uid.lower().startswith("admin")

        if is_admin:
            redeem_specs = list(REDEEM_COMMON_SPECS) + REDEEM_OFFLINE_ADMIN_SPECS
            if is_online:
                redeem_specs += REDEEM_ONLINE_ADMIN_SPECS

            def redeem_users(online_only=False):
                if not is_online:
                    return []        # 单机没有其他用户：目标候选只有 @a/@s
                names = {}
                for p in (getattr(firefly, 'all_players', None) or []):
                    n = (p or {}).get('name')
                    if n:
                        names[n] = bool(p.get('online'))
                for p in (getattr(firefly, 'latest_players', None) or []):
                    n = (p or {}).get('name')
                    if n:
                        names[n] = True
                try:
                    names.pop(firefly.get_my_name(), None)
                except Exception:
                    pass
                items = sorted(names.items(), key=lambda kv: (0 if kv[1] else 1, kv[0]))
                if online_only:
                    return [n for n, on in items if on]
                return [n for n, _on in items]

            self.code_completer = SlashCompleter(self.code_edit,
                                                 lambda: (True, redeem_users),
                                                 redeem_specs)
        # ------------------------------------

        layout.addWidget(self.code_edit)

        btn_layout = QHBoxLayout()
        self.ok_btn = QPushButton("确定")
        self.cancel_btn = QPushButton("取消")
        self.ok_btn.clicked.connect(self.on_ok)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def on_ok(self):
        code = self.code_edit.text().strip()
        if not code:
            QMessageBox.warning(self, "提示", "请输入兑换码")
            return
        self.code_submitted.emit(code)
        self.code_edit.clear()  # 清空以便继续输入

class ColorPixmapLabel(QLabel):
    """彩色图标标签：直接绘制 pixmap，绕开 QStyle 的禁用灰度化。

    QLabel 画 pixmap 时走 QStyle::drawItemPixmap，控件一旦处于禁用态
    （背包条目是 disabled 的 QWidgetAction），Qt 会把整张图转成灰阶 —— 这就是
    用户看到的「背包图标没有颜色」。这里自己 QPainter.drawPixmap，保证彩色。
    """

    def __init__(self, pixmap=None, parent=None):
        super().__init__(parent)
        self._pm = None
        self.setStyleSheet("background: transparent;")
        if pixmap is not None:
            self.setColorPixmap(pixmap)

    def setColorPixmap(self, pixmap):
        self._pm = pixmap
        if pixmap is not None and not pixmap.isNull():
            self.setFixedSize(pixmap.size())
        self.update()

    def colorPixmap(self):
        return self._pm

    def paintEvent(self, event):
        if self._pm is not None and not self._pm.isNull():
            p = QPainter(self)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(0, 0, self._pm)
            p.end()
            return
        super().paintEvent(event)


class RoundedMenu(QMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(MENU_STYLE)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            super().mouseReleaseEvent(event)

    def showEvent(self, event):
        # 仅调用父类，不处理任何统计逻辑
        super().showEvent(event)
        # 只处理 Firefly 实例，避免被子类或其他对象误调用
        if not isinstance(self, Firefly):
            super().showEvent(event)  # 调用父类（QWidget）的 showEvent
            return

        super().showEvent(event)
        if not self._initialized:
            try:
                self.apply_cursor_style()
                self.apply_pet_size()
                self._init_movie_from_file()
                self.hi_music()
                self._initialized = True
            except Exception as e:
                print(f"初始化资源失败: {e}")

        # 计数逻辑（仅对 Firefly 实例有效）
        self.check_month_reset()
        self.current_user["total_open_count"] += 1
        self.current_user["monthly_open_count"] += 1
        self._save_current_user()

class DarkRoundedMenu(RoundedMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QMenu{
                border: 1px solid #3d3d3d;
                background-color: #2b2b2b;
                padding: 5px;
                border-radius: 8px;
            }
            QMenu::item{
                padding: 7px 22px;
                margin: 1px 4px;
                border-radius: 5px;
                color: #f0f0f0;
                background-color: transparent;
            }
            QMenu::item:selected{
                background-color: #3f6d8f;
                color: #ffffff;
            }
            QMenu::item:disabled{
                color: #808080;
            }
            QMenu::separator{
                height: 1px;
                background-color: #454545;
                margin: 5px 10px;
            }
        """)

# ====================== 兑换明细窗口 ======================
class HistoryWindow(QDialog):
    def __init__(self, user_data, is_admin=False, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "📋")
        self.user_data = user_data
        self.is_admin = is_admin
        self.setWindowTitle("兑换明细")
        self.setFixedSize(*fit_4_3(550, 400))
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #888888;
                font-size: 14px;
            }
            QListWidget {
                background-color: #2D2D2D;
                border: 1px solid #555555;
                border-radius: 8px;
                color: #CCCCCC;
                font-size: 12px;
                padding: 5px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("历史记录")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        self.setLayout(layout)
        self.load_history()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def load_history(self):
        history = self.user_data.get("redeem_history", [])
        for record in history:
            self.list_widget.addItem(record)
        if not history:
            self.list_widget.addItem("暂无记录")


# ====================== 商店窗口 ======================
# 商店货币：显示名与库存字段名（星铁专票在库存里是复数）
SHOP_CURRENCY_NAMES = {"credit": "信用点", "jade": "星琼", "star_rail_ticket": "星铁专票"}


def shop_currency_key(currency):
    return 'star_rail_tickets' if currency == 'star_rail_ticket' else (currency or 'credit')


def shop_currency_name(currency):
    return SHOP_CURRENCY_NAMES.get(currency, currency or 'credit')


def shop_balance(inv, currency):
    if not inv:
        return 0
    return inv.get(shop_currency_key(currency), 0)


class ShopMainWindow(QDialog):
    """
    左侧分类列表 + 右侧商品网格，数据驱动。
    单机模式只显示「食物」分类；联机模式显示「食物」和「兑换」。
    支持库存更新信号，自动刷新卡片状态。
    """
    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        self.is_online = self.firefly.mp_client.connected
        set_window_emoji_icon(self, "🛒")
        self.setWindowTitle("商店")
        self.setMinimumSize(*fit_4_3(800, 550))
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 12px;
            }
            QListWidget {
                background-color: #2D2D2D;
                border: none;
                border-radius: 8px;
                color: #CCCCCC;
                font-size: 15px;
                padding: 10px 0;
                outline: none;
            }
            QListWidget::item {
                padding: 10px 20px;
                border-radius: 6px;
            }
            QListWidget::item:selected {
                background-color: #4CAF50;
                color: white;
            }
            QListWidget::item:hover {
                background-color: #3A3A3A;
            }
            QScrollArea {
                background: transparent;
                border: none;
            }
            QWidget#card {
                background-color: #2D2D2D;
                border-radius: 12px;
                padding: 12px;
                margin: 6px;
            }
            QWidget#card:hover {
                background-color: #3A3A3A;
            }
            QLabel#name {
                color: #FFFFFF;
                font-size: 15px;
                font-weight: bold;
                margin-top: 6px;
            }
            QLabel#price {
                color: #FFD700;
                font-size: 14px;
            }
            QLabel#desc {
                color: #888888;
                font-size: 11px;
                margin-bottom: 6px;
            }
        """)

        # ---------- 商品数据（联机：由服务端「商店管理」下发；单机：默认蛋糕） ----------
        self.goods_list = []
        if self.is_online:
            self._load_server_goods()
        if not self.goods_list:
            self.goods_list = self._default_goods()

        # 自定义物品（服务端上架）
        self._load_custom_goods()

        # 连接库存更新信号
        self.firefly.inventory_updated.connect(self.on_inventory_updated)

        self.init_ui()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def showEvent(self, event):
        """每次显示商店都重新拉取一次最新商品（保证分区/上下架/名字是最新的）"""
        super().showEvent(event)
        try:
            self.refresh_online_state()
        except Exception as e:
            print(f"[商店] 显示时刷新失败: {e}")

    def _show_loading(self):
        """进入商店时的加载态：网格清空并提示「正在加载…」"""
        try:
            while self.grid.count():
                child = self.grid.takeAt(0)
                w = child.widget() if child is not None else None
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            lab = QLabel("正在加载…")
            lab.setStyleSheet("color:#9a9a9a; font-size:16px;")
            lab.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(lab, 0, 0, 1, 3)
        except Exception:
            pass
        self._set_status("正在加载…", '#9a9a9a')

    def refresh_online_state(self):
        """重读联机标志并按"最佳可用数据"重绘。
        每次显示商店、以及复用已可见窗口时都要走一次：窗口可能在离线时创建，
        之后才登录成功；只靠 __init__ 取一次会一直当成单机（表现为"商店连接不上"）。"""
        try:
            self.is_online = bool(getattr(self.firefly.mp_client, 'connected', False))
        except Exception:
            pass
        if self.is_online:
            # 每次进入都先显示「正在加载…」，再向服务端拉取最新数据重绘
            # （服务端未响应时由重试/超时兜底逻辑自动改用本地数据，不会一直空白）
            self._show_loading()
            self.request_shop_goods()
        else:
            # 单机：直接用默认商品重绘（不显示"单机模式"之类文字）
            goods, src = self._best_available_goods()
            self.apply_shop_goods(goods, src)

    def closeEvent(self, event):
        """窗口关闭时断开信号连接，并清掉主窗口的引用
        （QDialog.close() 不会派发 finished，不主动清引用会导致之后再点商店"打不开"）"""
        try:
            self._stop_shop_retry()
        except Exception:
            pass
        try:
            self.firefly.inventory_updated.disconnect(self.on_inventory_updated)
        except Exception:
            pass
        try:
            if getattr(self.firefly, 'shop_window', None) is self:
                self.firefly.shop_window = None
        except Exception:
            pass
        event.accept()

    def _get_inventory(self):
        if self.firefly.mp_client.connected and self.firefly.online_inventory is not None:
            return self.firefly.online_inventory
        return self.firefly.current_user

    def _get_balance(self, currency_type):
        return shop_balance(self._get_inventory(), currency_type)

    def init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)

        # 左侧分类列表
        left_widget = QWidget()
        left_widget.setFixedWidth(150)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.category_list = QListWidget()
        self._rebuild_categories()
        self.category_list.setCurrentRow(0)
        self.category_list.itemClicked.connect(self.on_category_changed)
        # 只接 itemClicked 的话，"程序化切换分区"（重建分类/自动切到有货分区）不会触发重绘，
        # 表现为"上架后分类还是空、要手动点一下别的分类再点回来才出来"。
        # 这里再接一个 currentItemChanged，让程序化切换同样立即重绘。
        self.category_list.currentItemChanged.connect(
            lambda cur, _prev: self.on_category_changed(cur))
        left_layout.addWidget(self.category_list)

        main_layout.addWidget(left_widget)

        # 右侧网格
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        title_row = QHBoxLayout()
        title = QLabel("🛒 商店")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #FFFFFF; margin-bottom: 10px;")
        title_row.addWidget(title, 1)
        # 已按要求去掉「刷新」按钮：每次进入商店都会自动拉取最新数据并重绘
        self.refresh_btn = None
        right_layout.addLayout(title_row)
        # 状态行控件保留（内部仍会写入文案/日志），但**不再显示**：
        # 加载态统一显示在网格里（居中「正在加载…」），左上角不再多出一行
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color:#9a9a9a; font-size:12px; margin-bottom:4px;")
        self.status_lbl.hide()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.container = QWidget()
        self.container.setStyleSheet("background: transparent;")
        self.grid = QGridLayout(self.container)
        self.grid.setContentsMargins(10, 10, 10, 10)
        self.grid.setSpacing(15)
        scroll.setWidget(self.container)
        right_layout.addWidget(scroll)

        main_layout.addWidget(right_widget, 1)

        self.current_category = "食物"
        self.refresh_grid()

    def _rebuild_categories(self):
        """按商品分区重建分类列表：食物/兑换/自定义 优先，其余自定义分区按出现顺序追加"""
        if not hasattr(self, 'category_list') or self.category_list is None:
            return
        cur = self.current_category if hasattr(self, 'current_category') else None
        self.category_list.clear()
        # 分区固定三种：美食 / 兑换 / 更多（始终显示这三个）
        cats = ["美食", "兑换", "更多"]
        for c in cats:
            _item = QListWidgetItem(c)
            _item.setData(Qt.UserRole, c)   # 真实分区名（显示只保留分区名，不带数量）
            self.category_list.addItem(_item)
        self.current_category = cur if cur in cats else cats[0]
        self.category_list.setCurrentRow(cats.index(self.current_category))
        # 强制重绘，避免分类文本延迟刷新
        try:
            self.category_list.clearSelection()
            self.category_list.setCurrentRow(cats.index(self.current_category))
            self.category_list.update()
            self.category_list.viewport().update()
        except Exception:
            pass

    # ---- 商店物品图标：内置资源映射（客户端自带） ----
    BUILTIN_SHOP_ICONS = {
        'cake': os.path.join('foods', 'Cake.png'),
        'balance': os.path.join('foods', 'Cake.png'),
        'star_rail_ticket': 'star_rail_ticket.png',
        'ticket': 'star_rail_ticket.png',
    }

    def shop_server_sig(self):
        """服务器识别码（区分进入的是哪个服务器）"""
        try:
            uid = getattr(self.firefly, 'server_uid', None) or getattr(self, 'server_uid', None)
            if uid:
                return ''.join(ch for ch in str(uid) if ch.isalnum() or ch in '_-')[:40]
        except Exception:
            pass
        try:
            # 不把 ip:端口 写进目录名（用户要求）：改用 sha256 短哈希（乱码，稳定不变）
            host = getattr(self.firefly, 'server_host', '') or ''
            port = getattr(self.firefly, 'server_web_port', '') or ''
            raw = f'{host}:{port}'.strip(':')
            if raw:
                return 'h' + hashlib.sha256(('FireflySrv|' + raw).encode('utf-8')).hexdigest()[:16]
        except Exception:
            pass
        return 'h' + hashlib.sha256(b'FireflySrv|unknown').hexdigest()[:16]

    def shop_asset_dir(self):
        """自定义物品资源目录：server/<服务器识别码>/shop/self_area_img/（不存在则创建）"""
        d = os.path.join(get_writable_dir(), 'server', self.shop_server_sig(),
                         'shop', 'self_area_img')
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def sync_shop_images(self):
        """把自定义物品的图片下载到 server/<识别码>/shop/self_area_img/<id>.png（按版本覆盖）"""
        try:
            d = self.shop_asset_dir()
            items = list(getattr(self.firefly, 'server_custom_items', None) or [])
            for g in (getattr(self.firefly, 'shop_goods', None) or []):
                if isinstance(g, dict) and g.get('id'):
                    items.append(g)
            for it in items:
                if not isinstance(it, dict):
                    continue
                iid = it.get('id')
                if not iid:
                    continue
                rel = it.get('image') or it.get('gacha_image') or ''
                if not rel or iid in self.BUILTIN_SHOP_ICONS:
                    continue
                dest = os.path.join(d, f'{iid}.png')
                ver_file = os.path.join(d, f'{iid}.ver')
                ver = str(it.get('ver') or rel)
                cur = ''
                if os.path.exists(ver_file):
                    try:
                        with open(ver_file, 'r', encoding='utf-8') as f:
                            cur = f.read().strip()
                    except Exception:
                        cur = ''
                if os.path.exists(dest) and cur == ver:
                    continue
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                    if self.firefly._download_custom_asset(rel, dest):
                        with open(ver_file, 'w', encoding='utf-8') as f:
                            f.write(ver)
                        print(f'[商店资源] 已下载 {iid} → {dest}')
                except Exception as e:
                    print(f'[商店资源] 下载失败 {iid}: {e}')
        except Exception as e:
            print(f'[商店资源] 同步失败: {e}')

    def shop_item_icon_path(self, item_id):
        """按物品 id 解析图标：自定义下载目录 → 客户端内置资源 → None"""
        iid = str(item_id or '').strip()
        if not iid:
            return None
        custom = os.path.join(self.shop_asset_dir(), f'{iid}.png')
        if os.path.exists(custom):
            return custom
        builtin = self.BUILTIN_SHOP_ICONS.get(iid)
        if builtin:
            p = os.path.join(get_base_dir(), 'assets', 'images', builtin)
            if os.path.exists(p):
                return p
        return None

    def _default_goods(self):
        """默认兜底商品 = 原版两件：橡木蛋糕卷 + 星铁专票
        （服务端未下发 / 全部下架 / 单机模式下至少保证这两件可买）"""
        _tk = os.path.join(get_base_dir(), "assets", "images", "star_rail_ticket.png")
        return [{
            "name": "橡木蛋糕卷",
            "price": 3600,
            "desc": "流萤最喜欢的点心",
            "emoji": "🍰",
            "img_paths": [
                os.path.join(get_base_dir(), "assets", "images", "foods", "Cake.png")
            ],
            "item_type": "cake",
            "amount_per_unit": 1,
            "category": "美食",
            "currency": "credit"
        }, {
            "name": "星铁专票",
            "price": 160,
            "desc": "可用于群星跃迁的专票",
            "emoji": "🎫",
            "img_paths": [_tk] if os.path.exists(_tk) else [],
            "item_type": "star_rail_ticket",
            "amount_per_unit": 1,
            "category": "兑换",
            "currency": "jade"
        }]

    def _best_available_goods(self):
        """按优先级挑选可用商店数据：服务端 → 缓存 → 物品表合成 → 默认原版两件。
        返回 (goods, source)：source ∈ server/cache/synth/default

        这里的 is_online 判断依赖窗口上的标志；该标志在 __init__ 与每次 showEvent /
        reload_server_goods 都会用 mp_client.connected 重新取值，所以不会出现过期值
        （过期会导致"明明在线却当成单机/商店连接不上"）。
        """
        # 单机模式：只用默认原版两件，不显示服务端残留或缓存里的“代币”等商品
        try:
            if not getattr(self, 'is_online', False):
                return self._default_goods(), 'default'
        except Exception:
            pass
        try:
            srv = [g for g in (getattr(self.firefly, 'shop_goods', None) or [])
                   if isinstance(g, dict) and g.get('id') and g.get('enabled')]
            if srv:
                return srv, 'server'
            # 服务端已明确下发过清单（哪怕一件都没上架）：以服务端为准，
            # 不再用缓存/物品表兜底，否则刚下架的商品会被"合成"回来
            if getattr(self.firefly, '_shop_goods_seen', False):
                return [], 'server'
        except Exception:
            pass
        try:
            c = self._load_shop_cache()
            if c:
                return c, 'cache'
        except Exception:
            pass
        try:
            sy = self._synthesize_goods_from_items()
            if sy:
                return sy, 'synth'
        except Exception:
            pass
        return self._default_goods(), 'default'

    def _synthesize_goods_from_items(self):
        """服务端没有下发商店数据时，用本地物品表合成商店内容（兼容未更新的服务端）。
        价格/分区优先取上次成功获取的缓存，其次用物品自带字段。"""
        try:
            cache_map = {g.get('id'): g for g in self._load_shop_cache()
                         if isinstance(g, dict) and g.get('id')}
        except Exception:
            cache_map = {}
        out = []
        # 内置：橡木蛋糕卷
        cake = cache_map.get('cake') or {
            'id': 'cake', 'name': '橡木蛋糕卷', 'price': 3600, 'currency': 'credit',
            'item_type': 'cake', 'enabled': True, 'emoji': '🍰', 'category': '美食'}
        out.append(dict(cake, enabled=True))
        for it in (getattr(self.firefly, 'server_custom_items', None) or []):
            if not isinstance(it, dict) or not it.get('id') or not it.get('shop'):
                continue
            c = cache_map.get(it['id']) or {}
            out.append({
                'id': it['id'],
                'name': it.get('name') or it['id'],
                'price': c.get('price', it.get('price') or 0),
                'currency': c.get('currency') or it.get('currency') or 'credit',
                'item_type': 'custom',
                'enabled': True,
                'emoji': it.get('icon') or '🎁',
                'image': it.get('image') or it.get('gacha_image') or '',
                'category': c.get('category') or '更多',
            })
        return out if len(out) > 1 else []

    def _load_server_goods(self):
        """按服务端下发的商店商品构建 goods_list（商店管理可改价/上下架/加自定义物品）。
        服务端没有下发（或全部下架）时，回退默认商品——保证商店永远不为空。"""
        self.goods_list = []
        self._synth_source = False
        for g in (getattr(self.firefly, 'shop_goods', None) or []):
            if not isinstance(g, dict) or not g.get('id'):
                continue
            if not g.get('enabled'):
                continue   # 已下架的商品不在商店显示
            itype = g.get('item_type') or ('custom' if not g.get('builtin') else g['id'])
            img = None
            try:
                # 先按物品 id 找本地图标（server/<识别码>/shop/self_area_img 或客户端内置资源）
                img = self.shop_item_icon_path(g.get('id'))
            except Exception:
                img = None
            if img is None and g.get('image'):
                try:
                    fake = {'id': g['id'], 'image': g.get('image'), 'ver': g.get('ver') or 0}
                    img = self.firefly.custom_item_image_path(fake)
                except Exception:
                    img = None
            entry = {
                "name": g.get('name') or g['id'],
                "price": int(g.get('price') or 0),
                "desc": g.get('desc') or '',
                "emoji": g.get('emoji') or '🎁',
                "img_paths": [img] if img else [],
                "item_type": itype if itype in ('cake', 'star_rail_ticket') else 'custom',
                "custom_id": g['id'],
                "amount_per_unit": 1,
                "currency": g.get('currency') or 'credit',
            }
            # 分区固定三种：美食 / 兑换 / 更多（非法值按类型推断）
            _cat = g.get('category')
            if _cat not in ('美食', '兑换', '更多'):
                if entry["item_type"] == 'cake' or g.get('id') in ('cake', 'balance'):
                    _cat = '美食'
                elif (entry["item_type"] == 'star_rail_ticket'
                      or g.get('id') in ('star_rail_ticket', 'ticket', 'jade', 'credit')):
                    _cat = '兑换'
                else:
                    _cat = '更多'
            entry["category"] = _cat
            self.goods_list.append(entry)
        if not self.goods_list:
            if getattr(self.firefly, '_shop_goods_seen', False):
                # 服务端已明确下发（可能一件都没上架）：以服务端为准，保持为空。
                # 不能用物品表/缓存兜底，否则刚下架的商品会被"合成"回来
                # （表现为下架后热更新还在，重连后才消失）
                self.goods_list = []
                self._synth_source = False
            else:
                # 服务端未下发（例如服务端 exe 未同步更新）→ 用本地物品表合成商店内容
                synth = self._synthesize_goods_from_items()
                self.goods_list = synth or self._default_goods()
                self._synth_source = bool(synth)

    def reload_server_goods(self):
        """服务端商店内容变化后即时刷新（无需重开商店）；异常不影响之后重新打开"""
        try:
            self.is_online = bool(getattr(self.firefly.mp_client, 'connected', False))
        except Exception:
            pass
        if not self.is_online:
            return
        try:
            # 数据总是更新（下次打开即最新）；界面刷新仅在窗口可见时进行
            self._load_server_goods()
            self._load_custom_goods()
            if not self.isVisible():
                return
            self._rebuild_categories()
            # 上架新商品后：当前分区若已空而其它分区有货 → 自动切过去，保证"立即可见"
            try:
                cats = [self.category_list.item(i).data(Qt.UserRole) or
                        self.category_list.item(i).text()
                        for i in range(self.category_list.count())]
                if self.current_category in cats and not any(
                        g.get("category") == self.current_category for g in self.goods_list):
                    for _c in cats:
                        if any(g.get("category") == _c for g in self.goods_list):
                            self.current_category = _c
                            self.category_list.setCurrentRow(cats.index(_c))
                            break
            except Exception:
                pass
            self.refresh_grid()
            self._log_debug('reload')
            # 强制重绘，避免网格/分类延迟刷新
            try:
                self.grid.update()
                self.container.update()
                self.category_list.viewport().update()
            except Exception:
                pass
        except Exception as e:
            print(f"[商店] 热刷新失败（忽略，可重新打开商店）: {e}")

    def _load_custom_goods(self):
        """补充「自定义」分类商品：
        若服务端已通过 shop_goods 下发（新版），这里不再清理/重复添加，避免把服务端商品清掉；
        仅在没有下发数据时用自定义物品表兜底"""
        if getattr(self.firefly, 'shop_goods', None):
            return
        if getattr(self.firefly, '_shop_goods_seen', False):
            return   # 服务端已下发过清单（哪怕为空）→ 不用物品表兜底
        self.goods_list = [g for g in self.goods_list if g.get("category") != "自定义"]
        if not self.is_online:
            return
        for it in (self.firefly.server_custom_items or []):
            # 手改配置可能出现非法条目，逐条容错，避免整个商店打不开
            if not isinstance(it, dict) or not it.get('shop') or not it.get('id'):
                continue
            img = None
            try:
                img = self.firefly.custom_item_image_path(it)
            except Exception:
                img = None
            try:
                _price = max(0, int(it.get('price') or 0))
            except Exception:
                _price = 0
            self.goods_list.append({
                "name": it.get('name') or it.get('id'),
                "price": _price,
                "desc": it.get('description') or '',
                "emoji": it.get('icon') or ('👗' if it.get('type') == 'clothes' else '🎁'),
                "img_paths": [img] if img else [],
                "item_type": "custom",
                "custom_id": it.get('id'),
                "amount_per_unit": 1,
                "category": "更多",
                "currency": it.get('currency') or 'credit',
            })

    def reload_custom_goods(self):
        """服务端物品变更后即时刷新（无需重开商店）"""
        if not hasattr(self, 'category_list') or self.category_list is None:
            # 界面还没构建完成，等初始化时自然带上新商品
            return
        self._load_custom_goods()
        self._rebuild_categories()
        self.refresh_grid()

    def on_category_changed(self, item):
        if item is None:
            return
        try:
            self.current_category = item.data(Qt.UserRole) or item.text()
        except Exception:
            return
        self.refresh_grid()

    # ---------- 商店数据：缓存 / 请求 / 状态 ----------
    def _cache_path(self):
        return os.path.join(get_writable_dir(), 'shop_cache.json')

    def _save_shop_cache(self, goods):
        try:
            with open(self._cache_path(), 'w', encoding='utf-8') as f:
                json.dump({'goods': goods or []}, f, ensure_ascii=False)
        except Exception as e:
            print(f"[商店] 缓存写入失败: {e}")

    def _load_shop_cache(self):
        try:
            p = self._cache_path()
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    return (json.load(f) or {}).get('goods') or []
        except Exception as e:
            print(f"[商店] 缓存读取失败: {e}")
        return []

    def _set_status(self, text, color='#9a9a9a'):
        try:
            if getattr(self, 'status_lbl', None) is not None:
                self.status_lbl.setText(text)
                self.status_lbl.setStyleSheet(
                    f"color:{color}; font-size:12px; margin-bottom:4px;")
        except Exception:
            pass

    def apply_shop_goods(self, goods, source='server'):
        """统一入口：更新内存/缓存并重建列表（响应、缓存、手动刷新都走这里）"""
        goods = [g for g in (goods or []) if isinstance(g, dict) and g.get('id')]
        if source == 'server':
            self.firefly._shop_goods_seen = True    # 服务端来源：哪怕空清单也算已下发
            self._stop_shop_retry()                 # 拿到了就不再重试
        if goods:
            self.firefly.shop_goods = goods
            self._save_shop_cache(goods)
        try:
            self._load_server_goods()
            self._load_custom_goods()
            self._rebuild_categories()
            self.refresh_grid()
            self._log_debug('apply:' + source)
            # 状态行只用于"加载中/异常"提示：正常拿到服务端数据时清空，不再显示模式与版本
            if getattr(self, '_synth_source', False):
                self._set_status("⚠ 服务端未响应，已用本地物品表显示商店", '#ffd76a')
            elif source == 'cache':
                self._set_status("正在向服务端获取最新数据…", '#ffd76a')
            elif source in ('synth', 'default'):
                self._set_status("⚠ 服务端未提供商店数据，已用本地内容显示", '#ffd76a')
            elif source == 'server':
                self._set_status("", '#9a9a9a')
            else:
                self._set_status("", '#9a9a9a')
        except Exception as e:
            print(f"[商店] 应用数据失败: {e}")

    def request_shop_goods(self, tries=3):
        """主动拉取：发出请求后**持续重试直到拿到服务端数据**（在线时一直显示"正在加载…"），
        不回退显示本地缓存/合成数据。"""
        try:
            mp = getattr(self.firefly, 'mp_client', None)
            if mp is None or not getattr(mp, 'connected', False):
                self._set_status("", '#9a9a9a')
                return
            self._set_status("正在加载…", '#9a9a9a')
            try:
                mp._send({'type': 'request_shop_goods', 'data': {}})
                print('[商店] 已请求最新商店数据')
            except Exception as e:
                print(f"[商店] 请求失败: {e}")
            self._start_shop_retry()
        except Exception as e:
            print(f"[商店] 请求失败: {e}")

    def _start_shop_retry(self):
        """每 2 秒重发一次请求，直到拿到服务端商店数据（或窗口关闭）"""
        try:
            if getattr(self, '_shop_retry_timer', None) is None:
                self._shop_retry_timer = QTimer(self)
                self._shop_retry_timer.timeout.connect(self._retry_request)
            if not self._shop_retry_timer.isActive():
                self._shop_retry_timer.start(2000)
        except Exception:
            pass

    def _stop_shop_retry(self):
        try:
            t = getattr(self, '_shop_retry_timer', None)
            if t is not None and t.isActive():
                t.stop()
        except Exception:
            pass

    def _warn_if_no_response(self):
        """3.5 秒后仍没有服务端数据 → 用本地数据渲染并明确提示（多为服务端未更新/未响应）"""
        try:
            if getattr(self.firefly, 'shop_goods', None):
                return
            goods, src = self._best_available_goods()
            self.apply_shop_goods(goods, src)
            self._set_status(
                f"⚠ 服务端未响应，已显示本地数据（{len(self.goods_list)} 件）", '#ff9a9a')
            self._log_debug('no-response')
        except Exception:
            pass

    def _retry_request(self):
        try:
            mp = getattr(self.firefly, 'mp_client', None)
            if mp is None or not getattr(mp, 'connected', False):
                return
            if len(getattr(self.firefly, 'shop_goods', None) or []) > 0:
                self._stop_shop_retry()   # 已拿到数据，停止重试
                return
            self._set_status("正在加载…", '#9a9a9a')
            mp._send({'type': 'request_shop_goods', 'data': {}})
            print('[商店] 重试请求商店数据')
        except Exception:
            pass

    def _shop_log(self, note):
        """商店收发诊断（写入 shop_debug.log，便于定位"下架后没刷新"这类问题）"""
        try:
            _p = os.path.join(get_writable_dir(), 'shop_debug.log')
            with open(_p, 'a', encoding='utf-8') as f:
                f.write("%s %s" % (datetime.now().strftime('%H:%M:%S'), note) + chr(10))
        except Exception:
            pass

    def _log_debug(self, note):
        """把商店刷新状态写入日志文件（shop_debug.log），便于排查刷新问题"""
        try:
            p = os.path.join(get_writable_dir(), 'shop_debug.log')
            with open(p, 'a', encoding='utf-8') as f:
                f.write("%s %s | server=%d goods=%d cats=%s" % (
                    datetime.now().strftime('%H:%M:%S'), note,
                    len(getattr(self.firefly, 'shop_goods', None) or []),
                    len(self.goods_list),
                    [self.category_list.item(i).text()
                     for i in range(self.category_list.count())]) + chr(10))
        except Exception:
            pass

    def _update_title_count(self):
        """窗口/页面标题固定为「商店」（不再显示件数）"""
        try:
            self.setWindowTitle("商店")
        except Exception:
            pass

    def refresh_grid(self):
        self._update_title_count()   # 标题同步件数
        # 清空网格：takeAt 只是把控件移出布局，控件依然是网格的子控件、
        # 依然可见（旧卡片会盖在界面上最久到 deleteLater 生效为止）；
        # 必须先 setParent(None) 立刻摘掉，再 deleteLater 释放 —— 否则
        # "下架/改价后界面还是旧的"，要退出重进才刷新。
        while self.grid.count():
            child = self.grid.takeAt(0)
            w = child.widget() if child is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        filtered = [g for g in self.goods_list if g.get("category") == self.current_category]
        col_count = 2 if len(filtered) <= 2 else 3
        for index, goods in enumerate(filtered):
            row = index // col_count
            col = index % col_count
            card = self._create_card(goods)
            self.grid.addWidget(card, row, col, 1, 1, Qt.AlignTop)

        if not filtered:
            label = QLabel("暂无商品")
            label.setStyleSheet("color: #888888; font-size: 16px;")
            label.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(label, 0, 0, 1, col_count)

    def _create_card(self, goods):
        card = QWidget()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        # 图标
        icon_label = QLabel()
        icon_label.setFixedSize(96, 96)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("background: transparent;")
        pixmap = None
        for path in goods.get("img_paths", []):
            if path and os.path.exists(path):
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    break
        if pixmap and not pixmap.isNull():
            pixmap = pixmap.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        else:
            icon_label.setText(goods.get("emoji") or "🎁")
            icon_label.setStyleSheet("font-size: 62px; background: transparent;")
        layout.addWidget(icon_label, alignment=Qt.AlignCenter)

        # 名称
        name_label = QLabel(goods["name"])
        name_label.setObjectName("name")
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        # 价格（不再显示描述）
        currency = goods.get("currency", "credit")
        price_text = f"{goods['price']} {shop_currency_name(currency)}"
        price_label = QLabel(price_text)
        price_label.setObjectName("price")
        layout.addWidget(price_label)

        # 购买按钮
        buy_btn = self._create_buy_button(goods)
        layout.addWidget(buy_btn)

        # 保存按钮引用以便更新
        card.buy_btn = buy_btn
        card.goods = goods

        return card

    def _create_buy_button(self, goods):
        btn = QPushButton("购买")
        btn.setFixedWidth(80)
        currency = goods.get("currency", "credit")
        balance = self._get_balance(currency)
        # 0 价（免费）商品始终可购买；否则按余额判断
        can_buy = True if goods['price'] <= 0 else (balance // goods['price']) >= 1
        btn.setEnabled(can_buy)
        if can_buy:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    padding: 6px 14px;
                    font-size: 13px;
                }
                QPushButton:hover {
                    background-color: #45A049;
                }
                QPushButton:pressed {
                    background-color: #3D8B40;
                }
            """)
        else:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #666666;
                    color: #AAAAAA;
                    border: none;
                    border-radius: 6px;
                    padding: 6px 14px;
                    font-size: 13px;
                }
            """)
        btn.clicked.connect(lambda checked, g=goods: self._open_exchange_dialog(g))
        return btn

    def on_inventory_updated(self):
        """库存更新时刷新所有卡片"""
        # 重新生成所有卡片
        self.refresh_grid()

    def _open_exchange_dialog(self, goods):
        dlg = ShopExchangeDialog(self.firefly, goods, self)
        dlg.exec_()

# ====================== 兑换弹窗（包含描述） ======================
class ShopExchangeDialog(QDialog):
    """
    通用兑换弹窗，支持 credit 和 jade 两种货币。
    滑块上限：可购买数量超过 9999 时显示 9999，否则显示实际可购买数。
    支持库存更新信号，实时刷新余额和滑块。
    """
    def __init__(self, firefly, item_data, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        self.item_data = item_data
        self.setWindowTitle("兑换")
        self.setFixedSize(*fit_4_3(550, 400))
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 12px;
            }
            QLabel {
                color: #CCCCCC;
                font-size: 14px;
            }
            QLabel#title {
                color: #FFFFFF;
                font-size: 18px;
                font-weight: bold;
            }
            QLabel#desc {
                color: #888888;
                font-size: 13px;
            }
            QSlider {
                height: 20px;
            }
            QSlider::groove:horizontal {
                border: 1px solid #444444;
                background: #2D2D2D;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #4CAF50;
                width: 18px;
                height: 18px;
                margin: -6px 0;
                border-radius: 9px;
            }
            QSlider::handle:horizontal:hover {
                background: #45A049;
            }
            QSlider::sub-page:horizontal {
                background: #4CAF50;
                border-radius: 3px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 20px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton:pressed {
                background-color: #3D8B40;
            }
            QPushButton#cancel {
                background-color: #666666;
            }
            QPushButton#cancel:hover {
                background-color: #888888;
            }
        """)

        # 连接库存更新信号
        self.firefly.inventory_updated.connect(self._on_inventory_updated)

        # 获取库存和货币信息
        self.inv = self._get_inventory()
        self.currency = self.item_data.get("currency", "credit")
        self.balance = shop_balance(self.inv, self.currency)
        self.currency_name = shop_currency_name(self.currency)

        self.price = self.item_data['price']
        # 计算最大可购买数量：免费商品不限量（上限 9999）
        if self.price <= 0:
            self.max_quantity = 9999
        else:
            max_possible = self.balance // self.price
            # 上限限制：若超过 9999 则设为 9999，否则为实际数量
            self.max_quantity = 9999 if max_possible >= 9999 else max_possible

        self.init_ui()
        self.update_display()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def closeEvent(self, event):
        """窗口关闭时断开信号连接"""
        try:
            self.firefly.inventory_updated.disconnect(self._on_inventory_updated)
        except Exception:
            pass
        event.accept()

    def _get_inventory(self):
        if self.firefly.mp_client.connected and self.firefly.online_inventory is not None:
            return self.firefly.online_inventory
        return self.firefly.current_user

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(25, 25, 25, 20)
        main_layout.setSpacing(15)

        # ---- 顶部：图标 + 标题/描述 ----
        top_layout = QHBoxLayout()
        top_layout.setSpacing(15)

        icon_label = QLabel()
        icon_label.setFixedSize(96, 96)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("background: transparent;")
        pixmap = None
        for path in self.item_data.get("img_paths", []):
            if os.path.exists(path):
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    break
        if pixmap and not pixmap.isNull():
            pixmap = pixmap.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        else:
            icon_label.setText(self.item_data.get("emoji") or "🎁")
            icon_label.setStyleSheet("font-size: 62px; background: transparent;")
        top_layout.addWidget(icon_label)

        info_layout = QVBoxLayout()
        name_label = QLabel(self.item_data["name"])
        name_label.setObjectName("title")
        info_layout.addWidget(name_label)

        if self.item_data.get("desc"):
            desc_label = QLabel(self.item_data["desc"])
            desc_label.setObjectName("desc")
            desc_label.setWordWrap(True)
            info_layout.addWidget(desc_label)

        top_layout.addLayout(info_layout, 1)
        main_layout.addLayout(top_layout)

        # ---- 中间：滑块 + 数量 ----
        slider_layout = QHBoxLayout()
        slider_layout.setSpacing(15)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, self.max_quantity)
        initial_value = 1 if self.max_quantity >= 1 else 0
        self.slider.setValue(initial_value)
        self.slider.setEnabled(self.max_quantity > 0)
        self.slider.valueChanged.connect(self.on_slider_changed)
        slider_layout.addWidget(self.slider, 1)

        self.quantity_label = QLabel(f"兑换数量：{initial_value}")
        self.quantity_label.setFixedWidth(120)
        self.quantity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.quantity_label.setStyleSheet("color: #FFFFFF; font-size: 15px;")
        slider_layout.addWidget(self.quantity_label)

        main_layout.addLayout(slider_layout)

        # ---- 余额和总价 ----
        info_bottom_layout = QVBoxLayout()
        info_bottom_layout.setSpacing(5)

        self.balance_label = QLabel()
        self.balance_label.setStyleSheet("color: #AAAAAA; font-size: 14px;")
        info_bottom_layout.addWidget(self.balance_label)

        self.total_label = QLabel()
        self.total_label.setStyleSheet("color: #FFD700; font-size: 16px; font-weight: bold;")
        info_bottom_layout.addWidget(self.total_label)

        main_layout.addLayout(info_bottom_layout)

        # ---- 按钮 ----
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("cancel")
        cancel_btn.clicked.connect(self.reject)

        self.confirm_btn = QPushButton("确认购买")
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(self.do_buy)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.confirm_btn)
        main_layout.addLayout(btn_layout)

    def on_slider_changed(self, value):
        self.quantity_label.setText(f"兑换数量：{value}")
        self.update_display(value)

    def update_display(self, quantity=None):
        if quantity is None:
            quantity = self.slider.value()
        total = quantity * self.price

        self.balance_label.setText(f"当前 {self.currency_name}：{self.balance}")

        if total > self.balance:
            self.total_label.setStyleSheet("color: #FF5555; font-size: 16px; font-weight: bold;")
            self.confirm_btn.setEnabled(False)
        else:
            self.total_label.setStyleSheet("color: #FFD700; font-size: 16px; font-weight: bold;")
            self.confirm_btn.setEnabled(quantity > 0 and self.max_quantity > 0)

        self.total_label.setText(f"总计：{total} {self.currency_name}")

    def _on_inventory_updated(self):
        """库存更新时刷新余额和滑块"""
        self.inv = self._get_inventory()
        self.balance = shop_balance(self.inv, self.currency)

        if self.price <= 0:
            self.max_quantity = 9999
        else:
            max_possible = self.balance // self.price
            self.max_quantity = 9999 if max_possible >= 9999 else max_possible

        self.slider.setRange(0, self.max_quantity)
        self.slider.setEnabled(self.max_quantity > 0)
        if self.max_quantity < 1:
            self.slider.setValue(0)
            self.confirm_btn.setEnabled(False)
        else:
            # 若当前滑块值超过新上限，自动调低
            if self.slider.value() > self.max_quantity:
                self.slider.setValue(self.max_quantity)
        self.update_display()

    def do_buy(self):
        quantity = self.slider.value()
        if quantity <= 0:
            QMessageBox.warning(self, "提示", "请选择至少 1 件商品")
            return

        total = quantity * self.price
        if total > self.balance:
            QMessageBox.warning(self, "提示", f"{self.currency_name}不足")
            return

        # 联机：一切商店交易都交给服务端结算（服务端校验上架状态/价格/余额）
        if self.firefly.mp_client.connected:
            self.firefly.mp_client._send({
                'type': 'buy_shop_item',
                'data': {'id': self.item_data.get('custom_id') or self.item_data.get('item_type'),
                         'qty': quantity}
            })
            self.accept()
            return
        if self.item_data.get('item_type') == 'custom':
            QMessageBox.warning(self, "提示", "自定义物品仅联机模式可用")
            return

        # 扣除货币（按实际币种字段，含星铁专票的复数键）
        _cur_key = shop_currency_key(self.currency)
        self.inv[_cur_key] = self.balance - total

        # 增加物品
        item_type = self.item_data['item_type']
        amount_per_unit = self.item_data.get('amount_per_unit', 1)
        add_amount = quantity * amount_per_unit

        item_desc = ""
        if item_type == 'cake':
            self.inv['balance'] = self.inv.get('balance', 0) + add_amount
            item_desc = f"橡木蛋糕卷 x{add_amount}"
            self.firefly.check_month_reset()
            self.firefly.current_user['monthly_cake_gained'] = self.firefly.current_user.get('monthly_cake_gained', 0) + add_amount
        elif item_type == 'star_rail_ticket':
            self.inv['star_rail_tickets'] = self.inv.get('star_rail_tickets', 0) + add_amount
            item_desc = f"星铁专票 x{add_amount}"
            self.firefly.check_month_reset()
            self.firefly.current_user['monthly_ticket_gained'] = self.firefly.current_user.get('monthly_ticket_gained', 0) + add_amount
            self.firefly.current_user['total_ticket_gained'] = self.firefly.current_user.get('total_ticket_gained', 0) + add_amount
        else:
            QMessageBox.warning(self, "错误", f"未知商品类型：{item_type}")
            return

        # 保存存档并通知库存更新
        if self.firefly.mp_client.connected and self.firefly.online_inventory is not None:
            self.firefly.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.firefly.online_inventory}})
        else:
            self.firefly._save_current_user()

        # 通知库存更新（触发所有窗口刷新）
        self.firefly.notify_inventory_updated()

        # 记录历史
        history = self.firefly.current_user.get('redeem_history', [])
        history.append(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 商店购买 {self.item_data['name']} x{quantity}，花费 {total} {self.currency_name}（获得 {item_desc}）"
        )
        self.firefly.current_user['redeem_history'] = history
        self.firefly._save_current_user()

        self.firefly.update_bag_display()

        QMessageBox.information(self, "购买成功", f"成功购买 {self.item_data['name']} x{quantity}！")
        self.accept()

# ====================== 重置密码窗口 ======================
class ResetPasswordWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🔒")
        self.setWindowTitle("重置密码")
        self.setFixedSize(400, 520)
        # 登录/注册/忘记密码：不进任务栏（Qt.Tool 与抽卡窗口同一套做法）
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                font-size: 14px;
                color: #888888;
                background: transparent;
            }
            QLineEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
                outline: none;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-size: 15px;
                font-weight: 500;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            QPushButton:hover {
                background-color: #45A049;
                box-shadow: 0 4px 8px rgba(0,0,0,0.15);
            }
            QPushButton:pressed {
                background-color: #3D8B40;
            }
            QDateEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QDateEdit::drop-down {
                border: none;
                subcontrol-origin: padding;
                subcontrol-position: right center;
                width: 20px;
            }
            QDateEdit::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #888888;
                width: 0px;
                height: 0px;
                margin-right: 8px;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(15)
        title = QLabel("重置密码")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:22px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("请输入用户名或用户ID")
        layout.addWidget(QLabel("账号："))
        layout.addWidget(self.user_input)
        self.new_pwd_input = QLineEdit()
        self.new_pwd_input.setPlaceholderText("请输入新密码")
        self.new_pwd_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("新密码："))
        layout.addWidget(self.new_pwd_input)
        self.confirm_pwd_input = QLineEdit()
        self.confirm_pwd_input.setPlaceholderText("请再次输入新密码")
        self.confirm_pwd_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("确认新密码："))
        layout.addWidget(self.confirm_pwd_input)
        reset_btn = QPushButton("重置密码")
        reset_btn.clicked.connect(self.do_reset)
        layout.addWidget(reset_btn)
        spacer = QSpacerItem(20, 80, QSizePolicy.Minimum, QSizePolicy.Expanding)
        layout.addItem(spacer)
        self.setLayout(layout)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def do_reset(self):
        key = self.user_input.text().strip()
        new_pwd = self.new_pwd_input.text().strip()
        confirm_pwd = self.confirm_pwd_input.text().strip()
        if not key or not new_pwd or not confirm_pwd:
            QMessageBox.warning(self, "提示", "请填写完整信息")
            return
        if new_pwd != confirm_pwd:
            QMessageBox.warning(self, "提示", "两次密码输入不一致")
            return
        user_file = None
        user_data = None
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json"):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except:
                    continue
                if not isinstance(data, dict):
                    continue  # 跳过非字典格式的文件
                if isinstance(data, dict) and (data.get("username") == key or data.get("userid") == key):
                    user_file = filepath
                    user_data = data
                    break
        if user_file is None:
            QMessageBox.warning(self, "提示", "账号不存在")
            return
        user_data["password"] = new_pwd
        user_data["reset_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_data["redeem_history"] = user_data.get("redeem_history", [])
        user_data["redeem_history"].append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 重置密码")
        with open(user_file, "w", encoding="utf-8") as f:
            json.dump(user_data, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "成功", "密码重置成功！请使用新密码登录")
        self.accept()


# ====================== 注册窗口 ======================
class RegisterWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "📝")
        self.setWindowTitle("注册账号")
        self.setFixedSize(400, 560)
        # 登录/注册/忘记密码：不进任务栏（Qt.Tool 与抽卡窗口同一套做法）
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                font-size: 14px;
                color: #888888;
                background: transparent;
            }
            QLineEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
                outline: none;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-size: 15px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QDateEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                background-color: #2D2D2D;
                color: #888888;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(12)
        title = QLabel("用户注册")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("用户名（唯一）")
        layout.addWidget(QLabel("用户名："))
        layout.addWidget(self.user_input)
        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("用户ID（长度<20，仅字母数字下划线）")
        layout.addWidget(QLabel("用户ID："))
        layout.addWidget(self.id_input)
        self.pwd_input = QLineEdit()
        self.pwd_input.setPlaceholderText("密码")
        self.pwd_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("密码："))
        layout.addWidget(self.pwd_input)
        self.pwd2_input = QLineEdit()
        self.pwd2_input.setPlaceholderText("确认密码")
        self.pwd2_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("确认密码："))
        layout.addWidget(self.pwd2_input)
        self.birth_input = QDateEdit()
        self.birth_input.setCalendarPopup(True)
        self.birth_input.setDate(QDate.currentDate().addYears(-18))
        layout.addWidget(QLabel("生日："))
        layout.addWidget(self.birth_input)
        reg_btn = QPushButton("完成注册")
        reg_btn.clicked.connect(self.do_register)
        layout.addWidget(reg_btn)
        self.setLayout(layout)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def generate_unique_uid(self):
        existing_uids = set()
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json"):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if "uid" in data:
                            existing_uids.add(data["uid"])
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    # 跳过损坏或无法读取的文件
                    continue
        while True:
            rand_num = str(random.randint(0, 10 ** 9 - 1)).zfill(9)
            uid_candidate = f"user-{rand_num}"
            if uid_candidate not in existing_uids:
                return uid_candidate

    def do_register(self):
        username = self.user_input.text().strip()
        userid = self.id_input.text().strip()
        pwd = self.pwd_input.text().strip()
        pwd2 = self.pwd2_input.text().strip()
        birth = self.birth_input.date().toString("yyyy-MM-dd")
        if not username or not userid or not pwd or not pwd2:
            QMessageBox.warning(self, "提示", "请填写完整信息")
            return
        if pwd != pwd2:
            QMessageBox.warning(self, "提示", "两次密码不一致")
            return
        if len(userid) >= 20:
            QMessageBox.warning(self, "提示", "用户ID长度必须小于20")
            return
        if not re.match(r'^[a-zA-Z0-9_]+$', userid):
            QMessageBox.warning(self, "提示", "用户ID只能包含字母、数字和下划线")
            return
        if len(pwd) < 6:
            QMessageBox.warning(self, "提示", "密码长度不能少于 6 位")
            return
        if len(pwd) > 64:
            QMessageBox.warning(self, "提示", "密码长度不能超过 64 位")
            return
        # 系统级保留词不允许作为账户 ID（Python 关键字 + 常见系统词汇）
        try:
            import keyword as _keyword
            _kw = {k.lower() for k in _keyword.kwlist}
        except Exception:
            _kw = set()
        _reserved = {'admin', 'administrator', 'system', 'server', 'root', 'null', 'none',
                     'undefined', 'console', 'guest', 'debugger', 'sys', 'os', 'test',
                     '官方', '系统', '管理员', '所有人', 'all', 'everyone'}
        if userid.lower() in _kw or userid.lower() in _reserved:
            QMessageBox.warning(self, "提示", f"账户 ID「{userid}」是系统保留词，请更换其它 ID")
            return
        userid_path = os.path.join(USER_DATA_DIR, f"{userid}.json")
        if os.path.exists(userid_path):
            QMessageBox.warning(self, "提示", "用户ID已存在")
            return
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except:
                    continue
                if not isinstance(existing, dict):
                    continue
                if existing.get("username") == username:
                    QMessageBox.warning(self, "提示", "用户名已存在")
                    return
        uid = self.generate_unique_uid()
        initial_credit = 99999 if uid.lower().startswith("trailblazer") else 0
        data = {
            "uid": uid,
            "username": username,
            "userid": userid,
            "password": pwd,
            "birthday": birth,
            "register_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "balance": 20,
            "credit": initial_credit,
            "last_sign_date": None,
            "banned": False,
            "banned_until": None,
            "redeemed_codes": [],
            "redeem_history": [f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 注册账号，获得20橡木蛋糕卷" + (
                f"，获得{initial_credit}信用点" if initial_credit > 0 else "")],
            "cursor_style": "None",
            "pet_size": "normal",
            "owned_clothes": ["normal"],
            "mailbox": [],
            "star_rail_tickets": 0,
            "last_birthday_greet_year": None,
            "volume": 0.5
        }
        with open(userid_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "成功", f"注册成功！\n用户ID：{userid}\nUID：{uid}")
        self.accept()


# ====================== 登录窗口 ======================
class LoginWindow(QDialog):
    login_success = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🔑")
        self.setWindowTitle("用户登录")
        self.setFixedSize(400, 520)
        # 登录/注册/忘记密码：不进任务栏（Qt.Tool 与抽卡窗口同一套做法）
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                font-size: 14px;
                color: #888888;
                background: transparent;
            }
            QLineEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
                outline: none;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-size: 15px;
                font-weight: 500;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            QPushButton:hover {
                background-color: #45A049;
                box-shadow: 0 4px 8px rgba(0,0,0,0.15);
            }
            QPushButton:pressed {
                background-color: #3D8B40;
            }
            QCheckBox {
                font-size: 13px;
                color: #888888;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 8px;
                border: 2px solid #555555;
                background-color: #2D2D2D;
            }
            QCheckBox::indicator:hover {
                border-color: #4CAF50;
            }
            QCheckBox::indicator:checked {
                background-color: #4CAF90;
                border-color: #FFFFFF;
            }
            QLabel#forgetPwdLabel {
                color: #4CAF50;
                font-size: 13px;
                text-decoration: underline;
            }
            QLabel#forgetPwdLabel:hover {
                color: #45A049;
                cursor: pointer;
            }
        """)
        self.current_user = None
        self.init_ui()
        self.load_remember()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())


    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(15)

        title = QLabel("登录")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:22px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)

        logo_path = os.path.join(get_base_dir(), "assets", "images", "icon", "Login_img.png")
        pixmap = QPixmap(logo_path)
        pixmap = pixmap.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        transparent_pixmap = QPixmap(pixmap.size())
        transparent_pixmap.fill(Qt.transparent)
        painter = QPainter(transparent_pixmap)
        painter.setOpacity(0.5)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        logo_label = QLabel()
        logo_label.setPixmap(transparent_pixmap)
        logo_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo_label)

        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("用户名 / 用户ID")
        layout.addWidget(QLabel("账号："))
        layout.addWidget(self.user_edit)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setPlaceholderText("密码")
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("密码："))
        layout.addWidget(self.pwd_edit)

        remember_forget_layout = QHBoxLayout()
        self.remember = QCheckBox("记住登录状态")
        self.remember.setStyleSheet("color:#888888;")
        self.forget_pwd_label = QLabel("忘记密码？")
        self.forget_pwd_label.setObjectName("forgetPwdLabel")
        self.forget_pwd_label.setAlignment(Qt.AlignRight)
        self.forget_pwd_label.mousePressEvent = self.open_reset_password
        remember_forget_layout.addWidget(self.remember)
        remember_forget_layout.addWidget(self.forget_pwd_label)
        layout.addLayout(remember_forget_layout)

        btn_layout = QHBoxLayout()
        login_btn = QPushButton("登录")
        reg_btn = QPushButton("注册")
        login_btn.clicked.connect(self.do_login)
        reg_btn.clicked.connect(self.open_reg)
        btn_layout.addWidget(login_btn)
        btn_layout.addWidget(reg_btn)
        layout.addLayout(btn_layout)

        spacer = QSpacerItem(20, 60, QSizePolicy.Minimum, QSizePolicy.Expanding)
        layout.addItem(spacer)
        self.setLayout(layout)

    def open_reset_password(self, event):
        reset_win = ResetPasswordWindow(self)
        reset_win.open()

    def closeEvent(self, event):
        event.accept()

    def load_remember(self):
        if not os.path.exists(USER_CONFIG):
            return
        try:
            with open(USER_CONFIG, encoding="utf-8") as f:
                d = json.load(f)
                if d.get("remember"):
                    self.user_edit.setText(d.get("userid", ""))
                    self.pwd_edit.setText(d.get("password", ""))
                    self.remember.setChecked(True)
        except:
            pass

    def save_remember(self):
        d = {
            "userid": self.current_user["userid"],
            "password": self.current_user["password"],
            "remember": self.remember.isChecked()
        }
        with open(USER_CONFIG, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    def open_reg(self):
        reg_win = RegisterWindow(self)
        reg_win.open()

    def do_login(self):
        import traceback
        log_path = os.path.join(os.environ.get('TEMP', '.'), 'firefly_crash.log')
        try:
            key = self.user_edit.text().strip()
            pwd = self.pwd_edit.text().strip()
            if not key or not pwd:
                QMessageBox.warning(self, "提示", "账号和密码不能为空")
                return

            found_user = None
            user_filepath = None
            for filename in os.listdir(USER_DATA_DIR):
                if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                    filepath = os.path.join(USER_DATA_DIR, filename)
                    data = safe_load_json_with_recovery(filepath)
                    if data is None:
                        continue
                    if data.get("username") == key or data.get("userid") == key:
                        found_user = data
                        user_filepath = filepath
                        break

            if found_user is None:
                QMessageBox.warning(self, "错误", "账号不存在")
                return
            if found_user["password"] != pwd:
                QMessageBox.warning(self, "错误", "密码错误")
                return

            # 修复用户字段（同样确保是字典）
            try:
                found_user = ensure_user_fields(found_user, user_filepath)
            except Exception as e:
                with open(log_path, 'a') as log:
                    log.write(f"ensure_user_fields 失败: {e}\n{traceback.format_exc()}\n")
                QMessageBox.critical(self, "错误", "用户数据修复失败，请联系管理员")
                return

            # 封禁检查（略，保持不变）
            banned_until = found_user.get("banned_until")
            if banned_until:
                try:
                    until_date = datetime.fromisoformat(banned_until)
                    if datetime.now() < until_date:
                        remaining = until_date - datetime.now()
                        days = remaining.days
                        QMessageBox.warning(self, "提示",
                                            f"此用户已被封禁，剩余 {days} 天，解封日期：{until_date.strftime('%Y-%m-%d')}")
                        return
                    else:
                        found_user["banned"] = False
                        found_user["banned_until"] = None
                        with open(user_filepath, "w", encoding="utf-8") as f:
                            json.dump(found_user, f, ensure_ascii=False, indent=2)
                except:
                    pass
            if found_user.get("banned"):
                QMessageBox.warning(self, "提示", "此用户已被禁止使用流萤桌宠")
                return

            self.current_user = found_user
            self.save_remember()
            logging.info(f"[登录] 校验通过，准备创建桌宠 userid={found_user.get('userid')} "
                         f"username={found_user.get('username')}")
            self.login_success.emit(found_user)
            self.accept()

        except Exception as e:
            with open(log_path, 'a') as log:
                log.write(f"登录过程未捕获异常: {e}\n{traceback.format_exc()}\n")
            QMessageBox.critical(self, "严重错误", f"登录时发生未知错误：{str(e)}\n详情已写入 {log_path}")

class ChangeNameDialog(QDialog):
    def __init__(self, parent=None, current_name="", remaining_changes=1, server_timezone_offset=0):
        super().__init__(parent)
        self.parent_firefly = parent
        self.current_name = current_name
        self.remaining_changes = remaining_changes
        self.server_timezone_offset = server_timezone_offset
        self.all_names = []  # 从服务端获取的所有在线玩家名字
        self._next_time = ""

        set_window_emoji_icon(self, "✏️")
        self.setWindowTitle("修改昵称")
        self.setFixedSize(460, 300)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit {
                background-color: #2D2D2D;
                color: white;
                border: 1px solid #555;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QLineEdit:focus { border-color: #4CAF50; }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; }
        """)
        self.init_ui()
        self.request_player_list()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def on_check_name_result(self, available, reason):
        if available:
            self.status_label.setText('<span style="color:green;">昵称可用</span>')
            self.ok_btn.setEnabled(True)
        else:
            self.status_label.setText(f'<span style="color:red;">{reason}</span>')
            self.ok_btn.setEnabled(False)

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        # 当前昵称
        layout.addWidget(QLabel("当前昵称:"))
        current_lbl = QLabel(self.current_name)
        current_lbl.setStyleSheet("color: #CCCCCC; font-size: 14px;")
        layout.addWidget(current_lbl)

        # 新昵称输入
        layout.addWidget(QLabel("新昵称 (最多16字符):"))
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(16)
        self.name_edit.setFixedHeight(32)
        self.name_edit.textChanged.connect(self.on_text_changed)
        layout.addWidget(self.name_edit)

        # 状态提示（小字）
        self.status_label = QLabel()
        self.status_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.status_label)

        # 剩余次数（小字）
        self.remaining_label = QLabel()
        self.update_remaining_label()
        layout.addWidget(self.remaining_label)

        # 按钮
        btn_layout = QHBoxLayout()
        self.ok_btn = QPushButton("确定")
        self.ok_btn.clicked.connect(self.on_ok_clicked)
        self.ok_btn.setEnabled(False)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def on_ok_clicked(self):
        """点击确定按钮时的处理：弹出确认框，确认后提交"""
        new_name = self.get_new_name()
        if not new_name:
            return

        # 构建确认消息，新昵称用绿色高亮
        msg = f'是否确定更改为：<span style="color:green; font-weight:bold;">{new_name}</span>'
        reply = QMessageBox.question(
            self,
            "确认改名",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.accept()

    def request_player_list(self):
        """请求在线玩家列表以检查重名"""
        if self.parent_firefly and hasattr(self.parent_firefly, 'mp_client'):
            self.parent_firefly.mp_client.request_player_list()

    def update_player_list(self, players):
        """更新在线玩家列表"""
        # 从 players 中提取名字，排除自己
        self.all_names = [p.get('name', '') for p in players if p.get('name') and p.get('name') != self.current_name]
        # 重新检查当前输入
        self.on_text_changed(self.name_edit.text())

    def update_remaining_label(self):
        """更新剩余状态显示（可修改/冷却中）"""
        if self.remaining_changes >= 1:
            color = "green"
            text = "✅ 昵称可修改"
        else:
            color = "red"
            text = "⏳ 修改冷却中"
        self.remaining_label.setText(f'<span style="color:{color};">{text}</span>')

    def on_text_changed(self, text):
        new_name = text.strip()
        if not new_name:
            self.status_label.setText("")
            self.ok_btn.setEnabled(False)
            return

        if not Firefly.is_valid_nickname(new_name):
            if new_name.startswith("开拓者-") and new_name != "开拓者":
                self.status_label.setText('<span style="color:red;">此昵称不合法</span>')
            else:
                self.status_label.setText('<span style="color:red;">昵称不合法</span>')
            self.ok_btn.setEnabled(False)
            return

        # 异步检查重名（通过父 Firefly 发送）
        if self.parent_firefly and hasattr(self.parent_firefly, 'mp_client'):
            self.ok_btn.setEnabled(False)
            self.status_label.setText('<span style="color:#AAAAAA;">检查中...</span>')
            self.parent_firefly.mp_client._send({'type': 'check_name', 'data': {'new_name': new_name}})

    def get_new_name(self):
        return self.name_edit.text().strip()

    def get_remaining_changes(self):
        return self.remaining_changes

    def set_remaining_changes(self, remaining, next_time_str=""):
        self.remaining_changes = remaining
        self._next_time = next_time_str  # 保存以便后续使用
        self.update_remaining_label()
        if remaining <= 0:
            self.ok_btn.setEnabled(False)
            # 使用换行符 \n 将日期放在第二行
            self.status_label.setText(
                f'<span style="color:red;">冷却期未到！\n下次可修改时间为 {next_time_str}</span>'
            )
        else:
            self.ok_btn.setEnabled(True)
            self.status_label.setText("")

import socket, time, threading, json, os
from PyQt5.QtCore import QTimer, pyqtSignal

class AddServerDialog(QDialog):
    """添加/编辑自定义服务器的对话框"""
    def __init__(self, parent=None, name='', addr=''):
        super().__init__(parent)
        set_window_emoji_icon(self, "➕")
        self.setWindowTitle("编辑服务器")
        self.setFixedSize(480, 320)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 20px; }
            QLabel { color: #CCCCCC; font-size: 20px; }
            QLineEdit { background-color: #2D2D2D; color: white; border:1px solid #555; border-radius:5px; padding:6px; }
            QLineEdit:focus { border-color: #4CAF50; }
            QPushButton { background-color: #4CAF50; color: white; border:none; border-radius:5px; padding:6px 16px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(QLabel("服务器名称："))
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("例如：流萤桌宠服务器")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("服务器地址 (IP:端口)："))
        self.addr_edit = QLineEdit(addr)
        self.addr_edit.setPlaceholderText("例如：127.0.0.1:20082")
        layout.addWidget(self.addr_edit)

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def get_server_info(self):
        name = self.name_edit.text().strip() or "自定义服务器"
        addr = self.addr_edit.text().strip()
        if ':' in addr:
            host, port = addr.split(':', 1)
            try:
                port = int(port)
            except:
                port = 20082
        else:
            host = addr
            port = 20082
        return name, host, port

class DirectJoinDialog(QDialog):
    """快速加入服务器对话框（独立弹出，居中父窗口）"""
    join_request = pyqtSignal(str, int)  # 发射 (host, port)

    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🚀")
        self.setWindowTitle("快速加入服务器")
        self.setFixedSize(420, 230)   # 增加高度，避免底部被遮挡
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #CCCCCC;
                font-size: 14px;
            }
            QLineEdit {
                background-color: #2D2D2D;
                color: white;
                border: 1px solid #555;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 20px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton#cancel_btn {
                background-color: #666666;
            }
            QPushButton#cancel_btn:hover {
                background-color: #888888;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(15)

        # 标题
        title = QLabel("🔗 输入服务器地址")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #FFFFFF;")
        layout.addWidget(title)

        # IP地址输入框
        self.addr_edit = QLineEdit()
        self.addr_edit.setPlaceholderText("例如：127.0.0.1:20082")
        layout.addWidget(QLabel("地址 (IP:端口)："))
        layout.addWidget(self.addr_edit)

        # 按钮区域
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("加入")
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("cancel_btn")
        ok_btn.clicked.connect(self._on_ok)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        # 调整位置：相对于父窗口居中，若超出屏幕则自动修正
        self._center_on_parent()

    def _center_on_parent(self):
        """将对话框居中于父窗口（如果存在），否则屏幕居中，并确保完全可见"""
        if self.parent():
            parent_rect = self.parent().geometry()
            x = parent_rect.x() + (parent_rect.width() - self.width()) // 2
            y = parent_rect.y() + (parent_rect.height() - self.height()) // 2
            # 确保不超出屏幕边界
            screen = QApplication.primaryScreen().availableGeometry()
            x = max(screen.x(), min(screen.right() - self.width(), x))
            y = max(screen.y(), min(screen.bottom() - self.height(), y))
            self.move(x, y)
        else:
            screen_center = QApplication.primaryScreen().availableGeometry().center()
            self.move(screen_center - self.rect().center())

    def _on_ok(self):
        addr = self.addr_edit.text().strip()
        if not addr:
            QMessageBox.warning(self, "提示", "请输入服务器地址")
            return

        # 中文冒号转英文冒号
        addr = addr.replace('：', ':')

        if ':' in addr:
            host, port_str = addr.rsplit(':', 1)
            try:
                port = int(port_str)
            except ValueError:
                QMessageBox.warning(self, "错误", "端口号必须是数字")
                return
        else:
            host = addr
            port = 20082

        if not host:
            QMessageBox.warning(self, "错误", "IP地址不能为空")
            return

        self.join_request.emit(host, port)
        self.accept()

class ServerItemWidget(QWidget):
    """Minecraft 多人游戏风格的服务器条目（无图标）：

    第一行：服务器名称（白色加粗）
    第二行：地址（灰） …………… 在线人数（灰） 延迟（按延迟着色）
    """
    def __init__(self, name, ip, port, server_type, parent=None):
        super().__init__(parent)
        self.name = name
        self.ip = ip
        self.port = port
        self.server_type = server_type
        self._status = "检测中..."
        self._latency = None
        self._online = None
        self._max = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 7, 12, 7)
        outer.setSpacing(2)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet(
            "color:#FFFFFF; font-weight:bold; font-size:15px; background:transparent;")
        outer.addWidget(self.name_label)

        info = QHBoxLayout()
        info.setSpacing(14)
        self.addr_label = QLabel(f"{ip}:{port}")
        self.addr_label.setStyleSheet("color:#9A9A9A; font-size:12px; background:transparent;")
        info.addWidget(self.addr_label)
        info.addStretch(1)
        self.online_label = QLabel("在线 --/--")
        self.online_label.setStyleSheet("color:#9A9A9A; font-size:12px; background:transparent;")
        info.addWidget(self.online_label)
        self.latency_label = QLabel("延迟 --")
        self.latency_label.setFixedWidth(96)
        self.latency_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.latency_label.setStyleSheet("color:#9A9A9A; font-size:12px; background:transparent;")
        info.addWidget(self.latency_label)
        outer.addLayout(info)

        self.setStyleSheet("background: transparent;")

    def _set_latency_text(self, text, color):
        self.latency_label.setText(text)
        self.latency_label.setStyleSheet(
            f"color:{color}; font-size:12px; background:transparent;")

    def update_status(self, latency, online, max_):
        if sip.isdeleted(self):
            return
        self._latency = latency
        self._online = online
        self._max = max_
        if latency is not None:
            # 按延迟着色（替代原来的状态图标）
            if latency <= 40:
                color = '#5BD75B'
            elif latency <= 120:
                color = '#FFD24A'
            else:
                color = '#FF6B6B'
            self._set_latency_text(f"{latency} ms", color)
        else:
            self._set_latency_text("延迟 超时", '#9A9A9A')
        if online is not None and max_ is not None:
            self.online_label.setText(f"在线 {online}/{max_}")
        else:
            self.online_label.setText("在线 --/--")
        self._status = "已检测"

    def set_checking(self):
        if sip.isdeleted(self):
            return
        self._set_latency_text("延迟 检测中", '#9A9A9A')
        self.online_label.setText("在线 检测中")
        self._status = "检测中"


class JoinServerDialog(QDialog):
    """服务器选择与加入对话框（单列表，内嵌状态显示）"""
    scan_finished = pyqtSignal(dict, bool)

    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        set_window_emoji_icon(self, "🌐")
        self.setWindowTitle("加入服务器")
        # 增大 1.5 倍（原 700x500 → 1050x750）
        self.setFixedSize(*fit_4_3(1050, 750))
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #CCCCCC; font-size: 13px; }
            QPushButton { background-color: #4CAF50; color: white; border:none; border-radius:5px; padding:6px 16px; }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; color: #AAAAAA; }
            QListWidget { background-color: #2B2B2B; color: white; border:1px solid #555;
                          border-radius:6px; padding:4px; outline:none; }
            QListWidget::item { padding: 0px; border-radius:4px; }
            QListWidget::item:hover { background: #3A3A3A; }
            QListWidget::item:selected { background: #2F4A2A; border: 1px solid #4CAF50; }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(10)

        header = QLabel("选择要加入的服务器（双击条目，或选中后点击「加入服务器」）")
        header.setStyleSheet("color:#8A8A8A; font-size:13px; background:transparent;")
        main_layout.addWidget(header)

        self.server_list = QListWidget()
        self.server_list.setSelectionMode(QListWidget.SingleSelection)
        self.server_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.server_list.itemDoubleClicked.connect(self.join_selected_item)
        self.server_list.currentRowChanged.connect(lambda _r: self.update_button_state())
        main_layout.addWidget(self.server_list, 1)

        # ---------- 底部按钮行（Minecraft 多人游戏风格） ----------
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.scan_btn = QPushButton("📡 扫描局域网")
        self.scan_btn.clicked.connect(self.scan_lan)

        self.direct_join_btn = QPushButton("🚀 直接加入")
        self.direct_join_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                font-weight: bold;
                padding: 8px;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #F57C00;
            }
        """)
        self.direct_join_btn.clicked.connect(self.open_direct_join_dialog)

        self.add_btn = QPushButton("➕ 添加服务器")
        self.add_btn.clicked.connect(self.add_custom_server)

        edit_delete_layout = QHBoxLayout()
        self.edit_btn = QPushButton("✏️ 编辑选中")
        self.edit_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton:disabled {
                background-color: #666666;
                color: #AAAAAA;
            }
        """)
        self.edit_btn.clicked.connect(self.edit_selected_server)
        self.edit_btn.setEnabled(False)

        self.delete_btn = QPushButton("🗑️ 删除")
        self.delete_btn.setStyleSheet("""
            QPushButton {
                background-color: #D32F2F;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background-color: #F44336;
            }
            QPushButton:disabled {
                background-color: #666666;
                color: #AAAAAA;
            }
        """)
        self.delete_btn.clicked.connect(self.delete_selected_server)
        self.delete_btn.setEnabled(False)

        # 主操作按钮（左：加入 / 直接连接 / 添加；右：编辑 / 删除 / 扫描 / 刷新）
        self.join_btn = QPushButton("▶ 加入服务器")
        self.join_btn.setStyleSheet(
            "QPushButton { background-color:#3C8527; color:white; font-weight:bold;"
            " border:none; border-radius:5px; padding:8px 20px; font-size:14px; }"
            "QPushButton:hover { background-color:#4CAF50; }"
            "QPushButton:disabled { background-color:#555555; color:#AAAAAA; }")
        self.join_btn.setDefault(True)
        self.join_btn.clicked.connect(self.join_current)
        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self.refresh_list)
        btn_row.addWidget(self.join_btn)
        btn_row.addWidget(self.direct_join_btn)
        btn_row.addWidget(self.add_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addWidget(self.scan_btn)
        btn_row.addWidget(self.refresh_btn)
        main_layout.addLayout(btn_row)

        # 数据
        self.found_servers = []
        self.custom_servers = []
        self.scan_finished.connect(self.on_scan_finished)

        self.load_custom_servers()
        self.refresh_list()
        self.center_on_screen()
        QTimer.singleShot(100, self.scan_lan)

    def center_on_screen(self):
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    # ---------- 持久化 ----------
    def load_custom_servers(self):
        path = os.path.join(USER_DATA_DIR, 'custom_servers.json')
        self.custom_servers = []  # 默认值
        if not os.path.exists(path):
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                return

            # 修复旧格式：将字符串元素转换为字典
            fixed = []
            needs_fix = False
            for item in data:
                if isinstance(item, str):
                    # 旧格式：只有名称，使用默认 IP 和端口（或从已有服务器中获取？）
                    # 最简单：给一个占位符，用户可以通过编辑按钮修正
                    fixed.append({'name': item, 'ip': '127.0.0.1', 'port': 20082})
                    needs_fix = True
                elif isinstance(item, dict) and 'name' in item:
                    # 确保有 ip 和 port 字段
                    item.setdefault('ip', '127.0.0.1')
                    item.setdefault('port', 20082)
                    fixed.append(item)
                else:
                    # 未知格式，忽略
                    continue
            self.custom_servers = fixed

            # 如果修复了数据，立即保存回文件，避免下次再次修复
            if needs_fix:
                self.save_custom_servers()
        except Exception as e:
            print(f"[加载自定义服务器] 错误: {e}")
            self.custom_servers = []

    def save_custom_servers(self):
        path = os.path.join(USER_DATA_DIR, 'custom_servers.json')
        with open(path, 'w') as f:
            json.dump(self.custom_servers, f, indent=2)

    def refresh_list(self):
        self.server_list.clear()
        # 自定义服务器
        for idx, srv in enumerate(self.custom_servers):
            name = srv.get('name', '自定义服务器')
            widget = ServerItemWidget(name, srv['ip'], srv['port'], 'custom')
            item = QListWidgetItem()
            item.setData(Qt.UserRole, ('custom', idx))
            if self.server_list.count() % 2 == 1:
                item.setBackground(QColor('#333333'))
            item.setSizeHint(widget.sizeHint())
            self.server_list.addItem(item)
            self.server_list.setItemWidget(item, widget)
            # 启动状态检测
            self._check_server_status(widget, srv['ip'], srv['port'])

        # 局域网服务器
        for i, srv in enumerate(self.found_servers):
            name = srv.get('name', '局域网服务器')
            widget = ServerItemWidget(name, srv['ip'], srv['port'], 'found')
            item = QListWidgetItem()
            item.setData(Qt.UserRole, ('found', i))
            if self.server_list.count() % 2 == 1:
                item.setBackground(QColor('#333333'))
            item.setSizeHint(widget.sizeHint())
            self.server_list.addItem(item)
            self.server_list.setItemWidget(item, widget)
            self._check_server_status(widget, srv['ip'], srv['port'])

        # 默认选中第一项
        if self.server_list.count() > 0:
            self.server_list.setCurrentRow(0)
        self.update_button_state()

    def update_button_state(self):
        """根据当前选中项更新编辑/删除按钮状态"""
        item = self.server_list.currentItem()
        if item:
            tag, _ = item.data(Qt.UserRole)
            enable = (tag == 'custom')
        else:
            enable = False
        self.edit_btn.setEnabled(enable)
        self.delete_btn.setEnabled(enable)
        try:
            self.join_btn.setEnabled(item is not None)
        except Exception:
            pass

    # ---------- 状态检测（异步） ----------
    def _check_server_status(self, widget, ip, port):
        """启动一个线程检测服务器状态，完成后更新 widget"""
        def check():
            online, max_, latency = self.query_server_status(ip, port)
            # 在主线程更新 UI
            QMetaObject.invokeMethod(self, "_update_widget_status",
                                     Qt.QueuedConnection,
                                     Q_ARG(object, widget),
                                     Q_ARG(object, latency),
                                     Q_ARG(object, online),
                                     Q_ARG(object, max_))
        threading.Thread(target=check, daemon=True).start()

    @pyqtSlot(object, object, object, object)
    def _update_widget_status(self, widget, latency, online, max_):
        """槽函数：更新 widget 状态（主线程执行）"""
        try:
            if not sip.isdeleted(widget):
                widget.update_status(latency, online, max_)
        except RuntimeError:
            pass  # widget 已被删除

    def query_server_status(self, ip, port, timeout=0.8):
        """查询服务器状态，返回 (online, max, latency_ms)"""
        start = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, port))
            req = json.dumps({'type': 'status'}) + '\n'
            sock.sendall(req.encode())
            data = sock.recv(1024)
            sock.close()
            elapsed = int((time.time() - start) * 1000)
            lines = data.decode('utf-8').strip().split('\n')
            for line in lines:
                if not line:
                    continue
                resp = json.loads(line)
                if resp.get('type') == 'status_response':
                    d = resp.get('data', {})
                    online = d.get('online')
                    max_ = d.get('max')
                    if online is not None and max_ is not None:
                        return online, max_, elapsed
            return None, None, elapsed
        except Exception:
            return None, None, None

    # ---------- 加入功能 ----------
    def join_current(self):
        """加入当前选中的服务器"""
        item = self.server_list.currentItem()
        if item is None:
            QMessageBox.information(self, "加入服务器", "请先选择一个服务器")
            return
        self.join_selected_item(item)

    def join_selected_item(self, item):
        tag, index = item.data(Qt.UserRole)
        if tag == 'custom' and 0 <= index < len(self.custom_servers):
            srv = self.custom_servers[index]
            self.firefly.join_server(srv['ip'], srv['port'])
            self.accept()
        elif tag == 'found' and 0 <= index < len(self.found_servers):
            srv = self.found_servers[index]
            self.firefly.join_server(srv['ip'], srv['port'])
            self.accept()

    # ---------- 自定义服务器操作 ----------
    def add_custom_server(self):
        dlg = AddServerDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            name, host, port = dlg.get_server_info()
            self.custom_servers.append({'name': name, 'ip': host, 'port': port})
            self.save_custom_servers()
            self.refresh_list()
            # 选中新添加的
            idx = len(self.custom_servers) - 1
            self.server_list.setCurrentRow(idx)
            self.update_button_state()

    def edit_selected_server(self):
        item = self.server_list.currentItem()
        if not item:
            return
        tag, index = item.data(Qt.UserRole)
        if tag != 'custom' or not (0 <= index < len(self.custom_servers)):
            return
        srv = self.custom_servers[index]
        dlg = AddServerDialog(self, name=srv.get('name', ''), addr=f"{srv['ip']}:{srv['port']}")
        if dlg.exec_() == QDialog.Accepted:
            name, host, port = dlg.get_server_info()
            self.custom_servers[index] = {'name': name, 'ip': host, 'port': port}
            self.save_custom_servers()
            self.refresh_list()
            self.server_list.setCurrentRow(index)
            self.update_button_state()

    def delete_selected_server(self):
        item = self.server_list.currentItem()
        if not item:
            return
        tag, index = item.data(Qt.UserRole)
        if tag != 'custom' or not (0 <= index < len(self.custom_servers)):
            return
        srv = self.custom_servers[index]
        name = srv.get('name', '未命名')
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("确认删除")
        msg_box.setText(f"确定要删除自定义服务器「{name}」吗？")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                color: #E0E0E0;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
                background-color: #1E1E1E;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton:disabled {
                background-color: #666;
            }
            QPushButton[text="No"] {
                background-color: #666666;
            }
            QPushButton[text="No"]:hover {
                background-color: #888888;
            }
        """)
        parent_geo = self.geometry()
        x = parent_geo.x() + (parent_geo.width() - msg_box.width()) // 2
        y = parent_geo.y() + (parent_geo.height() - msg_box.height()) // 2
        msg_box.move(x, y)
        reply = msg_box.exec_()
        if reply == QMessageBox.Yes:
            del self.custom_servers[index]
            self.save_custom_servers()
            self.refresh_list()
            self.update_button_state()

    # ---------- 扫描局域网 ----------
    def scan_lan(self):
        self.found_servers.clear()
        self.scan_btn.setEnabled(False)
        self.repaint()
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        discovered = {}
        local_found = False
        t0 = time.time()
        local_ok = self._check_port('127.0.0.1', 20082, timeout=1.0)
        if local_ok:
            online, max_ = self.get_server_status('127.0.0.1', 20082)
            discovered['127.0.0.1:20082'] = {
                'ip': '127.0.0.1',
                'port': 20082,
                'name': '本地服务器',
                'online': online,
                'max': max_
            }
            local_found = True
        local_ip = self._get_local_ip()
        if local_ip:
            base = '.'.join(local_ip.split('.')[:3])
            threads = []
            lock = threading.Lock()
            for i in range(1, 255):
                ip = f"{base}.{i}"
                t = threading.Thread(target=self._scan_ip, args=(ip, 20082, discovered, lock))
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=2.0)
        self.scan_finished.emit(discovered, local_found)

    def _check_port(self, ip, port, timeout=0.5):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((ip, port))
            s.close()
            return True
        except:
            return False

    def _scan_ip(self, ip, port, result_dict, lock):
        if self._check_port(ip, port):
            online, max_ = self.get_server_status(ip, port)
            with lock:
                result_dict[f"{ip}:{port}"] = {
                    'ip': ip,
                    'port': port,
                    'name': '局域网服务器',
                    'online': online,
                    'max': max_
                }

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith('127.'):
                return None
            return ip
        except:
            return None

    def get_server_status(self, ip, port, timeout=0.8):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, port))
            req = json.dumps({'type': 'status'}) + '\n'
            sock.sendall(req.encode())
            data = sock.recv(1024)
            sock.close()
            lines = data.decode('utf-8').strip().split('\n')
            for line in lines:
                if not line:
                    continue
                resp = json.loads(line)
                if resp.get('type') == 'status_response':
                    d = resp.get('data', {})
                    online = d.get('online')
                    max_ = d.get('max')
                    if online is not None and max_ is not None:
                        return online, max_
            return None, None
        except Exception:
            return None, None

    def on_scan_finished(self, discovered, local_found):
        self.found_servers = list(discovered.values())
        self.refresh_list()
        self.scan_btn.setEnabled(True)
        if self.server_list.count() > 0:
            self.server_list.setCurrentRow(0)
            self.update_button_state()

    # ---------- 直接加入 ----------
    def open_direct_join_dialog(self):
        dlg = DirectJoinDialog(self)
        dlg.join_request.connect(self._on_direct_join)
        dlg.exec_()

    def _on_direct_join(self, host, port):
        self.firefly.join_server(host, port)
        self.accept()


class PlayerListDialog(QDialog):
    def __init__(self, firefly, players=None, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        self.setWindowTitle("当前在线玩家")
        self.setMinimumSize(*fit_4_3(450, 350))
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 12px;
            }
            QTableWidget {
                background-color: #2D2D2D;
                color: #E0E0E0;
                border: 1px solid #555;
                border-radius: 6px;
                gridline-color: #444;
                font-size: 14px;
                outline: none;
            }
            QTableWidget::item {
                padding: 6px;
            }
            QHeaderView::section {
                background-color: #3A3A3A;
                color: #FFFFFF;
                padding: 8px;
                border: 1px solid #555;
                font-weight: bold;
                font-size: 14px;
            }
            QHeaderView::section:hover {
                background-color: #4A4A4A;
            }
            QTableWidget QTableCornerButton::section {
                background-color: #3A3A3A;
            }
            QLabel {
                color: #CCCCCC;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 20px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["名字", "延迟"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(True)
        # 确保滚动条在需要时显示（默认已启用，显式声明更安全）
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.title_label = QLabel("当前在线玩家 (0人)")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #FFFFFF;")
        layout.addWidget(self.title_label)
        layout.addWidget(self.table)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignCenter)

        self.firefly.mp_client.online_list_updated.connect(self.update_players)
        if players is not None:
            self.update_players(players)

        # 恢复到原来的屏幕居中（不依赖父窗口）
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def update_players(self, players):
        self.table.setRowCount(len(players))
        global_latency = self.firefly.mp_client._latest_latency_ms
        for row, p in enumerate(players):
            name = p.get('name', '未知')
            latency = p.get('latency')
            if latency is None:
                if global_latency >= 0:
                    latency_text = f"{global_latency} ms"
                else:
                    latency_text = "未知"
            else:
                latency_text = f"{latency} ms"
            # 名字按身份着色并加后缀：管理员红色[管理员]、开拓者黄色[开拓者]、普通白色无后缀
            is_admin = (p.get('permission') == 'admin')
            uid = str(p.get('uid') or '')
            is_trail = uid.lower().startswith('trailblazer')
            if is_admin:
                display_name = f"{name}[管理员]"
                color = QColor('#FF5A5A')
            elif is_trail:
                display_name = f"{name}[开拓者]"
                color = QColor('#FFD24A')
            else:
                display_name = name
                color = QColor('#FFFFFF')
            name_item = QTableWidgetItem(display_name)
            name_item.setTextAlignment(Qt.AlignCenter)
            name_item.setForeground(QBrush(color))
            lat_item = QTableWidgetItem(latency_text)
            lat_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, lat_item)
        # 调整列宽：第二列自适应内容，第一列自动填充
        self.table.horizontalHeader().resizeSection(1, self.table.columnWidth(1))
        # 但 Stretch 会覆盖，所以无需额外操作

        count = len(players)
        self.setWindowTitle(f"当前在线玩家 ({count}人)")
        self.title_label.setText(f"当前在线玩家 ({count}人)")

    def closeEvent(self, event):
        try:
            self.firefly.mp_client.online_list_updated.disconnect(self.update_players)
        except TypeError:
            pass
        # 关闭窗口（X / 关闭按钮）后清理持有引用，否则下次点击只会激活一个已隐藏的窗口
        try:
            if getattr(self.firefly, '_player_list_dialog', None) is self:
                self.firefly._player_list_dialog = None
        except Exception:
            pass
        event.accept()

# ==================== 管理员禁言（右键菜单） ====================
MUTE_SPIN_DEFS = [('年', 9999), ('月', 12), ('天', 31), ('小时', 60), ('分钟', 60)]


def _mute_spec_from_parts(y, mo, d, h, mi):
    """把 年/月/天/时/分 拼成服务端 parse_time_spec 支持的时长；全 0 返回 ''（=永久）"""
    parts = []
    if y:
        parts.append(f"{y}Y")
    if mo:
        parts.append(f"{mo}M")
    if d:
        parts.append(f"{d}D")
    if h:
        parts.append(f"{h}H")
    if mi:
        parts.append(f"{mi}m")
    return ''.join(parts)


def _remaining_mute_parts(until_iso):
    """把禁言截止时间换算为剩余 年/月/天/时/分（换算规则与服务端一致：1年=365天、1月=30天，向上取整到分钟）；
    已过期/无效返回 None"""
    if not until_iso:
        return None
    try:
        dt = datetime.fromisoformat(until_iso)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (dt - datetime.now(dt.tzinfo)).total_seconds()
    if secs <= 0:
        return None
    total_minutes = int(secs // 60) + (1 if secs % 60 else 0)
    days, minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    years, days = divmod(days, 365)
    months, days = divmod(days, 30)
    return {'y': min(years, 9999), 'mo': min(months, 12), 'd': min(days, 31),
            'h': min(hours, 60), 'mi': min(minutes, 60)}


def _is_admin_context(firefly):
    """当前用户是否为管理员（联机模式）"""
    try:
        if getattr(firefly, 'is_admin', False):
            return True
        client = getattr(firefly, 'mp_client', None)
        return bool(client is not None and getattr(client, 'server_permission', 'user') == 'admin')
    except Exception:
        return False


def _my_uid(firefly):
    try:
        client = getattr(firefly, 'mp_client', None)
        if client is not None and getattr(client, 'server_uid', ''):
            return client.server_uid
    except Exception:
        pass
    return getattr(firefly, 'online_uid', '') or getattr(firefly, 'server_uid', '')


def _send_admin_command(firefly, cmd):
    try:
        firefly.mp_client.send_command(cmd)
    except Exception as e:
        try:
            QMessageBox.warning(None, "禁言", f"发送命令失败：{e}")
        except Exception:
            pass


def open_mute_duration_dialog(parent, firefly, uid, name, scope, mute, ban_info=None):
    """选择禁言时长并执行；mute=False 时直接解除禁言"""
    scope_label = '全局禁言' if scope == 'global' else '服内禁言'
    if not mute:
        _send_admin_command(firefly, f"/unmute {uid}{' global' if scope == 'global' else ''}")
        # 本地立即清除该用户的禁言缓存（服务端广播随后校正），横幅即时消失
        clear_local = getattr(firefly, 'apply_local_unmute', None)
        if clear_local is not None:
            try:
                clear_local(uid)
            except Exception:
                pass
        return
    info = ban_info if isinstance(ban_info, dict) else {}
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"{scope_label} - {name}")
    dlg.setFixedWidth(400)
    # 深色主题：QDialog 默认白底，浅色提示文字会"看不见"
    dlg.setStyleSheet(
        "QDialog { background-color:#252525; }"
        "QLabel { color:#DDDDDD; font-size:13px; background:transparent; }"
        "QSpinBox { background:#1E1E1E; color:#FFFFFF; border:1px solid #5A5A5A;"
        "           border-radius:4px; padding:4px; font-size:13px; }"
        "QPushButton { background:#3A5A40; color:white; border:none; border-radius:6px;"
        "             padding:6px 18px; font-size:13px; }"
        "QPushButton:hover { background:#4A7A50; }"
        "QPushButton:pressed { background:#2F4A34; }")
    layout = QVBoxLayout(dlg)
    tip = QLabel(f"为 {name} 设置{scope_label}时长（全部为 0 = 永久）：")
    layout.addWidget(tip)
    # 年/月/天/时/分 数字输入
    spins = []
    spin_row = QHBoxLayout()
    for label, vmax in MUTE_SPIN_DEFS:
        sp = QSpinBox()
        sp.setRange(0, vmax)
        sp.setValue(0)
        sp.setAlignment(Qt.AlignCenter)
        sp.setFixedWidth(58)
        spin_row.addWidget(sp)
        spin_row.addWidget(QLabel(label))
        spins.append(sp)
    spin_row.addStretch()
    layout.addLayout(spin_row)
    # 默认预填"上次设定后剩余的时间"（该用户当前仍在禁言中时）
    if scope == 'global':
        prefill = _remaining_mute_parts((info.get('pm_ban') or {}).get('until'))
    else:
        prefill = _remaining_mute_parts((info.get('mute_server') or {}).get('until'))
    if prefill:
        for sp, key in zip(spins, ('y', 'mo', 'd', 'h', 'mi')):
            sp.setValue(prefill[key])
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton("确定")
    cancel_btn = QPushButton("取消")
    btn_row.addWidget(ok_btn)
    btn_row.addWidget(cancel_btn)
    layout.addLayout(btn_row)
    ok_btn.clicked.connect(dlg.accept)
    cancel_btn.clicked.connect(dlg.reject)
    if dlg.exec_() != QDialog.Accepted:
        return
    spec = _mute_spec_from_parts(*[sp.value() for sp in spins])
    cmd = f"/mute {uid}{' global' if scope == 'global' else ''}"
    if spec:
        cmd += ' ' + spec
    _send_admin_command(firefly, cmd)


def build_admin_mute_menu(menu, firefly, uid, name, target_permission=None, ban_info=None, parent=None):
    """管理员右键用户：追加「服内禁言/解除」「全局禁言/解除」两项。
    非管理员、目标是管理员或自己时不追加；返回是否追加了菜单项。"""
    if not _is_admin_context(firefly):
        return False
    # 已注销账号不可禁言（档案已删除）
    if (getattr(firefly, 'destroyed_users', {}) or {}).get(uid):
        return False
    if not uid or uid == _my_uid(firefly):
        return False
    if target_permission == 'admin':
        return False
    info = ban_info if isinstance(ban_info, dict) else {}
    server_mute = info.get('mute_server')
    if not isinstance(server_mute, dict):
        server_mute = (getattr(firefly, 'user_server_mutes', {}) or {}).get(uid) or {}
    pm_ban = info.get('pm_ban')
    if not isinstance(pm_ban, dict):
        ban_text = (getattr(firefly, 'user_pm_bans', {}) or {}).get(uid)
        pm_ban = {'banned': bool(ban_text)}
    is_server_muted = bool(server_mute.get('muted'))
    is_global_muted = bool(pm_ban.get('banned'))
    menu.addSeparator()
    a1 = menu.addAction("🔊 解除服内禁言" if is_server_muted else "🔇 服内禁言")
    a1.triggered.connect(
        lambda: open_mute_duration_dialog(parent, firefly, uid, name, 'server', not is_server_muted, info))
    a2 = menu.addAction("🌐 解除全局禁言" if is_global_muted else "🌐 全局禁言")
    a2.triggered.connect(
        lambda: open_mute_duration_dialog(parent, firefly, uid, name, 'global', not is_global_muted, info))
    return True


class OnlineListOverlay(QFrame):
    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: rgba(0,0,0,0.7); color: white; border-radius: 10px; padding: 15px;")
        self.layout = QVBoxLayout(self)
        self.label = QLabel("在线玩家")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.layout.addWidget(self.label)
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("background: transparent; color: white;")
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_right_click)
        self.layout.addWidget(self.list_widget)
        self.adjustSize()
        self.hide()

    def _on_right_click(self, pos):
        """在线列表右键：管理员可对用户执行服内/全局禁言"""
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        p = item.data(Qt.UserRole) or {}
        uid = p.get('uid')
        if not uid:
            return
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background:#2D2D2D; border:1px solid #555; color:white; }"
                           "QMenu::item { padding:5px 20px; }"
                           "QMenu::item:selected { background:#3A5A40; }")
        if build_admin_mute_menu(menu, self.firefly, uid, p.get('name'),
                                 p.get('permission'),
                                 {'pm_ban': p.get('pm_ban'), 'mute_server': p.get('mute_server')},
                                 self):
            menu.exec_(self.list_widget.mapToGlobal(pos))

    def update_players(self, players):
        self.list_widget.clear()
        for p in players:
            if is_placeholder_player_name(p.get('name')):
                continue
            text = f"{p['name']} (ID: {p['id']})"
            if 'uid' in p:
                text += f" [UID: {p['uid']}]"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, p)
            self.list_widget.addItem(item)
        self.adjustSize()
        # 移动到屏幕上方居中
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.center().x() - self.width() // 2
        y = screen.top() + 30
        self.move(x, y)

    def show_online(self):
        self.show()

    def hide_online(self):
        self.hide()


class ConvListDelegate(QStyledItemDelegate):
    """会话条目分段着色：备注部分金色(#FFC83D)，括号及括号内的名字保持白色；
    其余（📌/在线灯/⭐/未读等）保持白色。背景与选中/悬浮样式仍走 QSS。"""
    GOLD = "#FFC83D"
    WHITE = "#FFFFFF"

    @staticmethod
    def build_segments(display, remark, name):
        if not remark:
            return [(display, ConvListDelegate.WHITE)]
        paren = f"({name})" if name else None
        idx = display.find(paren) if paren else -1
        if idx <= 0:
            return [(display, ConvListDelegate.WHITE)]
        return [(display[:idx], ConvListDelegate.GOLD), (display[idx:], ConvListDelegate.WHITE)]

    def paint(self, painter, option, index):
        data = index.data(Qt.UserRole + 1)
        if not isinstance(data, dict):
            super().paint(painter, option, index)
            return
        # 修复文本残影：设置样式表时 QStyleSheetStyle 会无视被清空的 option.text、
        # 直接按 index 的 DisplayRole 绘制文本，与分段文本叠加成"残影"。
        # 因此数据条目的背景与文本完全自绘，不再走原生绘制。
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#3A5A40'))
            painter.drawRoundedRect(option.rect, 6, 6)
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#383838'))
            painter.drawRoundedRect(option.rect, 6, 6)
        display = data.get('display') or ''
        segments = self.build_segments(display, data.get('remark') or '', data.get('name') or '')
        text_rect = option.rect.adjusted(8, 6, -8, -6)
        fm = option.fontMetrics
        elided = fm.elidedText(display, Qt.ElideRight, max(text_rect.width(), 1))
        painter.setFont(option.font)
        if elided != display:
            # 触发省略号：整体单色绘制（有备注用金色，否则白色）
            painter.setPen(QColor(self.GOLD if data.get('remark') else self.WHITE))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        else:
            x = text_rect.left()
            for seg_text, seg_color in segments:
                w = fm.horizontalAdvance(seg_text)
                painter.setPen(QColor(seg_color))
                painter.drawText(x, text_rect.top(), w, text_rect.height(),
                                 Qt.AlignVCenter | Qt.AlignLeft, seg_text)
                x += w
        painter.restore()


# ==================== Minecraft 风格命令补全 ====================
CHAT_COMMAND_SPECS = [
    ('/bind', False, ['绑定码']),
    ('/help', False, []),
    ('/recall', False, ['消息ID']),
    ('/redeemstats', False, []),
    ('/mute', True, ['user', ('server', 'global'), 'duration']),
    ('/unmute', True, ['user', ('server', 'global')]),
    ('/ban', True, ['user', 'duration']),
    ('/unban', True, ['user']),
    ('/kick', True, ['online_user']),
    ('/op', True, ['user']),
    ('/deop', True, ['user']),
    ('/give', True, ['target', ('cake', 'credit', 'ticket', 'jade', 'ruyi'), '数量']),
    ('/set', True, ['set_target', ('cake', 'credit', 'ticket', 'jade', 'ruyi'), '数量']),
    ('/clear', True, ['target', ('cake', 'credit', '*')]),
    ('/revoke', True, ['target', '物品:数量...']),
    ('/resetname', True, ['user']),
    ('/maxplayers', True, ['人数']),
    ('/access', True, [('whitelist', 'blacklist', 'none'), ('on', 'off')]),
    ('/whitelist', True, [('on', 'off', 'add', 'remove', 'list')]),
    ('/blacklist', True, [('on', 'off', 'add', 'remove', 'list')]),
    ('/function', True, [('AI', 'Chat'), ('on', 'off')]),
]

ARG_PLACEHOLDER_HINTS = {
    'duration': '时长（1D/12H/30m/2026-12-31，留空=永久）',
    '数量': '数量',
    '人数': '人数',
    '消息ID': '消息ID',
    '物品:数量...': '物品:数量&物品:数量',
    '新名称': '新名称',
    '兑换码': '兑换码',
    '国家代码': '国家代码',
    '时区名': '时区名',
    'IP': 'IP地址',
}


class SlashCompleter(QObject):
    """Minecraft 风格命令补全：白色已输入部分 + 灰色内联提示 + Tab 补齐 + 参数候选。

    参数候选按命令需要提供（provider 返回 (is_admin, users_fn)）：
      'user'        → 全部玩家名字（在线优先）
      'online_user' → 仅在线玩家名字
      'target'      → @a / @s + 全部玩家名字
      'ban_target'  → ip / area / timezone + 全部玩家名字
      'set_target'  → newcode / @a / @s + 全部玩家名字
      元组/列表      → 固定候选（如 server|global、on|off、物品名）
      其他字符串     → 仅灰色占位提示（不可补齐，如 时长/数量）
    """

    def __init__(self, edit, provider, specs):
        super().__init__(edit)
        self.edit = edit
        self.provider = provider
        self.specs = specs
        self._sel = 0
        self._typed = ''
        self._stage = None
        self._cands = []          # [(补全token, 显示文本)]
        self._static_hint = ''
        self.mention_provider = None   # '@' 提及候选来源（服内聊天用；返回用户名字列表）
        self._just_completed = False   # 刚用 TAB 补齐过（再按 TAB 循环切换）
        self._cycle_cands = []         # 刚补齐位置的候选列表
        self._cycle_token = ''         # 刚补齐的 token
        self._in_cycle = False         # 循环替换中（避免 refresh 重算候选位置）
        self._ime_composing = False   # 输入法组合中（此时不显示提示，避免与输入法候选窗重叠）
        self.allow_at_s = True         # @目标里是否给 '@s'（本人）：服务端控制台没有本人，置 False
        self._ime_seq = 0            # 输入法事件序号（用于延迟恢复提示）
        self._hint_lbl = QLabel(edit)
        self._hint_lbl.setStyleSheet('color: rgba(255,255,255,110); background:transparent;')
        self._hint_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint_lbl.hide()
        self._popup = QListWidget(edit.window())
        self._popup.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._popup.setFocusPolicy(Qt.NoFocus)
        self._popup.setStyleSheet(
            "QListWidget { background:#2D2D2D; color:#DDDDDD; border:1px solid #555;"
            " border-radius:6px; font-size:12px; }"
            "QListWidget::item { padding:3px 8px; }"
            "QListWidget::item:selected { background:#3A5A40; color:white; }")
        self._popup.hide()
        self._popup.itemClicked.connect(self._on_item_clicked)
        edit.installEventFilter(self)
        edit.textEdited.connect(self._on_user_edit)
        edit.cursorPositionChanged.connect(self.refresh)

    def _on_user_edit(self):
        """用户手动输入：清除 TAB 循环状态"""
        self._just_completed = False
        self._cycle_cands = []
        self.refresh()

    # ---------- 解析 ----------
    def _compute(self, text):
        """解析输入，返回 (candidates, typed, stage, static_hint)"""
        if not text.startswith('/'):
            # '@' 提及：给出用户候选（TAB 可选择）；'@' 后一旦出现空格即结束提及
            if not self.mention_provider:
                return [], '', None, ''
            at = text.rfind('@')
            if at < 0:
                return [], '', None, ''
            token = text[at + 1:]
            if ' ' in token:
                return [], '', None, ''
            try:
                names = self.mention_provider() or []
            except Exception:
                names = []
            cands = [n for n in names if n.lower().startswith(token.lower())]
            return [(n, '@' + n) for n in cands][:8], token, 'mention', ''
        ends_space = text.endswith(' ')
        # 折叠末尾多余空格：多敲一个空格不应跳到下一个参数位
        parts = (text[1:].rstrip(' ').split(' ') if ends_space else text[1:].split(' '))
        try:
            is_admin, users_fn = self.provider()
        except Exception:
            is_admin, users_fn = False, (lambda online_only=False: [])
        if len(parts) == 1 and not ends_space:
            token = parts[0]
            out = []
            for cmd, admin_only, args in self.specs:
                if admin_only and not is_admin:
                    continue
                if cmd.startswith('/' + token):
                    out.append((cmd, self._cmd_display(cmd, args)))
            return out, token, 'cmd', ''
        cmd = '/' + parts[0]
        arg_spec = None
        for c, admin_only, args in self.specs:
            if c == cmd and (not admin_only or is_admin):
                arg_spec = args
                break
        if arg_spec is None:
            return [], '', None, ''
        arg_index = len(parts) if ends_space else len(parts) - 1   # '/mute ' → 第1个参数
        typed = '' if ends_space else parts[-1]
        if arg_index < 1 or arg_index - 1 >= len(arg_spec):
            return [], '', None, ''
        arg = arg_spec[arg_index - 1]
        if arg == 'user':
            cands = users_fn(False)
        elif arg == 'online_user':
            cands = users_fn(True)
        elif arg == 'target':
            cands = (['@a', '@s'] if getattr(self, 'allow_at_s', True) else ['@a']) + users_fn(False)
        elif arg == 'ban_target':
            cands = ['ip', 'area', 'timezone'] + users_fn(False)
        elif arg == 'set_target':
            cands = (['newcode', '@a', '@s'] if getattr(self, 'allow_at_s', True)
                     else ['newcode', '@a']) + users_fn(False)
        elif isinstance(arg, (list, tuple)):
            cands = list(arg)
        else:
            return [], typed, 'arg', ARG_PLACEHOLDER_HINTS.get(arg, str(arg))
        cands = [c for c in cands if c.lower().startswith(typed.lower())]
        return [(c, c) for c in cands][:8], typed, 'arg', ''

    def _cmd_display(self, cmd, args):
        ph = []
        for a in args:
            if isinstance(a, (list, tuple)):
                ph.append('[' + '|'.join(a) + ']')
            elif a == 'user':
                ph.append('<玩家>')
            elif a == 'online_user':
                ph.append('<在线玩家>')
            elif a == 'target':
                ph.append('<@a/@s/玩家>' if getattr(self, 'allow_at_s', True) else '<@a/玩家>')
            elif a == 'ban_target':
                ph.append('<玩家|ip|area|timezone>')
            elif a == 'duration':
                ph.append('[时长]')
            elif a == 'items':
                ph.append('<物品:数量...>')
            else:
                ph.append('<%s>' % ARG_PLACEHOLDER_HINTS.get(a, a))
        return cmd + (' ' + ' '.join(ph) if ph else '')

    def _cmd_args_hint(self, cmd):
        for c, _admin_only, args in self.specs:
            if c == cmd:
                disp = self._cmd_display(cmd, args)
                return disp[len(cmd):]
        return ''

    def _cmd_first_arg_preview(self, cmd):
        """第一个参数的第一个具体候选（用于灰色内联预览，如玩家名）；不可枚举则给占位提示"""
        for c, _admin_only, args in self.specs:
            if c == cmd:
                if not args:
                    return ''
                a = args[0]
                try:
                    _is_admin, users_fn = self.provider()
                except Exception:
                    return ''
                if a == 'user':
                    cands = users_fn(False)
                elif a == 'online_user':
                    cands = users_fn(True)
                elif a == 'target':
                    cands = (['@a', '@s'] if getattr(self, 'allow_at_s', True) else ['@a']) + users_fn(False)
                elif a == 'ban_target':
                    cands = ['ip', 'area', 'timezone'] + users_fn(False)
                elif a == 'set_target':
                    cands = (['newcode', '@a', '@s'] if getattr(self, 'allow_at_s', True)
                             else ['newcode', '@a']) + users_fn(False)
                elif isinstance(a, (list, tuple)):
                    cands = list(a)
                else:
                    return ARG_PLACEHOLDER_HINTS.get(a, str(a))
                return cands[0] if cands else ''
        return ''

    # ---------- 刷新 UI ----------
    def refresh(self):
        if getattr(self, '_in_cycle', False):
            return
        if getattr(self, '_ime_composing', False):
            # 输入法还在组合：不要弹出提示/候选（等上屏后再显示）
            self._hide_all()
            return
        e = self.edit
        if not e.text().startswith('/'):
            # 非命令文本：仅当处于 '@' 提及输入中才继续提供候选
            if not (self.mention_provider and '@' in e.text()):
                self._hide_all()
                return
        cands, typed, stage, static_hint = self._compute(e.text())
        self._cands, self._typed, self._stage, self._static_hint = cands, typed, stage, static_hint
        if self._sel >= max(len(cands), 1):
            self._sel = 0
        self._update_hint()
        self._update_popup()

    def _hide_all(self):
        self._cands, self._stage, self._static_hint = [], None, ''
        self._hint_lbl.hide()
        self._popup.hide()

    def _update_hint(self):
        e = self.edit
        hint = ''
        if self._cands:
            if self._sel >= len(self._cands):
                self._sel = 0
            token = self._cands[self._sel][0]
            if self._stage == 'cmd':
                # 命令词补全不含斜杠：/mu → te；并预览第一个参数的第一个候选（如玩家名）
                word = token[1:]
                if word.lower().startswith(self._typed.lower()):
                    hint = word[len(self._typed):]
                preview = self._cmd_first_arg_preview(token)
                if preview:
                    hint = (hint + ' ' + preview).strip() + ' ...'
                hint = hint.strip()
            elif token.lower().startswith(self._typed.lower()):
                hint = token[len(self._typed):]
        elif self._static_hint:
            hint = ' ' + self._static_hint
        if not hint or e.cursorPosition() != len(e.text()):
            self._hint_lbl.hide()
            return
        self._hint_lbl.setText(hint)
        self._hint_lbl.setFont(e.font())
        self._hint_lbl.adjustSize()
        fm = e.fontMetrics()
        try:
            opt = QStyleOptionFrame()
            e.initStyleOption(opt)
            cr = e.style().subElementRect(QStyle.SE_LineEditContents, opt, e)
            x = cr.x() + fm.horizontalAdvance(e.text()) + 2
        except Exception:
            x = fm.horizontalAdvance(e.text()) + 8
        if x < 0 or x + self._hint_lbl.width() > e.width() - 4:
            self._hint_lbl.hide()
            return
        self._hint_lbl.move(x, (e.height() - self._hint_lbl.height()) // 2)
        self._hint_lbl.show()

    def _update_popup(self):
        e = self.edit
        if getattr(self, '_ime_composing', False):
            self._popup.hide()
            return
        if not self._cands:
            self._popup.hide()
            return
        self._popup.clear()
        for token, disp in self._cands:
            it = QListWidgetItem(disp)
            it.setData(Qt.UserRole, token)
            self._popup.addItem(it)
        if self._sel >= len(self._cands):
            self._sel = 0
        self._popup.setCurrentRow(self._sel)
        self._popup.move(e.mapToGlobal(QPoint(0, e.height() + 2)))
        self._popup.setFixedWidth(max(e.width(), 300))
        self._popup.setFixedHeight(min(len(self._cands) * 24 + 10, 190))
        if not self._popup.isVisible():
            self._popup.show()
        self._popup.raise_()

    def _safe_hide_popup(self):
        try:
            self._popup.hide()
        except Exception:
            pass

    def _on_item_clicked(self, item):
        token = item.data(Qt.UserRole)
        self._apply(token)
        self._popup.hide()

    # ---------- 补齐 ----------
    def _complete(self):
        e = self.edit
        text = e.text()
        # 连续 TAB：在刚补齐的参数位上循环切换候选（server → global → server / 用户间切换）
        tail_space = self._cycle_token + ' '
        if (self._just_completed and self._cycle_cands
                and (text.endswith(tail_space) or text.endswith(self._cycle_token))):
            matches = [c for c, _ in self._cycle_cands]
            if self._cycle_token in matches and len(matches) > 1:
                idx = matches.index(self._cycle_token)
                self._sel = (idx + 1) % len(self._cycle_cands)
                new_token = self._cycle_cands[self._sel][0]
                strip_len = len(tail_space) if text.endswith(tail_space) else len(self._cycle_token)
                head = text[:len(text) - strip_len]
                keep_space = ' ' if text.endswith(tail_space) else ''
                self._in_cycle = True
                try:
                    e.setText(head + new_token + keep_space)
                    e.setCursorPosition(len(e.text()))
                finally:
                    self._in_cycle = False
                self._cycle_token = new_token
                self._just_completed = True
                # 重算到下一参数位：灰色提示/弹窗显示下一参数（如 server|global），
                # 避免刚补齐的名字又被灰色重复显示一遍（/mute 开拓者 开拓者）
                self.refresh()
                return True
        if not self._cands:
            return False
        if self._sel >= len(self._cands):
            self._sel = 0
        self._apply(self._cands[self._sel][0])
        self._popup.hide()
        return True

    def _apply(self, token):
        e = self.edit
        text = e.text()
        # '@' 提及：把 @未完成词 替换为 @用户名 + 空格
        if self._stage == 'mention':
            at = text.rfind('@')
            if at >= 0:
                e.setText(text[:at] + '@' + token + ' ')
                e.setCursorPosition(len(e.text()))
                self.refresh()
                return
        # 全程挂起 refresh（setText/setCursorPosition 都会触发信号重算候选）：
        # 先保住"当前参数位"的候选供 TAB 循环，再让 refresh 重算到下一参数位。
        # 注意：补齐后**不自动补空格**——空格由用户输入，作为"进入下一阶段提示"的信号
        self._in_cycle = True
        try:
            if self._stage == 'cmd':
                e.setText(token)
            elif text.endswith(' '):
                e.setText(text + token)
            else:
                e.setText(text[:len(text) - len(self._typed)] + token)
            e.setCursorPosition(len(e.text()))
        finally:
            self._in_cycle = False
        self._cycle_cands = list(self._cands)
        self._cycle_token = token
        self._just_completed = True
        self.refresh()

    # ---------- 事件 ----------
    def eventFilter(self, obj, ev):
        if obj is not self.edit:
            return False
        if ev.type() == QEvent.InputMethod:
            # 输入法：任何输入法事件都先让位 —— 部分输入法（"不显示拼音串"）在组合期间
            # preeditString() 为空，只看 preedit 会漏判，导致提示/候选窗与输入法候选窗重叠。
            self._ime_seq = getattr(self, '_ime_seq', 0) + 1
            _seq = self._ime_seq
            try:
                _pre = ev.preeditString()
            except Exception:
                _pre = ''
            self._hide_all()
            if _pre:
                self._ime_composing = True      # 组合中：保持隐藏
            else:
                self._ime_composing = False
                # 组合刚结束：稍等再恢复（避免输入法候选窗尚未关闭提示就弹回）
                def _resume(_seq=_seq):
                    try:
                        if getattr(self, '_ime_seq', 0) != _seq:
                            return          # 期间又来了输入法事件：继续让位
                        self.refresh()
                    except RuntimeError:
                        pass                # 控件已销毁
                QTimer.singleShot(180, _resume)
            return False
        if ev.type() == QEvent.FocusOut:
            # 延迟隐藏：让弹窗条目的点击先生效（带异常保护：对象可能已销毁）
            QTimer.singleShot(150, self._safe_hide_popup)
            return False
        if ev.type() != QEvent.KeyPress:
            return False
        key = ev.key()
        if key == Qt.Key_Tab:
            _t = self.edit.text()
            if _t.startswith('/') or (self.mention_provider and '@' in _t):
                # 有候选则补齐/切换；无候选（如时长占位提示）则留空——都不让焦点跳出输入框
                self._complete()
                return True
            return False
        if key in (Qt.Key_Down, Qt.Key_Up) and self._popup.isVisible() and self._cands:
            n = len(self._cands)
            self._sel = (self._sel + (1 if key == Qt.Key_Down else -1)) % n
            self._popup.setCurrentRow(self._sel)
            self._update_hint()
            return True
        if key == Qt.Key_Escape and self._popup.isVisible():
            self._popup.hide()
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._popup.hide()
        return False


class ChatWindow(QDialog):
    def __init__(self, firefly, username, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "💬")
        self.firefly = firefly
        self.username = username
        self.setWindowTitle("服务器聊天")
        self.setFixedSize(*fit_4_3(860, 550))
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 12px; }
            QScrollArea { background: #2D2D2D; border: none; }
            QLineEdit { background: #2D2D2D; border: 1px solid #555555; border-radius: 6px; padding: 8px 12px; color: white; font-size: 14px; font-family: "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif; }
            QLineEdit:focus { border-color: #1AAD19; }
            QPushButton { background: #4CAF50; color: white; border: none; border-radius: 6px; padding: 8px 20px; font-size: 14px; font-weight: bold; font-family: "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif; }
            QPushButton:hover { background: #45A049; }
            QMenu { background-color: #2D2D2D; border: 1px solid #555; color: white; }
            QMenu::item { padding: 5px 20px; }
            QMenu::item:selected { background-color: #4CAF50; }
            /* 修复：消息框是 QDialog 子类，会继承上面的深色背景，但文字默认黑色 →
               看起来是"空白弹窗"。这里显式给文字上色，保证弹窗内容可读。 */
            QMessageBox { background-color: #1E1E1E; }
            QMessageBox QLabel { color: #E0E0E0; font-size: 14px; }
            QMessageBox QPushButton { min-width: 64px; }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 5)
        main_layout.setSpacing(8)

        # ================== 主体：左侧会话列表 + 右侧消息区 ==================
        body_layout = QHBoxLayout()
        body_layout.setSpacing(8)

        # ---------- 左侧：会话列表 ----------
        self.conv_panel = QWidget()
        self.conv_panel.setMinimumWidth(150)  # 宽度由分栏控制（可拖拽/双击自适应）
        self.conv_panel.setStyleSheet("background: #252525; border-radius: 8px;")
        conv_layout = QVBoxLayout(self.conv_panel)
        conv_layout.setContentsMargins(6, 6, 6, 6)
        conv_layout.setSpacing(4)
        # 顶部一行：左侧"会话"标题（实时未读数），右侧"+"发起聊天小按钮（深色 UI 配色）
        conv_header = QHBoxLayout()
        conv_header.setSpacing(6)
        self.conv_title_lbl = QLabel("会话")
        self.conv_title_lbl.setStyleSheet(
            "color:#AAAAAA; font-size:12px; font-weight:bold; background:transparent;")
        conv_header.addWidget(self.conv_title_lbl)
        conv_header.addStretch(1)
        self.new_chat_btn = QPushButton("+")
        self.new_chat_btn.setFixedSize(24, 22)
        self.new_chat_btn.setStyleSheet(
            'QPushButton { background: #3C3C3C; color: #E8E8E8; border: 1px solid #5A5A5A;'
            ' border-radius: 6px; font-size: 15px; font-weight: bold; padding: 0px; }'
            ' QPushButton:hover { background: #4C4C4C; border-color: #7C7C7C; color: #FFFFFF; }'
            ' QPushButton:pressed { background: #2F2F2F; }')
        self.new_chat_btn.setToolTip("发起聊天：选择玩家开始私信")
        self.new_chat_btn.clicked.connect(self._start_new_chat)
        self.new_chat_btn.setAutoDefault(False)  # 修复：QDialog 中回车会误触发该按钮
        self.new_chat_btn.setDefault(False)
        conv_header.addWidget(self.new_chat_btn)
        conv_layout.addLayout(conv_header)
        self.conv_list = QListWidget()
        self.conv_list.setStyleSheet("""
            QListWidget { background: transparent; border: none; color: #DDDDDD; font-size: 13px;
                          font-family: "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif; outline: none; }
            QListWidget::item { padding: 6px 8px; border-radius: 6px; }
            QListWidget::item:selected { background: #3A5A40; color: white; }
            QListWidget::item:hover { background: #383838; }
        """)
        # 修复：会话列表只竖向滚动，不再出现横向滚动条（长条目省略号截断）
        self.conv_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.conv_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.conv_list.setTextElideMode(Qt.ElideRight)
        self.conv_list.setItemDelegate(ConvListDelegate())
        self.conv_list.setMouseTracking(True)  # 委托自绘悬浮背景需要 hover 追踪
        self.conv_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conv_list.customContextMenuRequested.connect(self._on_conv_right_click)
        # 修复：改用 itemClicked 切换会话。此前 currentItemChanged 在 Windows 下
        # 右键也会把条目设为当前项，导致右键弹菜单的同时切走了会话页面
        self.conv_list.itemClicked.connect(self._on_conv_clicked)
        conv_layout.addWidget(self.conv_list, 1)
        # ---------- 会话面板与消息区之间可拖拽调宽 ----------
        self.body_splitter = QSplitter(Qt.Horizontal)
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.setHandleWidth(6)
        self.body_splitter.setStyleSheet(
            "QSplitter::handle { background: #3A3A3A; }"
            "QSplitter::handle:hover { background: #4A7A50; }")
        self.body_splitter.addWidget(self.conv_panel)

        # ---------- 右侧：消息区 ----------
        right_layout = QVBoxLayout()
        right_layout.setSpacing(8)

        # 禁令横幅：显示在消息气泡区最上方（自己/对方被禁止私信时整行红字）
        self.ban_banner = QLabel()
        self.ban_banner.setWordWrap(True)
        self.ban_banner.setTextFormat(Qt.PlainText)
        self.ban_banner.setStyleSheet(
            "background:#3A1E1E; color:#FF6B6B; border:1px solid #8B2E2E;"
            " border-radius:6px; padding:6px 10px; font-size:13px; font-weight:bold;")
        self.ban_banner.setVisible(False)
        right_layout.addWidget(self.ban_banner)

        # 会话页面栈：每个会话是独立页面且自带滚动区域，切换不销毁任何消息控件
        self.conv_stack = QStackedWidget()
        self.conv_stack.setStyleSheet("background: #2D2D2D;")
        self._server_page, self._server_scroll, self.msg_container, self.msg_layout = self._make_convo_page()
        self.conv_stack.addWidget(self._server_page)
        right_layout.addWidget(self.conv_stack, 1)

        input_layout = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("输入消息，支持 @用户名 或 @所有人")
        self.input_edit.returnPressed.connect(self.send_message)
        # Minecraft 风格命令补全（灰色提示 + Tab 补齐 + 用户名候选）
        self._slash = SlashCompleter(self.input_edit, self._completer_provider, CHAT_COMMAND_SPECS)
        self._slash.mention_provider = self._mention_candidates  # 服内聊天输入 @ 的候选
        self.send_btn = QPushButton("发送")
        self.send_btn.setAutoDefault(False)  # 回车只走 returnPressed 发送一次
        self.send_btn.clicked.connect(self.send_message)
        input_layout.addWidget(self.input_edit)
        input_layout.addWidget(self.send_btn)
        right_layout.addLayout(input_layout)

        right_widget = QWidget()
        right_widget.setLayout(right_layout)
        self.body_splitter.addWidget(right_widget)
        self.body_splitter.setStretchFactor(0, 0)
        self.body_splitter.setStretchFactor(1, 1)
        body_layout.addWidget(self.body_splitter, 1)
        main_layout.addLayout(body_layout, 1)

        # 面板宽度：恢复上次记录；拖动后保存；双击分隔条按内容自适应
        self._conv_settings = QSettings("GUCNMC", "流萤桌宠")
        try:
            saved_w = int(self._conv_settings.value("chat_conv_panel_width", 190))
        except Exception:
            saved_w = 190
        saved_w = max(150, min(saved_w, 600))
        self.body_splitter.setSizes([saved_w, max(400, 1050 - saved_w)])
        self.body_splitter.splitterMoved.connect(self._save_conv_panel_width)
        try:
            self.body_splitter.handle(1).installEventFilter(self)
        except Exception:
            pass

        # ================== 禁言状态（先于会话模型初始化，_refresh_ui 依赖） ==================
        self._muted = False
        self._muted_perm = False
        self._sending = False
        self._muted_until = None
        self._muted_dt = None
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self._update_countdown)
        self.countdown_timer.setInterval(1000)

        self.common_mute = firefly.common_mute
        self.is_admin = firefly.is_admin

        # ================== 会话模型 ==================
        # key: 'server' 或 'pm:<server_uid>'
        self.convos = {
            'server': {'type': 'server', 'key': 'server', 'uid': None, 'name': '服内聊天',
                       'remark': '', 'page': self._server_page, 'scroll': self._server_scroll,
                       'widget': self.msg_container, 'layout': self.msg_layout,
                       'waiting': False, 'unread': 0, 'history_loaded': True}
        }
        self.current_conv = 'server'
        # 置顶/不再显示：来自服务端保存的状态（服内聊天默认置顶且不可取消）
        self.pinned_keys = set(getattr(firefly, 'pm_pinned', []) or []) | {'server'}
        self.hidden_keys = set(getattr(firefly, 'pm_hidden', []) or [])
        self._new_chat_fallback_pending = False  # "发起聊天"全量名单请求是否还在等待兜底
        self._pending_pms = {}                   # 待回执的私信：{to_uid: [entry, ...]}
        # 从桌宠状态恢复好友与等待回复的陌生人
        for f in (getattr(self.firefly, 'friends', []) or []):
            self._ensure_friend_convo(f.get('uid'), f.get('name'), f.get('remark', ''))
        for ps in (getattr(self.firefly, 'pending_strangers', []) or []):
            convo = self._ensure_stranger_convo(ps.get('uid'), ps.get('name'), ps.get('remark'))
            if convo is not None:
                convo['waiting'] = True
                self._append_system(convo, '⏳ 上次发送的消息仍在等待对方回复')
        self._rebuild_conv_list()
        self._select_conv('server')

        # 加载历史消息
        self.load_history()
        self.update_mute_status(self.firefly.muted_until,
                                bool(getattr(self.firefly, 'muted_perm', False)))
        self.update_chat_status(self.firefly.chat_enabled)
        self._refresh_ui()

    # ================== 会话管理 ==================
    def _make_convo_page(self):
        """创建一个会话页面：独立 QScrollArea + 消息容器，返回 (page, scroll, container, layout)"""
        page = QWidget()
        page.setStyleSheet("background: #2D2D2D;")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget()
        container.setStyleSheet("background: #2D2D2D;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addStretch()
        scroll.setWidget(container)
        page_layout.addWidget(scroll)
        # 每个会话滚动区独立绑定右键菜单（会话模型构造完成后 convo 才存在，此处先绑定通用回调）
        scroll.viewport().customContextMenuRequested.connect(self._on_viewport_right_click)
        # 布局更新导致滚动范围变化时，兑现"滚到底部"的请求
        scroll.verticalScrollBar().rangeChanged.connect(
            lambda mn, mx, s=scroll: self._on_scroll_range_changed(s, mn, mx))
        return page, scroll, container, layout

    def _ensure_friend_convo(self, uid, name, remark=''):
        if not uid:
            return None
        key = 'pm:' + uid
        convo = self.convos.get(key)
        if convo is None:
            page, scroll, widget, layout = self._make_convo_page()
            self.conv_stack.addWidget(page)
            convo = {'type': 'friend', 'key': key, 'uid': uid, 'name': name or uid,
                     'remark': remark or '', 'page': page, 'scroll': scroll,
                     'widget': widget, 'layout': layout,
                     'waiting': False, 'unread': 0, 'history_loaded': False}
            self.convos[key] = convo
        else:
            if name:
                convo['name'] = name
            if remark:
                # 仅在服务端确实下发了备注时更新，避免覆盖已保留的备注
                convo['remark'] = remark
            convo['type'] = 'friend'
            convo['waiting'] = False
        return convo

    def _ensure_stranger_convo(self, uid, name, remark=None):
        if not uid:
            return None
        key = 'pm:' + uid
        convo = self.convos.get(key)
        if convo is None:
            page, scroll, widget, layout = self._make_convo_page()
            self.conv_stack.addWidget(page)
            convo = {'type': 'stranger', 'key': key, 'uid': uid, 'name': name or uid,
                     'remark': remark or '', 'page': page, 'scroll': scroll,
                     'widget': widget, 'layout': layout,
                     'waiting': False, 'unread': 0, 'history_loaded': False}
            self.convos[key] = convo
        else:
            if name:
                convo['name'] = name
            # 陌生人 / 被拉黑 / 拉黑时保留既有备注（不因关系变化清空）
            if remark:
                convo['remark'] = remark
        return convo

    def _current_convo(self):
        return self.convos.get(self.current_conv) or self.convos['server']

    def _conv_display(self, convo):
        if convo['type'] == 'server':
            return '🌐 服内聊天'
        pin = '📌 ' if convo['key'] in self.pinned_keys else ''
        base = f"{convo['remark']}({convo['name']})" if convo.get('remark') else convo['name']
        special = convo['uid'] in (getattr(self.firefly, 'pm_special_focus', []) or [])
        if convo['type'] == 'friend':
            txt = pin + ('🟢 ' if self._friend_online(convo['uid']) else '⚪ ') + ('⭐ ' if special else '') + base
        else:
            txt = pin + '👤 ' + ('⭐ ' if special else '') + base
            if convo.get('waiting'):
                txt += '（等待回复）'
        if convo['uid'] in (getattr(self.firefly, 'blocked', []) or []):
            txt = '🚫 ' + txt
        if convo.get('unread', 0) > 0:
            txt += f"  🔴{convo['unread']}"
        return txt

    def _friend_online(self, uid):
        for p in (getattr(self.firefly, 'latest_players', []) or []):
            if p.get('uid') == uid:
                return True
        return False

    def _rebuild_conv_list(self):
        self.conv_list.blockSignals(True)
        self.conv_list.clear()

        def add_header(text):
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemIsEnabled)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setForeground(QColor('#8FBF8F'))
            self.conv_list.addItem(item)

        def add_entry(convo):
            display = self._conv_display(convo)
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, convo['key'])
            # 分段着色数据：备注金色(#FFC83D)，括号及括号内名字白色（见 ConvListDelegate）
            item.setData(Qt.UserRole + 1, {'display': display,
                                           'remark': convo.get('remark') or '',
                                           'name': convo.get('name') or ''})
            self.conv_list.addItem(item)

        add_header('━━━ 服内聊天 ━━━')
        add_entry(self.convos['server'])

        # 特别关注栏目：位于服内聊天与好友聊天之间，数量为 0 时隐藏；
        # 已特别关注的会话不再重复出现在好友/陌生人栏目
        focus_set = set(getattr(self.firefly, 'pm_special_focus', []) or [])
        focused = [c for c in self.convos.values()
                   if c['type'] != 'server' and c.get('uid') in focus_set
                   and c['key'] not in self.hidden_keys]
        focused.sort(key=lambda c: (0 if c['key'] in self.pinned_keys else 1, c['name']))
        if focused:
            add_header(f'━━━ ⭐ 特别关注 ({len(focused)}) ━━━')
            for c in focused:
                add_entry(c)

        friends = [c for c in self.convos.values()
                   if c['type'] == 'friend' and c.get('uid') not in focus_set]
        friends.sort(key=lambda c: (0 if c['key'] in self.pinned_keys else 1,
                                    0 if self._friend_online(c['uid']) else 1,
                                    c.get('remark') or c['name']))
        visible_friends = [c for c in friends if c['key'] not in self.hidden_keys]
        # 修复：板块数量为 0 时不显示标题
        if visible_friends:
            add_header(f'━━━ 好友聊天 ({len(visible_friends)}/100) ━━━')
            for c in visible_friends:
                add_entry(c)

        strangers = [c for c in self.convos.values()
                     if c['type'] == 'stranger' and c.get('uid') not in focus_set]
        strangers.sort(key=lambda c: (0 if c['key'] in self.pinned_keys else 1, c['name']))
        visible_strangers = [c for c in strangers if c['key'] not in self.hidden_keys]
        if visible_strangers:
            add_header(f'━━━ 陌生人 ({len(visible_strangers)}) ━━━')
            for c in visible_strangers:
                add_entry(c)

        # 顶部标题实时显示总未读数（无未读时仅显示"会话"）
        total_unread = sum(c.get('unread', 0) for c in self.convos.values())
        if hasattr(self, 'conv_title_lbl'):
            self.conv_title_lbl.setText(f'会话({total_unread}条未读)' if total_unread > 0 else '会话')

        # 恢复当前选中
        row = self._row_of(self.current_conv)
        if row >= 0:
            self.conv_list.setCurrentRow(row)
        self.conv_list.blockSignals(False)

    def _row_of(self, key):
        for i in range(self.conv_list.count()):
            it = self.conv_list.item(i)
            if it.data(Qt.UserRole) == key:
                return i
        return -1

    def _on_conv_clicked(self, item):
        """左键点击会话条目才切换页面；右键仅弹菜单不切换"""
        if item is None:
            return
        key = item.data(Qt.UserRole)
        if key and key != self.current_conv:
            self._switch_to(key)

    def _select_conv(self, key):
        row = self._row_of(key)
        if row < 0:
            return
        self.conv_list.blockSignals(True)
        self.conv_list.setCurrentRow(row)
        self.conv_list.blockSignals(False)
        self._switch_to(key)

    def _switch_to(self, key):
        convo = self.convos.get(key)
        if convo is None:
            return
        old = self.convos.get(self.current_conv)
        if old is not None:
            old['unread'] = 0
        self.current_conv = key
        self.conv_stack.setCurrentWidget(convo['page'])
        convo['unread'] = 0
        self._maybe_load_history(convo)
        self._request_scroll_bottom(key)
        self._rebuild_conv_list()
        self.update_ban_banner()
        self._refresh_ui()

    def open_pm_with(self, uid, name=None):
        """右键名字→私信：加入对应板块（好友/陌生人）"""
        if not uid or uid == (self.firefly.mp_client.server_uid or self.firefly.server_uid):
            return
        # 最底层兜底：已注销账号一律不可发起聊天（即使名单/缓存过期也拦住）
        if self._is_destroyed_user(uid, name):
            QMessageBox.information(self, "提示", "该账号已注销，无法发起聊天")
            return
        friend_info = None
        for f in (getattr(self.firefly, 'friends', []) or []):
            if f.get('uid') == uid:
                friend_info = f
                break
        if friend_info is not None:
            convo = self._ensure_friend_convo(uid, name or friend_info.get('name'), friend_info.get('remark', ''))
        else:
            remark = ''
            for p in (getattr(self.firefly, 'all_players', []) or []):
                if p.get('uid') == uid:
                    remark = p.get('remark') or ''
                    break
            convo = self._ensure_stranger_convo(uid, name, remark)
        if convo is None:
            return
        # 已注销的好友：进入会话即提示私信已关闭
        if (getattr(self.firefly, 'destroyed_users', {}) or {}).get(uid):
            convo['destroyed'] = True
            if not convo.get('destroyed_marked'):
                convo['destroyed_marked'] = True
                self._append_system(convo, '🔒 账号已注销，私信已关闭')
        self.hidden_keys.discard(convo['key'])  # 主动发起聊天恢复"不再显示"的入口
        self._push_prefs()
        self._rebuild_conv_list()
        self._select_conv(convo['key'])

    def refresh_online_status(self):
        self._rebuild_conv_list()

    def _start_new_chat(self):
        """发起聊天：列出所有加入过服务器的玩家（在线+离线），打开会话并加载历史记录。

        每次都向服务端拉取**最新**名单（此前直接用缓存，导致已注销/已退服的玩家仍会出现），
        2.5 秒未回包再用本地已知玩家兜底，避免按钮"点击无反应"。"""
        self._new_chat_fallback_pending = True
        self.firefly.request_all_players(self._show_new_chat_dialog)
        QTimer.singleShot(2500, self._new_chat_fallback)

    def _new_chat_fallback(self):
        """全量名单请求超时：用本地已知玩家（好友/等待回复/在线）兜底打开选择框"""
        if not self._new_chat_fallback_pending:
            return
        self._new_chat_fallback_pending = False
        if getattr(self.firefly, 'all_players', None):
            return  # 名单已回包，走正常路径
        self.firefly.cancel_all_players_request()
        players = self._local_known_players()
        if not players:
            QMessageBox.information(self, "发起聊天",
                                    "未获取到玩家名单（服务器无响应），且本地没有其他玩家记录")
            return
        self._show_new_chat_dialog(players, incomplete=True)

    def _local_known_players(self):
        """合并本地已知的玩家（名单缓存/好友/等待回复/在线名单），去重并排除自己"""
        my_uid = self.firefly.mp_client.server_uid or self.firefly.server_uid
        merged = {}

        destroyed = getattr(self.firefly, 'destroyed_users', {}) or {}
        destroyed_names = set(destroyed.values())
        destroyed_names |= {n for n in destroyed_names if not n}

        def _is_destroyed(uid, name):
            if uid and uid in destroyed:
                return True
            if name and name in destroyed_names:
                return True
            if name and str(name).startswith('账号已注销'):
                return True
            if is_placeholder_player_name(name):
                return True
            return False

        def add(uid, name, pid, online):
            if not uid or uid == my_uid or uid in merged:
                return
            if _is_destroyed(uid, name):   # 已注销用户不再出现在"发起聊天"名单中
                return
            merged[uid] = {'uid': uid, 'name': name or uid, 'id': pid or '', 'online': bool(online)}

        for src in (getattr(self.firefly, 'all_players', []) or [],
                    getattr(self.firefly, 'friends', []) or [],
                    getattr(self.firefly, 'pending_strangers', []) or []):
            for p in src:
                add(p.get('uid'), p.get('name'), p.get('id'), p.get('online'))
        for p in (getattr(self.firefly, 'latest_players', []) or []):
            add(p.get('uid'), p.get('name'), p.get('id'), True)
        result = list(merged.values())
        result.sort(key=lambda x: (0 if x['online'] else 1, str(x['name'])))
        return result

    def _show_new_chat_dialog(self, players, incomplete=False):
        """玩家名单到达后弹出选择框（GUI 线程回调）"""
        self._new_chat_fallback_pending = False
        picked = self._pick_from_players(players, incomplete=incomplete)
        if not picked:
            return
        uid, name = picked
        self.hidden_keys.discard('pm:' + uid)
        self._push_prefs()
        self.open_pm_with(uid, name)

    def _pick_from_players(self, players, incomplete=False):
        """从全部玩家（含离线）中选择，返回 (uid, name) 或 None"""
        my_uid = self.firefly.mp_client.server_uid or self.firefly.server_uid
        destroyed = getattr(self.firefly, 'destroyed_users', {}) or {}
        destroyed_names = set(destroyed.values())

        def _alive(p):
            if not p.get('uid') or p.get('uid') == my_uid:
                return False
            if p.get('uid') in destroyed:
                return False
            nm = p.get('name') or ''
            if nm in destroyed_names or str(nm).startswith('账号已注销'):
                return False
            if is_placeholder_player_name(nm):
                return False
            return True

        players = [p for p in (players or []) if _alive(p)]
        if not players:
            QMessageBox.information(self, "发起聊天",
                                    "暂无其他玩家记录" + ("（服务器无响应）" if incomplete else ""))
            return None
        dialog = QDialog(self)
        dialog.setWindowTitle("发起聊天")
        dialog.setMinimumWidth(460)
        dialog.resize(460, 420)
        layout = QVBoxLayout(dialog)
        if incomplete:
            tip = QLabel("⚠️ 未能获取完整名单（服务器无响应或版本较旧），以下为本地已知玩家：")
        else:
            tip = QLabel("选择玩家开始聊天（⚪ 离线也可查看历史记录）：")
        tip.setStyleSheet("color:#CCCCCC; background:transparent;")
        tip.setWordWrap(True)          # 提示文字换行显示，不再被截断
        layout.addWidget(tip)
        lst = QListWidget()
        lst.setStyleSheet(self.conv_list.styleSheet())
        # 名字/ID 较长时不再用省略号截断：允许横向滚动 + 悬浮显示完整文字
        try:
            lst.setTextElideMode(Qt.ElideNone)
            lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass
        for p in players:
            dot = '🟢' if p.get('online') else '⚪'
            _line = f"{dot} {p.get('name', '?')}  (ID: {p.get('id') or '?'})"
            _it = QListWidgetItem(_line)
            _it.setToolTip(_line)
            lst.addItem(_it)
        lst.itemDoubleClicked.connect(lambda item: dialog.accept())
        layout.addWidget(lst, 1)
        btn_row = QHBoxLayout()
        ok_btn = QPushButton("开始聊天")
        cancel_btn = QPushButton("取消")
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        def _confirm():
            if lst.currentRow() < 0:
                QMessageBox.warning(dialog, "提示", "请先选择一个玩家")
                return
            dialog.accept()

        ok_btn.clicked.connect(_confirm)
        cancel_btn.clicked.connect(dialog.reject)
        if dialog.exec_() != QDialog.Accepted or lst.currentRow() < 0:
            return None
        p = players[lst.currentRow()]
        return p.get('uid'), p.get('name', p.get('uid'))

    def _on_conv_right_click(self, pos):
        item = self.conv_list.itemAt(pos)
        if item is None:
            return
        key = item.data(Qt.UserRole)
        if not key:
            return
        convo = self.convos.get(key)
        if convo is None:
            return
        menu = DarkRoundedMenu(self)
        # 菜单顺序：常用操作 → 关系设置 → 排序/显示 → 危险操作（分组分隔、表情配色）
        if convo['type'] == 'server':
            # 服内聊天：默认置顶不可取消、不可隐藏，仅提供清空记录
            clear_action = menu.addAction("🧹 清空聊天记录")
            clear_action.triggered.connect(self.clear_history)
            menu.exec_(self.conv_list.mapToGlobal(pos))
            return
        open_action = menu.addAction("💬 打开会话")
        open_action.triggered.connect(lambda: self._select_conv(key))
        # 已注销账号：不提供备注/删除好友/特别关注/免打扰/拉黑等操作
        if convo.get('destroyed') or (getattr(self.firefly, 'destroyed_users', {}) or {}).get(convo.get('uid')):
            menu.addSeparator()
            na = menu.addAction("🚫 账号已注销，私信已关闭")
            na.setEnabled(False)
            menu.addSeparator()
            hide_action = menu.addAction("🙈 不再显示")
            hide_action.triggered.connect(lambda: self._hide_conv(key))
            clear_action = menu.addAction("🧹 清空聊天记录")
            clear_action.triggered.connect(lambda: self._clear_conv_history(convo))
            menu.exec_(self.conv_list.mapToGlobal(pos))
            return
        if convo['type'] == 'friend':
            menu.addSeparator()
            # 备注仅好友可设；陌生人只可拉黑（见下方统一的拉黑项）
            remark_action = menu.addAction("📝 设置备注")
            remark_action.triggered.connect(lambda: self._edit_remark(convo))
            del_action = menu.addAction("💔 删除好友")
            del_action.triggered.connect(lambda: self._delete_friend(convo))
        menu.addSeparator()
        is_special = convo['uid'] in (getattr(self.firefly, 'pm_special_focus', []) or [])
        is_muted = convo['uid'] in (getattr(self.firefly, 'pm_muted', []) or [])
        # 陌生人不可设置特别关注/消息免打扰；仅当此前设置过（后降级为陌生人）时保留取消入口
        if convo['type'] == 'friend' or is_special or is_muted:
            sf_action = menu.addAction("💛 取消特别关注" if is_special else "⭐ 设为特别关注")
            sf_action.triggered.connect(lambda: self._toggle_special_focus(convo))
            # 特别关注始终提醒，因此不再提供"消息免打扰"项
            if not is_special:
                mute_action = menu.addAction("🔕 取消消息免打扰" if is_muted else "🔔 消息免打扰")
                mute_action.triggered.connect(lambda: self._toggle_pm_mute(convo))
        # 置顶仅好友会话可用（陌生人右键不显示置顶按钮）
        if convo['type'] == 'friend':
            menu.addSeparator()
            pin_action = menu.addAction("📌 取消置顶" if key in self.pinned_keys else "📍 置顶")
            pin_action.triggered.connect(lambda: self._toggle_pin(key))
        menu.addSeparator()
        hide_action = menu.addAction("🙈 不再显示")
        hide_action.triggered.connect(lambda: self._hide_conv(key))
        is_blocked = convo['uid'] in (getattr(self.firefly, 'blocked', []) or [])
        block_action = menu.addAction("✅ 取消拉黑" if is_blocked else "⛔ 拉黑")
        block_action.triggered.connect(lambda: self._toggle_block(convo))
        clear_action = menu.addAction("🧹 清空聊天记录")
        clear_action.triggered.connect(lambda: self._clear_conv_history(convo))
        if convo.get('uid'):
            build_admin_mute_menu(menu, self.firefly, convo['uid'], convo.get('name'),
                                  self._target_permission(convo['uid']),
                                  self._target_ban_info(convo['uid']), self)
        menu.exec_(self.conv_list.mapToGlobal(pos))

    def _toggle_pin(self, key):
        """置顶/取消置顶会话入口（服内聊天默认置顶，不可取消）"""
        if key == 'server':
            return
        if key in self.pinned_keys:
            self.pinned_keys.discard(key)
        else:
            self.pinned_keys.add(key)
        self._push_prefs()
        self._rebuild_conv_list()

    def _hide_conv(self, key):
        """不再显示会话入口：好友/陌生人关系保留，收到新消息或重新发起聊天时恢复"""
        if key == 'server':
            return
        self.hidden_keys.add(key)
        self._push_prefs()
        if self.current_conv == key:
            self._select_conv('server')
        self._rebuild_conv_list()

    def _clear_conv_history(self, convo):
        """清空指定会话的本地聊天记录（不影响好友关系）"""
        if convo['type'] == 'server':
            self.clear_history(confirm=False)
            return
        self._clear_message_layout(convo['layout'])
        convo['unread'] = 0
        self._rebuild_conv_list()

    def _clear_message_layout(self, layout):
        """清空消息布局：立即脱离布局并延迟销毁控件"""
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            w = item.widget()
            if w is not None:
                layout.removeItem(item)
                # 必须先隐藏：可见控件直接 setParent(None) 会立刻变成顶层窗口，
                # 以"白底、带 最小化/最大化/关闭 的空窗口"形式闪现（每条消息刷新全部
                # 气泡时会成批出现，用户看到的是"很多空白窗口"）
                w.hide()
                w.setParent(None)
                w.deleteLater()
            elif item.spacerItem():
                layout.removeItem(item)
        layout.addStretch()

    def _edit_remark(self, convo):
        dialog = QInputDialog(self)
        dialog.setWindowTitle("设置备注")
        dialog.setLabelText(f"为好友 {convo['name']} 设置备注（留空清除，最长20字）：")
        dialog.setTextValue(convo.get('remark', ''))
        dialog.setStyleSheet("""
            QInputDialog { background: #2D2D2D; color: white; }
            QLabel { color: white; font-size: 13px; }
            QLineEdit { background: #1E1E1E; color: white;
                        border: 1px solid #5A5A5A; border-radius: 4px; padding: 5px; }
            QPushButton { background: #3A5A40; color: white; border: none;
                          border-radius: 4px; padding: 5px 14px; }
            QPushButton:hover { background: #4A7A50; }
        """)
        ok = dialog.exec_()
        text = dialog.textValue()
        if not ok:
            return
        self._apply_remark(convo, text)

    def _apply_remark(self, convo, text):
        """应用备注：不能和对方当前昵称相同（否则会显示成 名字(名字)）"""
        remark = (text or '').strip()[:20]
        if remark and remark == convo.get('name'):
            QMessageBox.warning(self, "提示", "备注不能和对方的昵称相同")
            return
        try:
            self.firefly.mp_client._send({'type': 'friend_remark',
                                          'data': {'target_uid': convo['uid'], 'remark': remark}})
        except Exception as e:
            QMessageBox.warning(self, "提示", f"发送失败：{e}")

    # ================== 私信事件 ==================
    def on_pm_ack(self, data):
        """自己发出的私信的回执"""
        uid = data.get('to_uid') or ''
        convo = self.convos.get('pm:' + uid)
        if convo is None:
            # 窗口重建等情况下会话缺失：按陌生人会话恢复（随后 friends_data 会补全名字）
            convo = self._ensure_stranger_convo(uid, uid)
            if convo is None:
                return
        if not data.get('ok'):
            reason = data.get('reason', '发送失败')
            ban = data.get('pm_ban')
            if isinstance(ban, dict) and ban.get('banned'):
                text = ban.get('until_text') or ('永久' if ban.get('permanent') else '')
                if text and uid:
                    self.firefly.user_pm_bans[uid] = text
                    self.update_ban_banner()
            entry = self._match_pending_pm(uid, data.get('local_id'))
            if entry is not None and '拉黑' in reason:
                # 拉黑导致的失败：保留气泡并显示红色感叹号（点击可重试）
                self._pm_send_failed(entry, reason)
                self._rebuild_conv_list()
                self._refresh_ui()
                return
            if entry is not None:
                self._discard_pending_bubble(entry)
            if '等待对方回复' in reason:
                convo['waiting'] = True
                self._append_system(convo, '⏳ 已发送过消息，等待对方回复后成为好友')
            else:
                self._append_system(convo, f'⚠️ {reason}')
            self._rebuild_conv_list()
            self._refresh_ui()
            return
        if data.get('friend_added'):
            self._mark_became_friends(convo)
        if uid in (getattr(self.firefly, 'user_pm_bans', {}) or {}):
            # 发送成功说明对方并未被禁私信：立即清除本地横幅缓存
            self.firefly.user_pm_bans.pop(uid, None)
            self.update_ban_banner()
        entry = self._match_pending_pm(uid, data.get('local_id'))
        if entry is not None:
            self._finalize_pending_pm(entry, data.get('msg_id'))
        else:
            self._append_bubble(convo['layout'], self.get_my_name(), data.get('text', ''),
                                self._format_time(data.get('time')), False,
                                data.get('msg_id'), True, conv_key=convo['key'],
                                msg_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        if not data.get('is_friend'):
            convo['waiting'] = True
            self._append_system(convo, '⏳ 消息已送达。对方回复后你们将成为好友')
            self._rebuild_conv_list()
            self._refresh_ui()
        if self.current_conv == convo['key']:
            self._request_scroll_bottom(convo['key'])

    def on_pm_recv(self, data):
        """收到私信"""
        uid = data.get('from_uid') or ''
        name = data.get('from_name') or uid
        if data.get('friend_added'):
            convo = self._ensure_friend_convo(uid, name, '')
            self._mark_became_friends(convo)
        else:
            convo = self._ensure_stranger_convo(uid, name)
        if convo is None:
            return
        self.hidden_keys.discard(convo['key'])  # 收到新消息恢复"不再显示"的入口
        self._push_prefs()
        self._append_bubble(convo['layout'], name, data.get('text', ''),
                            self._format_time(data.get('time')), False,
                            data.get('msg_id'), False, conv_key=convo['key'],
                            msg_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        if self.current_conv != convo['key']:
            convo['unread'] += 1
        self._rebuild_conv_list()
        if self.current_conv == convo['key']:
            self._request_scroll_bottom(convo['key'])
            self._refresh_ui()

    # ---------- 私信乐观发送 / 红色感叹号重试 ----------
    def _send_pm_optimistic(self, convo, text):
        """私信乐观上屏：立即显示气泡；发送失败/网络超时显示红色感叹号，点击重试"""
        container = self._build_bubble_widget(
            self.get_my_name(), text, datetime.now().strftime('%m-%d %H:%M'),
            False, None, True, self.firefly.mp_client.server_uid or self.firefly.server_uid,
            None, convo['key'])
        excl = QLabel("❗")
        excl.setStyleSheet("color:#E74C3C; font-size:16px; font-weight:bold; background:transparent;")
        excl.setVisible(False)
        excl.setToolTip("发送失败，点击重试")
        layout = convo['layout']
        if layout.count() > 0:
            last_item = layout.itemAt(layout.count() - 1)
            if last_item.spacerItem():
                layout.removeItem(last_item)
        layout.addWidget(container)
        container.layout().addWidget(excl)
        container.layout().setAlignment(excl, Qt.AlignRight)
        layout.addStretch()
        self._request_scroll_bottom(convo['key'])
        entry = {'local_id': 'tmp:' + str(uuid.uuid4()), 'convo_key': convo['key'],
                 'to_uid': convo['uid'], 'text': text,
                 'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 'container': container, 'excl': excl}
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._pm_send_timeout(entry))
        timer.start(8000)  # 8 秒无回执视为网络不佳
        entry['timer'] = timer
        self._pending_pms.setdefault(convo['uid'], []).append(entry)
        try:
            self.firefly.mp_client._send({'type': 'pm_send',
                                          'data': {'to_uid': convo['uid'], 'text': text,
                                                   'local_id': entry['local_id']}})
        except Exception as e:
            self._pm_send_failed(entry, f'发送失败：{e}')

    def _pop_pending_pm(self, to_uid):
        """按 FIFO 取出该会话最早的待回执（跳过已重试删除的）"""
        queue = self._pending_pms.get(to_uid)
        while queue:
            entry = queue.pop(0)
            timer = entry.get('timer')
            if timer is not None:
                timer.stop()
            if not entry.get('retried'):
                return entry
        return None

    def _match_pending_pm(self, to_uid, local_id):
        """按服务器回传的 local_id 精确匹配待回执；无 local_id 时按 FIFO 兼容"""
        queue = self._pending_pms.get(to_uid)
        if local_id and queue:
            for idx, entry in enumerate(queue):
                if entry.get('local_id') == local_id:
                    entry = queue.pop(idx)
                    timer = entry.get('timer')
                    if timer is not None:
                        timer.stop()
                    return entry
        return self._pop_pending_pm(to_uid)

    def _finalize_pending_pm(self, entry, msg_id):
        """回执成功：定稿气泡（绑定 msg_id 参与去重，隐藏感叹号）"""
        entry['done'] = True
        entry['container'].setProperty('msg_id', msg_id)
        entry['container'].setProperty('msg_time', entry.get('time'))
        excl = entry.get('excl')
        if excl is not None:
            excl.setVisible(False)

    def _discard_pending_bubble(self, entry):
        """移除乐观气泡（消息被服务器拒绝或重试时）"""
        entry['done'] = True
        queue = self._pending_pms.get(entry['to_uid']) or []
        if entry in queue:
            queue.remove(entry)
        timer = entry.get('timer')
        if timer is not None:
            timer.stop()
        container = entry['container']
        convo = self.convos.get(entry['convo_key'])
        if convo is not None:
            layout = convo['layout']
            for i in range(layout.count()):
                if layout.itemAt(i).widget() is container:
                    layout.removeWidget(container)
                    break
        container.deleteLater()

    def _pm_send_timeout(self, entry):
        """回执超时（网络不佳）：显示红色感叹号"""
        if entry.get('done'):
            return
        self._pm_send_failed(entry, '网络不佳，发送超时')

    def _pm_send_failed(self, entry, reason):
        """标记发送失败：显示红色感叹号，点击重试（保留在队列中，迟到的回执仍可定稿）"""
        if entry.get('done'):
            return
        entry['done'] = True
        timer = entry.get('timer')
        if timer is not None:
            timer.stop()
        excl = entry['excl']
        excl.setToolTip(f"{reason}，点击重试")
        excl.setVisible(True)
        excl.setCursor(Qt.PointingHandCursor)

        def _retry(event):
            self._retry_pending_message(entry)
        excl.mousePressEvent = _retry

    def _retry_pending_message(self, entry):
        """点击红色感叹号：删除失败气泡并重新发送"""
        if entry.get('retried'):
            return
        entry['retried'] = True
        self._discard_pending_bubble(entry)
        convo = self.convos.get(entry['convo_key'])
        if convo is None:
            return
        self._send_pm_optimistic(convo, entry['text'])

    def _toggle_block(self, convo):
        """拉黑/取消拉黑：拉黑后双方都无法向对方发送私信"""
        uid = convo.get('uid')
        if not uid:
            return
        is_blocked = uid in (getattr(self.firefly, 'blocked', []) or [])
        self.firefly.send_block(uid, not is_blocked)

    def _delete_friend(self, convo):
        """删除好友：双方解除关系，之后需重新互发消息才能再次成为好友"""
        uid = convo.get('uid')
        if not uid:
            return
        if QMessageBox.question(self, "删除好友",
                                f"确定要删除好友 {convo.get('name')} 吗？\n"
                                f"删除后需要重新互发一条消息才能再次成为好友。") != QMessageBox.Yes:
            return
        try:
            self.firefly.mp_client._send({'type': 'friend_delete',
                                          'data': {'target_uid': uid}})
        except Exception as e:
            QMessageBox.warning(self, "提示", f"发送失败：{e}")

    def on_friend_delete_result(self, data):
        """删除好友结果：会话降级为陌生人并清除备注"""
        if not data.get('ok'):
            QMessageBox.warning(self, "提示", data.get('message', '删除好友失败'))
            return
        uid = data.get('target_uid')
        convo = self.convos.get('pm:' + (uid or ''))
        if convo is not None:
            convo['type'] = 'stranger'
            # 删除好友后保留备注（备注独立存档，重新成为好友时仍在）
            convo['waiting'] = False
            self._append_system(convo, '💔 已删除好友，需重新互发一条消息才能再次成为好友')
        self._rebuild_conv_list()
        self._refresh_ui()

    def _toggle_special_focus(self, convo):
        """特别关注（最多 3 人）：⭐ 标记且始终提醒；关注时自动解除该会话的免打扰"""
        uid = convo.get('uid')
        if not uid:
            return
        focus = getattr(self.firefly, 'pm_special_focus', None)
        if focus is None:
            focus = self.firefly.pm_special_focus = []
        if uid in focus:
            focus.remove(uid)
        else:
            if len(focus) >= 3:
                QMessageBox.warning(self, "提示", "特别关注最多只能设置 3 人")
                return
            focus.append(uid)
            muted = getattr(self.firefly, 'pm_muted', None)
            if muted and uid in muted:
                muted.remove(uid)  # 特别关注优先提醒，自动解除免打扰
        self._push_prefs()
        self._rebuild_conv_list()
        self._refresh_ui()

    def _toggle_pm_mute(self, convo):
        """消息免打扰：特别关注的用户不可开启"""
        uid = convo.get('uid')
        if not uid:
            return
        focus = getattr(self.firefly, 'pm_special_focus', []) or []
        if uid in focus:
            QMessageBox.warning(self, "提示", "特别关注的用户会始终提醒，无法开启消息免打扰")
            return
        muted = getattr(self.firefly, 'pm_muted', None)
        if muted is None:
            muted = self.firefly.pm_muted = []
        if uid in muted:
            muted.remove(uid)
        else:
            muted.append(uid)
        self._push_prefs()
        self._rebuild_conv_list()

    def on_block_result(self, data):
        """拉黑操作结果"""
        if not data.get('ok'):
            QMessageBox.warning(self, "提示", data.get('message', '拉黑操作失败'))
            return
        uid = data.get('target_uid')
        convo = self.convos.get('pm:' + (uid or ''))
        if convo is not None:
            if data.get('blocked'):
                self._append_system(convo, '🚫 已拉黑对方：双方都无法再向对方发送私信')
            else:
                self._append_system(convo, '✅ 已取消拉黑，可以正常私信了')
        self._rebuild_conv_list()
        self._refresh_ui()

    def _mark_became_friends(self, convo):
        if convo['type'] != 'friend':
            convo['type'] = 'friend'
            convo['waiting'] = False
            self._append_system(convo, '🎉 你们已成为好友，现在可以互相发送私信了')
        else:
            convo['waiting'] = False
        self._rebuild_conv_list()
        self._refresh_ui()

    def _mark_destroyed_convos(self):
        """已注销账号：好友会话提示私信关闭，陌生人会话直接删除"""
        destroyed = getattr(self.firefly, 'destroyed_users', {}) or {}
        for uid, disp in destroyed.items():
            convo = self.convos.get('pm:' + uid)
            if convo is None or convo.get('destroyed_marked'):
                continue
            convo['destroyed_marked'] = True
            convo['destroyed'] = True
            if convo['type'] == 'stranger':
                try:
                    self.conv_stack.removeWidget(convo['page'])
                    convo['page'].deleteLater()
                except Exception:
                    pass
                self.convos.pop(convo['key'], None)
                if self.current_conv == convo['key']:
                    self._select_conv('server')
            else:
                self._append_system(convo, '🔒 账号已注销，私信已关闭')

    def on_friends_data(self):
        """好友列表同步：创建/升级会话，标记等待状态"""
        friends = getattr(self.firefly, 'friends', []) or []
        pending = getattr(self.firefly, 'pending_strangers', []) or []
        known = set()
        for f in friends:
            uid = f.get('uid')
            if not uid:
                continue
            known.add(uid)
            self._ensure_friend_convo(uid, f.get('name'), f.get('remark', ''))
        for ps in pending:
            uid = ps.get('uid')
            if not uid or uid in known:
                continue
            convo = self._ensure_stranger_convo(uid, ps.get('name'), ps.get('remark'))
            if convo is not None:
                convo['waiting'] = True
        self._mark_destroyed_convos()
        self._rebuild_conv_list()
        self._refresh_ui()

    def on_friend_remark_result(self, data):
        if data.get('ok'):
            uid = data.get('target_uid')
            convo = self.convos.get('pm:' + (uid or ''))
            if convo is not None:
                convo['remark'] = data.get('remark', '') or ''
                self._rebuild_conv_list()
        else:
            QMessageBox.warning(self, "提示", data.get('message', '备注保存失败'))

    def _maybe_load_history(self, convo):
        """打开会话时按需拉取历史记录（每个会话仅请求一次）"""
        if convo is None or convo['type'] == 'server':
            return
        if convo.get('history_loaded'):
            return
        convo['history_loaded'] = True  # 先标记，避免重复请求
        uid = convo.get('uid')
        if not uid:
            return
        try:
            self.firefly.mp_client._send({'type': 'get_pm_history', 'data': {'peer_uid': uid}})
        except Exception:
            pass

    def on_pm_history_data(self, data):
        """收到私信历史：按 msg_id 去重后插入会话顶部"""
        uid = data.get('peer_uid') or ''
        if not uid:
            return
        name = data.get('peer_name')
        convo = self.convos.get('pm:' + uid)
        if convo is None:
            if not (data.get('messages') or []):
                return
            friend_info = next((f for f in (getattr(self.firefly, 'friends', []) or [])
                                if f.get('uid') == uid), None)
            if friend_info is not None:
                convo = self._ensure_friend_convo(uid, name or friend_info.get('name'),
                                                  friend_info.get('remark', ''))
            else:
                convo = self._ensure_stranger_convo(uid, name or uid)
            if convo is None:
                return
        if name:
            convo['name'] = name
        if data.get('peer_remark'):
            convo['remark'] = data.get('peer_remark')
        ban = data.get('peer_pm_ban')
        if isinstance(ban, dict) and ban.get('banned'):
            text = ban.get('until_text') or ('永久' if ban.get('permanent') else '')
            if text:
                self.firefly.user_pm_bans[convo['uid']] = text
        sm = data.get('peer_mute_server')
        if isinstance(sm, dict):
            self.firefly.user_server_mutes[convo['uid']] = sm
        self._render_history_bubbles(convo, data.get('messages') or [])
        self._rebuild_conv_list()
        self.update_ban_banner()

    def _render_history_bubbles(self, convo, messages):
        """把历史消息插入消息区顶部（时间正序，按 msg_id 去重）"""
        if not messages:
            return
        layout = convo['layout']
        my_uid = self.firefly.mp_client.server_uid or self.firefly.server_uid
        index = 0
        for m in messages:
            mid = m.get('id')
            if not mid or self._layout_has_msg_id(layout, mid):
                continue
            sender = m.get('from_name') or m.get('from_uid') or '?'
            from_uid = m.get('from_uid')
            is_self = bool(my_uid and from_uid == my_uid)
            widget = self._build_bubble_widget(
                sender, m.get('text', ''), self._format_time(m.get('time', '')),
                False, mid, is_self, from_uid, None, convo['key'],
                msg_time=self._server_stamp_to_local_str(m.get('time')))
            layout.insertWidget(index, widget)
            index += 1

    def _is_destroyed_user(self, uid=None, name=None):
        """该消息作者是否为已注销账号（已注销者不可 @ / 私信 / 拉黑 / 禁言）"""
        destroyed = getattr(self.firefly, 'destroyed_users', {}) or {}
        if uid and uid in destroyed:
            return True
        if name:
            try:
                if name in set(destroyed.values()):
                    return True
            except Exception:
                pass
            # 历史记录里匿名化后的昵称（注销专用前缀）
            if name.startswith('账号已注销'):
                return True
        return False

    def _resolve_uid_by_name(self, name):
        for f in (getattr(self.firefly, 'friends', []) or []):
            if f.get('name') == name:
                return f.get('uid')
        for ps in (getattr(self.firefly, 'pending_strangers', []) or []):
            if ps.get('name') == name:
                return ps.get('uid')
        for p in (getattr(self.firefly, 'latest_players', []) or []):
            if p.get('name') == name:
                return p.get('uid')
        for p in (getattr(self.firefly, 'all_players', []) or []):
            if p.get('name') == name:
                return p.get('uid')
        return None

    # ================== 消息加载与显示 ==================
    def load_history(self):
        """从 firefly.chat_history 加载所有历史消息并显示"""
        for msg in self.firefly.chat_history:
            self.add_message_obj(msg)
        self._request_scroll_bottom()

    def add_message_obj(self, msg_obj):
        sender = msg_obj.get('sender') or msg_obj.get('from_name', '?')
        text = msg_obj.get('text')
        time_str = self._format_time(msg_obj.get('time'))
        is_system = msg_obj.get('is_system', False)
        # 重新计算 is_self：优先按 uid，兼容旧消息按名字
        my_uid = self.firefly.mp_client.server_uid or self.firefly.server_uid
        if msg_obj.get('from_uid') and my_uid:
            is_self = (msg_obj['from_uid'] == my_uid)
        else:
            is_self = (sender == self.firefly.online_name) or (sender == self.firefly.current_user.get('username', ''))
        msg_id = msg_obj.get('id') or msg_obj.get('msg_id')
        self.add_message(sender, text, time_str, is_system, msg_id, is_self,
                         from_uid=msg_obj.get('from_uid'), from_perm=msg_obj.get('from_perm'),
                         msg_time=self._server_stamp_to_local_str(msg_obj.get('time')))

    def add_message(self, sender, text, time_str, is_system=False, msg_id=None, is_self=False,
                    from_uid=None, from_perm=None, conv_key='server', msg_time=None):
        convo = self.convos.get(conv_key) or self.convos['server']
        self._append_bubble(convo['layout'], sender, text, time_str, is_system, msg_id, is_self,
                            from_uid=from_uid, from_perm=from_perm, conv_key=convo['key'],
                            msg_time=msg_time)

    def _append_system(self, convo, text):
        self._append_bubble(convo['layout'], '系统', text,
                            datetime.now().strftime('%m-%d %H:%M'), True, None, False,
                            conv_key=convo['key'])

    def _view_near_bottom(self, conv_key):
        """该会话视图是否停在底部附近（仅对正在查看的会话有意义）"""
        if conv_key != self.current_conv:
            return False
        convo = self.convos.get(conv_key)
        if convo is None:
            return False
        sb = convo['scroll'].verticalScrollBar()
        return sb.maximum() - sb.value() <= 60

    def _layout_has_msg_id(self, layout, msg_id):
        """消息区是否已存在该 msg_id 的气泡（历史与实时消息去重用）"""
        if not msg_id:
            return False
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if w is not None and w.property('msg_id') == msg_id:
                return True
        return False

    def _build_bubble_widget(self, sender, text, time_str, is_system=False, msg_id=None,
                             is_self=False, from_uid=None, from_perm=None, conv_key='server',
                             msg_time=None):
        container = QWidget()
        container.setProperty('msg_id', msg_id)
        container.setProperty('msg_time', msg_time)
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        if is_system:
            # 系统消息：占满整行宽度，长文本（如"XX 已被全局禁言（含私信），解除时间：UTC+8 6:00"）
            # 才能完整换行显示；文本必须转义，避免名字/内容里的 < > & 被当作 HTML 导致显示异常
            lbl = QLabel(f"<div align='center' style='color:#AAAAAA; font-size:12px;'>{html.escape(text)}</div>")
            lbl.setWordWrap(True)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            v.addWidget(lbl)
        else:
            # ---------- 名字标签 ----------
            name_lbl = QLabel(sender)
            name_lbl.setStyleSheet("font-size:12px; color:#CCCCCC; background:transparent; font-weight:bold;")
            name_lbl.setAlignment(Qt.AlignRight if is_self else Qt.AlignLeft)

            # ---------- @TA 与右键菜单（仅对非自己的消息） ----------
            if not is_self:
                target_name = sender
                target_uid = from_uid
                # 已注销账号：名字不可点击 @（右键菜单也已拦截）
                if conv_key == 'server' and not self._is_destroyed_user(target_uid, target_name):
                    # 修复：只有服内聊天支持点击名字快速 @；私信里不应 @ 别人
                    name_lbl.setCursor(Qt.PointingHandCursor)

                    def on_name_click(event):
                        # 左键：填充 @用户名 + 空格，并聚焦输入框
                        self.input_edit.setText(f"@{target_name} ")
                        self.input_edit.setFocus()
                        self.input_edit.setCursorPosition(len(self.input_edit.text()))

                    name_lbl.mousePressEvent = on_name_click

                def on_name_menu(pos):
                    self._show_name_menu(name_lbl.mapToGlobal(pos), target_name, target_uid,
                                         conv_key=conv_key)

                name_lbl.setContextMenuPolicy(Qt.CustomContextMenu)
                name_lbl.customContextMenuRequested.connect(on_name_menu)

            v.addWidget(name_lbl)

            # ---------- 气泡 ----------
            bubble_widget = QWidget()
            bubble_layout = QVBoxLayout(bubble_widget)
            bubble_layout.setContentsMargins(0, 0, 0, 0)
            bubble_layout.setSpacing(2)

            bubble = QLabel(text)
            bubble.setTextFormat(Qt.PlainText)  # 用户文本一律纯文本渲染，避免 < > & 破坏显示
            bubble.setWordWrap(True)
            bubble_bg = '#DCF8C6' if is_self else '#FFFFFF'
            bubble.setStyleSheet(f"""
                background-color:{bubble_bg};
                border-radius:14px;
                padding:10px 14px;
                font-size:14px;
                color:#000000;
                font-family: 'HarmonyOS Sans SC','Microsoft YaHei',sans-serif;
            """)
            bubble.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            # 修复：所有会话（服内/私信）的气泡都接右键菜单。
            # 此前仅服内接线，管理员在私信里右键完全无菜单、更无撤回
            bubble.setContextMenuPolicy(Qt.CustomContextMenu)
            # 注意：消息 id 必须在**点击时**从容器属性读取——乐观上屏的气泡
            # 构建时 msg_id 还是 None，回执到达后才由 _finalize_pending_pm 写入，
            # 若在构建时用默认参数捕获就会永远是 None（自己发的私信没有撤回按钮）。
            bubble.customContextMenuRequested.connect(
                lambda pos, s=is_self, wid=bubble, name=sender, perm=from_perm,
                       c=container, k=conv_key:
                self.show_message_menu(pos, c.property('msg_id'), s, wid, name,
                                       from_perm=perm, conv_key=k, container=c)
            )
            bubble_layout.addWidget(bubble)

            time_lbl = QLabel(time_str)
            time_lbl.setStyleSheet("color:#888888; font-size:10px; background:transparent;")
            time_lbl.setAlignment(Qt.AlignRight if is_self else Qt.AlignLeft)
            bubble_layout.addWidget(time_lbl)

            v.addWidget(bubble_widget)
            if is_self:
                v.setAlignment(Qt.AlignRight)
            else:
                v.setAlignment(Qt.AlignLeft)
        return container

    def _append_bubble(self, layout, sender, text, time_str, is_system=False, msg_id=None,
                       is_self=False, from_uid=None, from_perm=None, conv_key='server',
                       msg_time=None):
        # 修复：不再无条件滚到底部。只有用户本来就停留在底部（或消息是自己的）才跟随滚动，
        # 避免阅读历史时被新消息/系统通知强制拉回底部
        near_bottom = self._view_near_bottom(conv_key)
        if msg_id and self._layout_has_msg_id(layout, msg_id):
            return  # 去重：历史记录与实时消息可能是同一条
        if layout.count() > 0:
            last_item = layout.itemAt(layout.count() - 1)
            if last_item.spacerItem():
                layout.removeItem(last_item)
        container = self._build_bubble_widget(sender, text, time_str, is_system, msg_id,
                                              is_self, from_uid, from_perm, conv_key,
                                              msg_time=msg_time)
        layout.addWidget(container)
        layout.addStretch()
        if near_bottom or is_self:
            self._request_scroll_bottom(conv_key)

    def _show_name_menu(self, global_pos, sender, from_uid, conv_key='server'):
        """右键玩家名字：@TA（仅服内聊天）/ 私信 / 拉黑"""
        menu = DarkRoundedMenu(self)
        uid = from_uid or self._resolve_uid_by_name(sender)
        # 已注销账号：不允许 @ / 私信 / 拉黑 / 禁言
        if self._is_destroyed_user(uid, sender):
            na = menu.addAction("🚫 账号已注销，无法 @ / 私信 / 拉黑")
            na.setEnabled(False)
            menu.exec_(global_pos)
            return
        if conv_key == 'server':
            at_action = menu.addAction("@TA")
            at_action.triggered.connect(lambda: self._insert_at_mention(sender))
        my_uid = self.firefly.mp_client.server_uid or self.firefly.server_uid
        if uid and uid != my_uid:
            pm_action = menu.addAction("💬 私信")
            pm_action.triggered.connect(lambda: self.open_pm_with(uid, sender))
            menu.addSeparator()
            is_blocked = uid in (getattr(self.firefly, 'blocked', []) or [])
            block_action = menu.addAction("✅ 取消拉黑" if is_blocked else "⛔ 拉黑")
            block_action.triggered.connect(lambda: self.firefly.send_block(uid, not is_blocked))
        # 管理员：右键用户可执行 服内 / 全局禁言（对管理员目标不显示）
        if uid:
            build_admin_mute_menu(menu, self.firefly, uid, sender,
                                  self._target_permission(uid),
                                  self._target_ban_info(uid), self)
        menu.exec_(global_pos)

    # ================== 右键菜单 ==================
    def _push_prefs(self):
        """把会话状态（特别关注/免打扰/置顶/不再显示）提交到服务端保存"""
        try:
            self.firefly.save_chat_prefs(pinned=sorted(self.pinned_keys),
                                         hidden=sorted(self.hidden_keys))
        except Exception:
            pass

    def on_chat_prefs_data(self):
        """服务端会话状态下发：同步置顶/不再显示集合并刷新列表"""
        self.pinned_keys = set(getattr(self.firefly, 'pm_pinned', []) or []) | {'server'}
        self.hidden_keys = set(getattr(self.firefly, 'pm_hidden', []) or [])
        self._rebuild_conv_list()

    def _save_conv_panel_width(self):
        """记住会话面板宽度（下次打开恢复）"""
        try:
            sizes = self.body_splitter.sizes()
            if sizes:
                self._conv_settings.setValue("chat_conv_panel_width", int(sizes[0]))
        except Exception:
            pass

    def _auto_fit_conv_panel(self):
        """双击分隔条：按会话列表内容自适应面板宽度（保证文字显示完整）"""
        try:
            fm = self.conv_list.fontMetrics()
            max_w = 0
            for i in range(self.conv_list.count()):
                item = self.conv_list.item(i)
                if item is not None:
                    max_w = max(max_w, fm.horizontalAdvance(item.text()))
            target = max_w + 16 + 24 + 18  # 条目内边距 + 面板边距 + 竖直滚动条
            target = max(target, 150)
            sizes = self.body_splitter.sizes()
            total = sum(sizes) if sum(sizes) > 0 else 1050
            target = min(target, max(total - 200, 150))
            self.body_splitter.setSizes([target, total - target])
            self._save_conv_panel_width()
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # 双击分栏手柄：会话面板按内容自适应
        try:
            if event.type() == QEvent.MouseButtonDblClick and obj is self.body_splitter.handle(1):
                self._auto_fit_conv_panel()
                return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _server_time_to_local(self, raw):
        """服务端时间字符串 → 本地时间（用服务端时区偏移换算，避免时区/校时误差）"""
        if not raw:
            return None
        try:
            t = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S')
        except Exception:
            return None
        try:
            server_off = float(getattr(self.firefly, 'server_timezone_offset', 0) or 0)
            local_off = -time.timezone / 3600.0
            t = t + timedelta(hours=local_off - server_off)
        except Exception:
            pass
        return t

    def _server_stamp_to_local_str(self, raw):
        t = self._server_time_to_local(raw)
        return t.strftime('%Y-%m-%d %H:%M:%S') if t is not None else None

    def _recall_window_open(self, container, limit_seconds):
        """撤回时间窗口判断（msg_time 统一存本地时间）。

        实时消息取本机接收时间，历史消息按服务端时区换算，避免时区/校时误差
        把刚发的消息误判为超时。无时间信息时交由服务端判定。"""
        raw = container.property('msg_time') if container is not None else None
        if not raw:
            return True
        try:
            t = datetime.strptime(str(raw), '%Y-%m-%d %H:%M:%S')
        except Exception:
            return True
        return (datetime.now() - t).total_seconds() <= limit_seconds

    def _pm_recall_window_open(self, container):
        """私信自己消息的撤回窗口：一律 2 分钟（管理员也受限）"""
        return self._recall_window_open(container, 120)

    def show_message_menu(self, pos, msg_id, is_self, widget, sender, from_perm='user',
                          conv_key='server', container=None):
        menu = DarkRoundedMenu(self)
        # 判断当前用户是否为管理员（联机模式下）
        is_admin = False
        if self.firefly.mp_client.connected:
            # 双来源兜底：连接回执的权限 + 登录成功的权限，任一为 admin 即视为管理员
            is_admin = (self.firefly.mp_client.server_permission == 'admin'
                        or getattr(self.firefly, 'is_admin', False))

        # 作者是否已注销：已注销者的消息不可 @、不可撤回，只允许删除本地记录
        destroyed_author = self._is_destroyed_user(self._resolve_uid_by_name(sender), sender)

        # ---------- 添加 @TA 选项（仅服内聊天、非自己的消息） ----------
        # 修复：私信（好友/陌生人）里不应 @ 别人；已注销账号不可 @
        if not is_self and conv_key == 'server' and not destroyed_author:
            at_action = menu.addAction("@TA")
            at_action.triggered.connect(
                lambda: self._insert_at_mention(sender)
            )

        # ---------- 撤回选项 ----------
        can_recall = False
        if msg_id and not destroyed_author:
            if is_self:
                if conv_key == 'server':
                    # 服内：管理员撤回自己的消息不限时；普通用户 5 分钟内
                    can_recall = True if is_admin else self._recall_window_open(container, 300)
                else:
                    # 私信：自己的消息一律 2 分钟内可撤回（管理员也受限）
                    can_recall = self._recall_window_open(container, 120)
            elif is_admin and conv_key == 'server':
                # 服内聊天：管理员可撤回任何时段任何人的消息（含其他管理员，以及
                # 管理员期间发送、之后被降级的作者）
                can_recall = True
            # 私信（好友/陌生人）：任何人都不能撤回他人的消息（管理员也不行）
        if can_recall:
            recall_action = menu.addAction("↩️ 撤回")
            recall_action.triggered.connect(lambda: self.recall_message(msg_id))

        # ---------- 删除本地（危险操作，单独分组） ----------
        if can_recall or not is_self:
            menu.addSeparator()
        delete_action = menu.addAction("🗑 删除")
        delete_action.triggered.connect(lambda: self.delete_message_local(msg_id))

        menu.exec_(widget.mapToGlobal(pos))

    def _insert_at_mention(self, name):
        """辅助方法：在输入框插入 @用户名 """
        self.input_edit.setText(f"@{name} ")
        self.input_edit.setFocus()
        self.input_edit.setCursorPosition(len(self.input_edit.text()))

    def recall_message(self, msg_id):
        if not self.firefly.mp_client.connected:
            QMessageBox.warning(self, "提示", "未连接到服务器")
            return
        # 自定义确认对话框
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("撤回消息")
        msg_box.setText("确定要撤回这条消息吗？")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        # 设置样式表使文字白色
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
            }
            QLabel {
                color: white;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton[text="No"] {
                background-color: #666666;
            }
            QPushButton[text="No"]:hover {
                background-color: #888888;
            }
        """)
        # 居中显示
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg_box.move(screen_center - msg_box.rect().center())
        reply = msg_box.exec_()
        if reply == QMessageBox.Yes:
            self.firefly.mp_client.send_command(f"/recall {msg_id}")

    def _on_viewport_right_click(self, view_pos):
        """滚动区域空白处右键回调（仅服内聊天支持清空记录）"""
        if self._current_convo()['type'] != 'server':
            return
        convo = self._current_convo()
        if convo is None:
            return
        container = convo['widget']
        local_pos = container.mapFrom(convo['scroll'].viewport(), view_pos)
        hit_widget = container.childAt(local_pos)
        if hit_widget and "QScrollBar" in hit_widget.metaObject().className():
            hit_widget = None

        if hit_widget is None:
            menu = QMenu(self)
            clear_action = menu.addAction("清空记录")
            clear_action.triggered.connect(self.clear_history)
            menu.exec_(convo['scroll'].viewport().mapToGlobal(view_pos))

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            pos = event.pos()
            convo = self._current_convo()
            if convo is None or convo['type'] != 'server':
                return
            viewport = convo['scroll'].viewport()
            viewport_pos = viewport.mapFrom(self, pos)
            if viewport.rect().contains(viewport_pos):
                container = convo['widget']
                local_pos = container.mapFrom(viewport, viewport_pos)
                # 获取最底层子控件
                child = container.childAt(local_pos)
                if child is None:
                    # 完全空白
                    hit = False
                else:
                    # 检查 child 是否是消息容器（具有 msg_id 属性）
                    if child.property('msg_id') is not None:
                        # 检查点击是否在该容器的内部子控件上
                        child_local = child.mapFrom(container, local_pos)
                        hit = False
                        for sub in child.children():
                            if isinstance(sub, QWidget) and sub.isVisible():
                                sub_pos = sub.mapFrom(child, child_local)
                                if sub.rect().contains(sub_pos):
                                    hit = True
                                    break
                        if not hit:
                            hit = False
                        # 否则 hit 保持 True（点击到消息内容）
                    else:
                        hit = True  # 避免误触
                if not hit:
                    menu = DarkRoundedMenu(self)
                    clear_action = menu.addAction("清空记录")
                    clear_action.triggered.connect(self.clear_history)
                    menu.exec_(event.globalPos())
                    event.accept()
                    return
        super().mousePressEvent(event)

    def delete_message_local(self, msg_id):
        """删除本地消息（服内 + 所有私信会话）"""
        self.firefly.chat_history = [m for m in self.firefly.chat_history
                                     if (m.get('id') or m.get('msg_id')) != msg_id]
        for convo in self.convos.values():
            layout = convo['layout']
            for i in reversed(range(layout.count())):
                item = layout.itemAt(i)
                if item.widget() and item.widget().property('msg_id') == msg_id:
                    widget = item.widget()
                    layout.removeWidget(widget)
                    widget.deleteLater()
                    break
        self.scroll_to_bottom()

    def remove_pm_bubble_by_id(self, msg_id, note):
        """私信被撤回：移除对应气泡并显示系统提示"""
        for convo in self.convos.values():
            if convo['type'] == 'server':
                continue
            layout = convo['layout']
            for i in range(layout.count()):
                w = layout.itemAt(i).widget()
                if w is not None and w.property('msg_id') == msg_id:
                    layout.removeWidget(w)
                    w.deleteLater()
                    self._append_system(convo, f'↩️ {note}')
                    break

    def clear_history(self, confirm=True):
        if confirm:
            if not self._confirm_clear_history():
                return
        self.firefly.chat_history.clear()
        # 彻底清空布局
        self._clear_message_layout(self.msg_layout)
        self.msg_container.updateGeometry()
        self.convos['server']['scroll'].update()
        self.convos['server']['scroll'].verticalScrollBar().setValue(0)
        self.convos['server']['scroll'].verticalScrollBar().update()

    def _confirm_clear_history(self):
        # 自定义确认对话框（文字白色）
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("确认清空")
        msg_box.setText("确定要清空所有聊天记录吗？")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
            }
            QLabel {
                color: white;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton[text="No"] {
                background-color: #666666;
            }
            QPushButton[text="No"]:hover {
                background-color: #888888;
            }
        """)
        reply = msg_box.exec_()
        return reply == QMessageBox.Yes

    # ---------- 刷新全部消息（用于撤回后重绘） ----------
    def refresh_all_messages(self):
        # 清空服内聊天的消息控件后重新加载。
        # 注意：不能无条件滚到底部——此前系统广播（如"XXX 进入了服务器"）到来时，
        # 整段消息重建后会把正在翻看历史的用户强行拉到底部。
        scroll = self._server_scroll
        sb = scroll.verticalScrollBar()
        was_bottom = (sb.maximum() - sb.value()) <= 60
        old_val = sb.value()
        self._clear_message_layout(self.msg_layout)
        for msg in self.firefly.chat_history:
            self.add_message_obj(msg)
        if was_bottom:
            self._request_scroll_bottom()
        else:
            # 保持原阅读位置（内容高度变化后按原值尽力恢复）
            QTimer.singleShot(0, lambda v=old_val, bar=sb: self._restore_scroll(bar, v))

    def _restore_scroll(self, bar, value):
        try:
            bar.setValue(min(value, bar.maximum()))
        except Exception:
            pass

    # ---------- 滚动到底部 ----------
    def _convo_by_scroll(self, scroll):
        for c in self.convos.values():
            if c.get('scroll') is scroll:
                return c
        return None

    def _on_scroll_range_changed(self, scroll, mn, mx):
        """滚动范围变化（布局生效）时兑现"滚到底部"请求——修复新内容 setValue 停在旧底部"""
        convo = self._convo_by_scroll(scroll)
        if convo is not None and convo.pop('_pending_scroll_bottom', False):
            scroll.verticalScrollBar().setValue(mx)

    def _request_scroll_bottom(self, conv_key=None):
        """请求滚动到底部：立即滚动一次，并在布局更新后（rangeChanged）再兑现一次。
        新内容加入时 maximum 还是旧值，直接 setValue 会停在旧的底部位置。"""
        convo = self.convos.get(conv_key) if conv_key else self._current_convo()
        if convo is None:
            return
        convo['_pending_scroll_bottom'] = True
        sb = convo['scroll'].verticalScrollBar()
        sb.setValue(sb.maximum())
        # 兜底：范围未变化则清除挂起标记，避免之后尺寸变化意外跳底
        QTimer.singleShot(400, lambda c=convo: c.pop('_pending_scroll_bottom', None))

    def scroll_to_bottom(self):
        convo = self._current_convo()
        if convo is None:
            return
        scrollbar = convo['scroll'].verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ---------- 禁言与状态更新 ----------
    def update_chat_status(self, enabled):
        if not enabled:
            self.input_edit.setEnabled(False)
            self.input_edit.setReadOnly(True)
            self.input_edit.setPlaceholderText("❌ 聊天功能已被管理员关闭")
            self.send_btn.setEnabled(False)
            self.send_btn.setText("已关闭")
        else:
            if self._muted:
                pass
            else:
                self.input_edit.setEnabled(True)
                self.input_edit.setReadOnly(False)
                self.input_edit.setPlaceholderText("输入消息，支持 @用户名 或 @所有人")
                self.send_btn.setEnabled(True)
                self.send_btn.setText("发送")
        self._refresh_ui()

    def update_common_mute_status(self, enabled):
        self.common_mute = enabled
        self._refresh_ui()

    def _update_countdown(self):
        if not self._muted or self._muted_dt is None:
            self.countdown_timer.stop()
            return
        now = datetime.now(self._muted_dt.tzinfo)
        if self._muted_dt <= now:
            self.update_mute_status(None)
        else:
            remaining = self._format_remaining(self._muted_dt)
            self.input_edit.setPlaceholderText(f"你已被禁言，剩余 {remaining}")
            if self.input_edit.isEnabled():
                self.input_edit.setEnabled(False)
                self.input_edit.setReadOnly(True)
                self.send_btn.setEnabled(False)
                self.send_btn.setText("禁言中")

    def update_mute_status(self, muted_until, permanent=False):
        self.countdown_timer.stop()
        if permanent and not muted_until:
            self._muted = True
            self._muted_perm = True
            self._muted_until = None
            self._muted_dt = None
            self._refresh_ui()
            return
        self._muted_perm = False
        if muted_until is None:
            self._muted = False
            self._muted_until = None
            self._muted_dt = None
        else:
            try:
                dt = datetime.fromisoformat(muted_until)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                now = datetime.now(dt.tzinfo)
                if dt > now:
                    self._muted = True
                    self._muted_until = muted_until
                    self._muted_dt = dt
                else:
                    self._muted = False
                    self._muted_until = None
                    self._muted_dt = None
            except Exception as e:
                print(f"[禁言] 解析时间失败: {e}")
                self._muted = False
                self._muted_until = None
                self._muted_dt = None
        self._refresh_ui()

    def update_ban_banner(self):
        """刷新消息区顶部禁令横幅：自己私信封禁 > 当前会话对方被禁 > 隐藏"""
        try:
            ff = self.firefly
            if getattr(ff, 'self_pm_ban_text', ''):
                self.ban_banner.setText(
                    f"❗你已被禁止私信,无法回复/接收别人的消息!解除时间:{ff.self_pm_ban_text}")
                self.ban_banner.setVisible(True)
                return
            convo = self._current_convo()
            if convo is not None and convo.get('type') != 'server' and convo.get('uid'):
                text = (getattr(ff, 'user_pm_bans', {}) or {}).get(convo['uid'])
                if text:
                    self.ban_banner.setText(
                        f"❗该用户已被禁止私信,对方无法回复/接收你的消息!解除时间:{text}")
                    self.ban_banner.setVisible(True)
                    return
            self.ban_banner.setVisible(False)
        except Exception:
            traceback.print_exc()

    def _target_permission(self, uid):
        """查询目标玩家权限（用于判断是否显示禁言菜单）"""
        for src in (getattr(self.firefly, 'latest_players', []) or [],
                    getattr(self.firefly, 'all_players', []) or [],
                    getattr(self.firefly, 'friends', []) or [],
                    getattr(self.firefly, 'pending_strangers', []) or []):
            for p in src:
                if p.get('uid') == uid and p.get('permission'):
                    return p.get('permission')
        return None

    def _target_ban_info(self, uid):
        """查询目标玩家的禁言状态（服内禁言 + 私信封禁）"""
        for src in (getattr(self.firefly, 'latest_players', []) or [],
                    getattr(self.firefly, 'all_players', []) or [],
                    getattr(self.firefly, 'friends', []) or [],
                    getattr(self.firefly, 'pending_strangers', []) or []):
            for p in src:
                if p.get('uid') == uid:
                    return {'pm_ban': p.get('pm_ban'), 'mute_server': p.get('mute_server')}
        ff = self.firefly
        return {'pm_ban': {'banned': uid in (getattr(ff, 'user_pm_bans', {}) or {})},
                'mute_server': (getattr(ff, 'user_server_mutes', {}) or {}).get(uid) or {}}

    def _current_conv_waiting(self):
        convo = self._current_convo()
        return convo is not None and convo.get('type') != 'server' and convo.get('waiting', False)

    def _refresh_ui(self):
        if not getattr(self.firefly, 'chat_enabled', True):
            # 聊天被管理员关闭：任何状态下都保持禁用
            self.input_edit.setEnabled(False)
            self.input_edit.setReadOnly(True)
            self.input_edit.setPlaceholderText("❌ 聊天功能已被管理员关闭")
            self.send_btn.setEnabled(False)
            self.send_btn.setText("已关闭")
            self.countdown_timer.stop()
            return
        if not self.is_admin and self.common_mute:
            self.input_edit.setEnabled(False)
            self.input_edit.setReadOnly(True)
            self.input_edit.setPlaceholderText("🔇 全体禁言中")
            self.send_btn.setEnabled(False)
            self.send_btn.setText("禁言中")
            self.countdown_timer.stop()
        else:
            if self._muted and self._muted_perm:
                self.input_edit.setEnabled(False)
                self.input_edit.setReadOnly(True)
                self.send_btn.setEnabled(False)
                self.send_btn.setText("禁言中")
                self.input_edit.setPlaceholderText("你已被禁言（永久）")
                self.countdown_timer.stop()
            elif self._muted and self._muted_dt is not None:
                self.input_edit.setEnabled(False)
                self.input_edit.setReadOnly(True)
                self.send_btn.setEnabled(False)
                self.send_btn.setText("禁言中")
                remaining = self._format_remaining(self._muted_dt)
                self.input_edit.setPlaceholderText(f"你已被禁言，剩余 {remaining}")
                self.countdown_timer.start()
            elif self._current_conv_waiting():
                # 陌生人等待回复状态：暂停输入
                self.input_edit.setEnabled(False)
                self.input_edit.setReadOnly(True)
                self.send_btn.setEnabled(False)
                self.send_btn.setText("等待回复")
                self.input_edit.setPlaceholderText("已发送消息，对方回复后你们将成为好友")
                self.countdown_timer.stop()
            else:
                self.input_edit.setEnabled(True)
                self.input_edit.setReadOnly(False)
                self.input_edit.setPlaceholderText("输入消息，支持 @用户名 或 @所有人")
                self.send_btn.setEnabled(True)
                self.send_btn.setText("发送")
                self.countdown_timer.stop()

    def _format_remaining(self, dt):
        if dt is None:
            return "已过期"
        now = datetime.now(dt.tzinfo)
        if dt <= now:
            return "即将解禁"
        delta = dt - now
        days = delta.days
        seconds = delta.seconds
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if days > 0:
            return f"{days}天{hours}小时"
        elif hours > 0:
            return f"{hours}小时{minutes}分"
        else:
            if minutes > 0:
                if secs > 0:
                    return f"{minutes}分{secs}秒"
                else:
                    return f"{minutes}分"
            else:
                return f"{secs}秒"

    def _mention_candidates(self):
        """服内聊天输入 @ 时的用户候选；被禁言或私信会话不提供"""
        if self.current_conv != 'server':
            return []
        if getattr(self, '_muted', False) or not self.input_edit.isEnabled():
            return []
        ff = self.firefly
        names = []
        for p in (getattr(ff, 'all_players', None) or []):
            n = (p or {}).get('name')
            if n and n not in names:
                names.append(n)
        for p in (getattr(ff, 'latest_players', None) or []):
            n = (p or {}).get('name')
            if n and n not in names:
                names.append(n)
        for f in (getattr(ff, 'friends', None) or []):
            n = f.get('name')
            if n and n not in names:
                names.append(n)
        try:
            me = self.get_my_name()
            names = [n for n in names if n != me]
        except Exception:
            pass
        # 已注销用户不可被 @
        destroyed = getattr(ff, 'destroyed_users', {}) or {}
        if destroyed:
            labels = set(destroyed.values())
            names = [n for n in names if n not in labels]
        return names

    def _completer_provider(self):
        """命令补全数据源：是否管理员 + 玩家名字（在线优先，排除自己）"""
        ff = self.firefly

        def users(online_only=False):
            names = {}
            for p in (getattr(ff, 'all_players', None) or []):
                n = (p or {}).get('name')
                if n:
                    names[n] = bool(p.get('online'))
            for p in (getattr(ff, 'latest_players', None) or []):
                n = (p or {}).get('name')
                if n:
                    names[n] = True
            try:
                names.pop(ff.get_my_name(), None)
            except Exception:
                pass
            items = sorted(names.items(), key=lambda kv: (0 if kv[1] else 1, kv[0]))
            if online_only:
                return [n for n, on in items if on]
            return [n for n, _on in items]

        return _is_admin_context(ff), users

    def get_my_name(self):
        if self.firefly.mp_client.connected:
            return self.firefly.online_name or self.firefly.current_user['username']
        return self.firefly.current_user['username']

    def _format_time(self, raw_time):
        try:
            dt = datetime.strptime(raw_time, '%Y-%m-%d %H:%M:%S')
            return dt.strftime('%m-%d %H:%M')
        except:
            return raw_time

    # ---------- 发送消息 ----------
    def send_message(self):
        # 强制同步禁言状态
        self.update_mute_status(self.firefly.muted_until,
                                permanent=bool(getattr(self.firefly, 'muted_perm', False)))
        if self.firefly.muted_until is None and not getattr(self.firefly, 'muted_perm', False):
            self._muted = False
            self._muted_until = None
            self._muted_dt = None
            self._refresh_ui()

        if not self.firefly.chat_enabled:
            QMessageBox.warning(self, "提示", "❌ 聊天功能已被管理员关闭")
            return
        if not self.is_admin and self.firefly.common_mute:
            QMessageBox.warning(self, "提示", "❌ 全体禁言中，无法发送消息")
            return
        if self._muted and self._muted_dt and self._muted_dt <= datetime.now(self._muted_dt.tzinfo):
            self.update_mute_status(None)
        if self.firefly.muted_until and not self._muted:
            self.update_mute_status(self.firefly.muted_until)
        elif not self.firefly.muted_until and self._muted and not self._muted_perm:
            self.update_mute_status(None)
        if self._muted:
            if self._muted_perm:
                QMessageBox.warning(self, "提示", "你已被禁言（永久），无法发送消息")
                return
            if self._muted_until:
                try:
                    muted_dt = datetime.fromisoformat(self._muted_until)
                    if muted_dt > datetime.now().astimezone():
                        remaining = self._format_remaining(muted_dt)
                        if self.input_edit.isEnabled():
                            self.input_edit.setEnabled(False)
                            self.input_edit.setReadOnly(True)
                            self.send_btn.setEnabled(False)
                            self.send_btn.setText("禁言中")
                        QMessageBox.warning(self, "提示", f"你已被禁言，剩余 {remaining}")
                        return
                except:
                    pass
            self.update_mute_status(None)
        if self.firefly.banned:
            QMessageBox.warning(self, "提示", "你已被封禁，无法发送消息")
            return
        if not self.input_edit.isEnabled():
            QMessageBox.warning(self, "提示", "你已被禁言，无法发送消息")
            return
        if self._sending:
            return
        self._sending = True
        try:
            text = self.input_edit.text().strip()
            if not text:
                return
            convo = self._current_convo()
            if convo is not None and convo['type'] != 'server':
                # ---------- 私信发送（乐观上屏：失败/网络不佳显示红色感叹号，点击重试） ----------
                if convo.get('waiting'):
                    QMessageBox.warning(self, "提示", "已发送过消息，等待对方回复后才能继续")
                    return
                if text.startswith('/'):
                    QMessageBox.warning(self, "提示", "命令请在服内聊天中使用")
                    return
                # 注意：这里不做本地禁言拦截——本地缓存可能滞后（对方刚被解禁），
                # 误拦截会造成"已解禁却无法发送"。服务端是权威：真的被禁会被拒收，
                # 回执会更新本地缓存与红色横幅；未被禁则消息正常送达。
                if convo.get('destroyed') or (getattr(self.firefly, 'destroyed_users', {}) or {}).get(convo.get('uid')):
                    QMessageBox.warning(self, "提示", "账号已注销，私信已关闭")
                    return
                self.input_edit.clear()
                self._send_pm_optimistic(convo, text)
                return
            if text.startswith('/'):
                self.firefly.mp_client.send_command(text)
            else:
                self.firefly.mp_client.send_chat(text)
            self.input_edit.clear()
        finally:
            self._sending = False

    def append_message(self, msg):
        data = msg['data']
        self.add_message(
            sender=data.get('from_name', '?'),
            text=data['text'],
            time_str=self._format_time(data.get('time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))),
            is_system=data.get('is_system', False)
        )


# -------------------------- 核心桌宠类 --------------------------
class Firefly(QWidget):
    website_check_done = pyqtSignal(bool, str)
    inventory_updated = pyqtSignal()
    def __init__(self, user_data=None, parent=None):
        super(Firefly, self).__init__(parent)

        # ---------- 修复 QtWebEngineProcess.exe 找不到的问题 ----------
        if getattr(sys, 'frozen', False):
            base_dir = get_base_dir()
            bin_dir = os.path.join(base_dir, "bin")
            process_path = os.path.join(bin_dir, "QtWebEngineProcess.exe")
            if os.path.exists(process_path):
                os.environ["QTWEBENGINE_PROCESS_PATH"] = process_path
            if os.path.exists(bin_dir):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        # -------------------------------------------------------------
        self.current_user = user_data
        self.is_debugger = self.current_user.get('userid') == 'debugger' if self.current_user else False
        self.debug_mode = os.environ.get('FIREFLY_DEBUG_MODE', 'off') == 'on'
        self.online_name = None
        self.label = QLabel("", self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.resize(500, 500)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.tray_icon = None
        self.movie = None
        self.draggable = False
        self.offset = None
        self.current_animation_state = "Standby"
        self.last_animation_state = "Standby"
        self.last_update_time = time.time()
        self.settings = QSettings("GUCNMC", "流萤桌宠")
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.changeToDiscomfort)
        self.timer.start(1800000)
        self.music_file = os.path.join(get_base_dir(), "Large_Music", "不眠之夜.wav")
        self.music_file2 = os.path.join(get_base_dir(), "Large_Music", "打上花火.wav")
        self.music_file3 = os.path.join(get_base_dir(), "Large_Music", "520AM.wav")
        self.music_file4 = os.path.join(get_base_dir(), "Large_Music", "Dream of firefly.wav")
        self.music = None
        self.possible_file_names = ["流萤指针", "萨姆指针", "无指针"]
        self.GUI_file_names = ["Large", "small", "normal", "medium"]
        self.current_file_name = self.get_existing_file_name()
        self.setMouseTracking(True)
        self.installEventFilter(self)
        self.persistent_state = self.current_user.get("last_state", "Standby") if self.current_user else "Standby"

        if self.current_user:
            self.current_user = ensure_user_fields(self.current_user)
            # ===== 修复时装列表：移除无效时装 =====
            valid_clothes = list(AVAILABLE_CLOTHES.keys())
            owned = self.current_user.get('owned_clothes', ['normal'])
            filtered = [c for c in owned if c in valid_clothes]
            if 'normal' not in filtered:
                filtered.append('normal')
            if filtered != owned:
                self.current_user['owned_clothes'] = filtered
                self._save_current_user()
            self.apply_cursor_style()
            self.apply_pet_size()
            # ★ 添加：应用当前时装（自动修正无效时装）
            self.apply_current_clothes()
        self.last_interaction_time = time.time()
        self.emo_random_delay = random.randint(0, 15 * 60)
        self.sleep_start_time = None
        self.anim_restore_timer = None
        self.wake_action_tray = None
        if hasattr(self, 'timer') and self.timer:
            self.timer.stop()
        self.emo_check_timer = QTimer(self)
        self.emo_check_timer.timeout.connect(self.check_emo)
        self.emo_check_timer.start(1000)
        self.mp_client = MultiplayerClient(self)  # 注意传入 self
        self.shop_window = None  # 商店窗口引用
        self.mp_client.connection_changed.connect(self.on_mp_status_changed)
        self.mp_client.mentioned.connect(self.on_mentioned)
        self.mp_client.online_list_updated.connect(self.update_online_list_overlay)
        self.mp_client.login_failed.connect(self.on_mp_login_failed)
        self.mp_client.login_success.connect(self.on_login_success_mp)
        self.mp_client.message_received.connect(self.on_global_message)
        self.mp_client.permission_changed.connect(self.on_permission_changed)


        self.online_overlay = OnlineListOverlay(self)
        self.chat_window = None
        self.server_uid = None
        self.server_permission = 'user'
        self.online_inventory = None
        self.local_inventory_backup = None
        self.single_player_state = None
        self.online_mailbox = []
        self._pending_mail_request = None
        self.online_signed_today = False
        self.friends = []            # 好友列表 [{uid,name,remark,online}]
        self.pending_strangers = []  # 已发私信等待回复的陌生人 [{uid,name,time}]
        self.blocked = []            # 拉黑名单 [uid,...]（friends_data 下发）
        # 会话状态（特别关注/免打扰/置顶/不再显示）以**服务端**为准，登录后由 friends_data 下发
        self.pm_special_focus = []
        self.pm_muted = []
        self.pm_pinned = []
        self.pm_hidden = []
        self.destroy_pending_until = None   # 注销冷却期截止（联机销号）
        self.destroyed_users = {}           # {uid: 账号已注销#编号} 已注销账号
        self.gacha_config = {}              # 服务端下发的卡池配置（标题/封面/配色/消耗等）
        self.gacha_pity = {}                # 各卡池的保底进度 {pool_id: {pity, guaranteed}}
        self.muted_perm = False          # 服内永久禁言（生效值，含全局禁言的影响）
        self.server_muted_until = None   # 服内禁言原始到期时间（不含全局禁言）
        self.server_muted_perm = False   # 服内永久禁言原始标记
        self.self_pm_ban = None          # 自己的全局禁言（私信封禁）状态
        self.self_pm_ban_text = ''       # 自己私信封禁的解除时间文本（''=未被禁）
        self.user_pm_bans = {}           # {uid: 解除时间文本} 他人私信封禁
        self.user_server_mutes = {}      # {uid: 服内禁言状态} 他人服内禁言
        # 旧版本存在本地配置里的设置：首次登录迁移到服务端
        self._legacy_prefs_pending = {
            'focus': list((self.current_user or {}).get('pm_special_focus', []) or []),
            'muted': list((self.current_user or {}).get('pm_muted', []) or []),
        }
        self._pm_backlog = []        # 聊天窗口创建前收到的私信缓存
        self.latest_players = []     # 最近一次在线玩家名单
        self.all_players = []        # 所有加入过服务器的玩家（在线+离线）
        self._all_players_cb = None  # 请求全部玩家名单后的回调
        self.mp_client.sign_result.connect(self.on_sign_result)
        self.mp_client.command_result_signal.connect(self.on_command_result)
        self.mp_client.redeem_result_signal.connect(self.on_redeem_result)
        self.online_redeemed_codes = []
        self.mp_client.sign_result_signal.connect(self.on_sign_result)
        self.mp_client.claim_mail_result_signal.connect(self.on_claim_mail_result)
        self.mp_client.mails_ready.connect(self.on_mails_ready)
        self.chat_history = []
        self._refresh_chat_pending = False
        self.mp_client.chat_history_ready.connect(self.on_chat_history_ready)
        self.mp_client.all_players_ready.connect(self.on_all_players)
        self.mp_client.pm_history_ready.connect(self.on_pm_history_data)
        self.volume = self.current_user.get("volume", 0.5) if self.current_user else 0.5
        if pygame.mixer.get_init():
            pygame.mixer.music.set_volume(self.volume)
        self.connecting = False
        QApplication.instance().focusChanged.connect(self.on_focus_changed)
        self._initialized = False
        self._was_connected = False
        self.mp_client.mentioned_all.connect(self.on_mentioned_all)
        self.banned = False  # 封禁标志
        # 连接封禁信号
        self.mp_client.banned_by_server.connect(self.on_server_banned)
        self._ban_dialog_showing = False
        self._was_connected = False
        self._is_manual_disconnect = False  # 标记是否主动断开
        self.mp_client.server_shutdown.connect(self.on_server_shutdown)
        logging.basicConfig(filename=os.path.join(get_writable_dir(), 'firefly_client.log'), level=logging.DEBUG,
                            format='%(asctime)s %(levelname)s %(message)s')
        self._pending_new_name = None
        self._waiting_for_name = False
        self.rename_required = False
        self._rename_dialog = None
        self._pending_rename_dialog = None
        self.allow_move = False
        self.is_moving = False
        self.move_speed = 100.0  # 像素/秒
        self.move_timer = QTimer(self)
        self.move_timer.timeout.connect(self.start_random_move)

        self.move_animation_timer = QTimer(self)
        self.move_animation_timer.timeout.connect(self.update_move_position)
        self.move_target_x = self.move_target_y = 0
        self.move_start_x = self.move_start_y = 0
        self.move_start_time = 0.0
        self.move_duration = 0.0
        self.allow_move = self.current_user.get("allow_move", False) if self.current_user else False
        self._report_generating = False
        self.online_stats = None
        self._restore_from_backup_if_needed()
        self._suppress_disconnect_message = False
        self.muted_until = None
        if self.chat_window:
            self.chat_window.update_mute_status(self.muted_until,
                                               bool(getattr(self, 'muted_perm', False)))
        self.ai_enabled = True
        self.chat_enabled = True  # 默认开启
        self.common_mute = False
        self.is_admin = False
        self.ping_timer = QTimer(self)
        self.ping_timer.timeout.connect(self._on_ping_timer)
        self.ping_timer.setInterval(10000)  # 10秒
        self.server_display_name = "流萤服务器"  # 默认值
        self.rename_required = False
        self._rename_dialog = None
        self.online_uid = ''  # 新增，存储服务端返回的 uid 字段
        self._secret_action = None
        self._website_status = None
        self._website_title = None
        self.website_check_done.connect(self._on_website_check_done)
        self._start_website_check()
        # ---------- 服务端动态扩展 ----------
        self.server_custom_menu = []  # 服务端下发的菜单项
        self.server_custom_items = []  # 服务端下发的背包物品
        self.shop_goods = []           # 服务端下发的商店商品（商店管理维护）
        self._shop_goods_seen = False  # 服务端是否已明确下发过商店清单（含空清单）
        self.server_version = '未知'    # 服务端版本（排查商店请求无响应）
        self._custom_clothes_ids = set()  # 已登记的自定义时装（用于删除后注销）
        self._auto_download_resources = False  # 用户偏好：是否自动下载（可从配置文件读取）
        self._load_auto_download_pref()
        self._current_gacha_bridge = None
        QWebEngineProfile.defaultProfile()
        self.server_resource_dir = os.path.join(get_writable_dir(), "server_resources")
        self.cache_dir = os.path.join(get_writable_dir(), "web_cache_gacha")
        os.makedirs(self.server_resource_dir, exist_ok=True)
        self.debug_mode = os.environ.get('FIREFLY_DEBUG_MODE', 'off') == 'on'
        self.online_player_count = 0
        self.max_players = None
        self._is_kicked = False
        self._kick_pending = False

    def center_window(self, window, parent=None, screen_center=True):
        """
        将窗口居中于屏幕或父窗口（支持多显示器，对 QMessageBox 不调整尺寸）。
        """
        # 1. 获取窗口预期尺寸（不实际调整）
        if isinstance(window, QMessageBox):
            # 使用 sizeHint 获取合理尺寸，不调用 adjustSize
            hint = window.sizeHint()
            if hint.isValid() and hint.width() > 0 and hint.height() > 0:
                win_width, win_height = hint.width(), hint.height()
            else:
                # 兜底默认尺寸（QMessageBox 通常不会为空）
                win_width, win_height = 400, 150
        else:
            if window.width() > 0 and window.height() > 0:
                win_width, win_height = window.width(), window.height()
            else:
                hint = window.sizeHint()
                if hint.isValid() and hint.width() > 0 and hint.height() > 0:
                    win_width, win_height = hint.width(), hint.height()
                else:
                    # 对于自定义窗口，强制调整一次（不会频繁出现）
                    window.adjustSize()
                    win_width, win_height = window.width(), window.height()
                    if win_width == 0 or win_height == 0:
                        win_width, win_height = 400, 150

        # 2. 获取当前屏幕（鼠标所在显示器）
        cursor_pos = QCursor.pos()
        screen = QApplication.screenAt(cursor_pos)
        if not screen:
            screen = window.screen() if window.screen() else QApplication.primaryScreen()
        screen_geometry = screen.availableGeometry() if screen else QApplication.primaryScreen().availableGeometry()

        if screen_center:
            center = screen_geometry.center()
            x = center.x() - win_width // 2
            y = center.y() - win_height // 2
            window.move(x, y)
        else:
            if parent is None:
                parent = self
            if parent and parent.isVisible():
                parent_rect = parent.geometry()
                x = parent_rect.x() + (parent_rect.width() - win_width) // 2
                y = parent_rect.y() + (parent_rect.height() - win_height) // 2
                # 确保不超出屏幕边界
                x = max(screen_geometry.x(), min(screen_geometry.right() - win_width, x))
                y = max(screen_geometry.y(), min(screen_geometry.bottom() - win_height, y))
                window.move(x, y)
            else:
                center = screen_geometry.center()
                window.move(center.x() - win_width // 2, center.y() - win_height // 2)

    def show_player_list(self):
        """打开成员列表对话框，如果已存在则激活"""
        dlg = getattr(self, '_player_list_dialog', None)
        if dlg is not None:
            try:
                if dlg.isVisible():
                    dlg.raise_()
                    dlg.activateWindow()
                    return
            except RuntimeError:
                pass
            # 已关闭但引用未清（例如点窗口 X 关闭不会触发 finished）：丢弃后重建
            self._player_list_dialog = None

        # 请求最新玩家列表
        self.mp_client.request_player_list()
        # 创建对话框（初始为空，稍后信号更新）
        dlg = PlayerListDialog(self, players=[])
        self._player_list_dialog = dlg
        dlg.finished.connect(self._on_player_list_closed)
        dlg.open()

    def _on_player_list_closed(self):
        """对话框关闭时清理引用"""
        self._player_list_dialog = None

    def _restart_app(self):
        """保存当前状态并重启程序（保留用户登录状态）"""
        # 保存当前状态和位置
        if self.current_user:
            self.current_user["last_state"] = self.persistent_state
            pos = self.pos()
            self.current_user["last_x"] = pos.x()
            self.current_user["last_y"] = pos.y()
            self._save_current_user()
        # 断开服务器（可选）
        self.disconnect_server()
        # 获取重启命令
        if getattr(sys, 'frozen', False):
            program = sys.executable
            args = []
        else:
            program = sys.executable
            args = [os.path.abspath(sys.argv[0])]
        if sys.platform == 'win32':
            subprocess.Popen([program] + args, env=_spawn_env(),
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
        else:
            subprocess.Popen([program] + args, env=_spawn_env(), start_new_session=True)
        _hard_exit(0)

    def notify_inventory_updated(self):
        self.inventory_updated.emit()
        try:
            self.push_gacha_data()
        except Exception:
            pass

    def _resolve_local_item_id(self, raw):
        """把命令里写的物品 id 解析成规范 id（大小写不敏感；解析不了返回 None）。

        以前客户端控制台是 `parts[2].lower()` 后精确匹配 → 自定义物品 id 只要含大写字母
        （例如 Oneiric_Shard）就永远弹"未知物品ID"。
        现在：自定义物品精确匹配优先 → 内置名/别名（ticket、star_rail_ticket、ruyi…）→
        时装（忽略大小写）→ 自定义物品忽略大小写。
        """
        if raw is None:
            return None
        try:
            txt = str(raw).strip()
        except Exception:
            return None
        if not txt:
            return None
        low = txt.lower()
        alias = {'ruyi': '26_Newyear', 'ticket': 'ticket',
                 'star_rail_ticket': 'ticket', 'star_rail_tickets': 'star_rail_tickets',
                 'cake': 'cake', 'credit': 'credit', 'jade': 'jade', 'none': 'none'}
        try:
            _ci = self.server.load_custom_items() if hasattr(self, 'server') else []
        except Exception:
            _ci = []
        for it in (_ci or []):
            if it.get('id') == txt:
                return txt
        if low in alias:
            return alias[low]
        for cid in AVAILABLE_CLOTHES:
            if cid.lower() == low:
                return cid
        for it in (_ci or []):
            if str(it.get('id') or '').lower() == low:
                return it.get('id')
        return None

    def _is_clothes_like(self, item_id):
        """是否"时装类"（数量应固定为 1）：内置时装或自定义时装"""
        try:
            if item_id in AVAILABLE_CLOTHES:
                return True
            _ci = self.server.load_custom_items() if hasattr(self, 'server') else []
            for it in (_ci or []):
                if it.get('id') == item_id:
                    return (it.get('type') or 'item') == 'clothes'
        except Exception:
            pass
        return False

    def _get_remaining_text(self, expiry):
        """根据 expiry ISO 字符串返回剩余时间显示文本"""
        dt = resolve_expiry_dt(expiry)
        if dt is None:
            return ""
        try:
            now = datetime.now(dt.tzinfo)
            if now >= dt:
                return " (已过期)"
            delta = dt - now
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            mins, secs = divmod(rem, 60)
            if days > 0:
                return f" (剩余 {days}天{hours}小时)"
            elif hours > 0:
                return f" (剩余 {hours}小时{mins}分)"
            else:
                if mins > 0:
                    return f" (剩余 {mins}分{secs}秒)"
                else:
                    return f" (剩余 {secs}秒)"
        except:
            return ""

    def _close_gacha_window(self):
        """关闭抽卡窗口并清理桥接引用"""
        if hasattr(self, 'gacha_view') and self.gacha_view:
            try:
                self.gacha_view.close()
                self.gacha_view.deleteLater()  # 确保释放
                self.gacha_view = None
            except Exception as e:
                print(f"[Gacha] 关闭窗口异常: {e}")
        if hasattr(self, '_current_gacha_bridge'):
            self._current_gacha_bridge = None
        if hasattr(self, '_gacha_channel'):
            self._gacha_channel = None

    def _sync_shop_images_local(self):
        """从主窗口（Firefly）角度触发商店资源同步，并刷新已打开的商店"""
        try:
            helper = getattr(self, 'shop_window', None)
            if helper is not None:
                helper.sync_shop_images()
            else:
                # 没有商店窗口时用临时实例调用（纯文件操作）
                tmp = ShopMainWindow.__new__(ShopMainWindow)
                tmp.firefly = self
                ShopMainWindow.sync_shop_images(tmp)
        except Exception as e:
            print(f"[商店资源] 同步入口失败: {e}")
        try:
            self.sync_custom_item_images()      # 物品图也一起预取（背包要用）
        except Exception:
            pass
        try:
            if getattr(self, 'shop_window', None) is not None:
                self.shop_window.reload_server_goods()
        except Exception:
            pass

    def push_gacha_data(self):
        """把最新余额/保底推给所有已打开的卡池页面（页面不会显示旧数据）"""
        views = getattr(self, '_gacha_webviews', None) or {}
        if not views:
            return
        inv = self.online_inventory if isinstance(self.online_inventory, dict) else {}
        if not inv:
            inv = self.current_user if isinstance(self.current_user, dict) else {}
        _pity_map = getattr(self, 'gacha_pity', None) or {}
        payload = {
            'tickets': int(inv.get('star_rail_tickets', 0) or 0),
            'jade': int(inv.get('jade', 0) or 0),
            'pities': _pity_map,
            # 库存快照：右上角物品按绑定的物品 id 显示真实剩余量（含自定义物品）
            'inventory': dict(inv),
        }
        js = 'window.updateGachaData && window.updateGachaData(' + \
            json.dumps(payload, ensure_ascii=False) + ')'
        for mid, view in list(views.items()):
            try:
                if view is not None:
                    view.page().runJavaScript(js)
            except Exception as e:
                print(f"[卡池] 推送数据失败 {mid}: {e}")

    def _local_banner_name(self, menu_id, pool):
        """该卡池封面在本地页面目录里的**带版本号文件名**（'img/banner__<pid>__<hash>.png'）。

        没有它就只能用固定路径，换封面后会被浏览器缓存住 → 热更新看不到新封面。
        """
        try:
            pid = (pool or {}).get('id')
            banner = str((pool or {}).get('banner') or '')
            if not pid or not banner.startswith('/gacha_images/'):
                return ''
            _h = hashlib.md5(banner.encode('utf-8')).hexdigest()[:8]
            name = f'banner__{pid}__{_h}.png'
            p = os.path.join(self.server_resource_dir, menu_id, 'imgs', name)
            return ('imgs/' + name) if os.path.exists(p) else ''
        except Exception:
            return ''

    def _gacha_pools_args(self, menu_id, include_unpublished=False):
        """算出要注入页面的卡池参数：(pools, active, menu_id, known_items)

        include_unpublished=True（管理员编辑模式）时，未公开的卡池也一起下发，
        否则管理员保存后新卡池会从自己页面上消失（玩家端始终只拿已公开的池）。
        """
        cfg = dict(getattr(self, 'gacha_config', {}) or {})
        pools = []
        for _p in (cfg.get('pools') or []):
            if not isinstance(_p, dict):
                continue
            _mid = _p.get('menu_id') or ''
            if _mid and menu_id and _mid != menu_id:
                continue
            if not _p.get('published') and not include_unpublished:
                continue  # 未公开卡池不下发到玩家页面
            _pp = dict(_p)
            for _k in ('five_star_items', 'four_star_items', 'three_star_items'):
                _pp.pop(_k, None)
            # 带上"本地带版本号的封面文件名"：换封面热更新后必须换 URL 才能绕过缓存
            _lb = ''
            try:
                _lb = self._local_banner_name(menu_id, _pp) or ''
            except Exception:
                _lb = ''
            if _lb:
                _pp['_local_banner'] = _lb
            pools.append(_pp)
        active = cfg.get('active_pool') or ''
        if pools and active not in [x.get('id') for x in pools]:
            active = pools[0].get('id')
        known = []
        for _it in (getattr(self, 'server_custom_items', None) or []):
            if isinstance(_it, dict) and _it.get('id'):
                known.append({'id': _it.get('id'), 'name': _it.get('name') or _it.get('id'),
                              'image': _it.get('image') or '',
                              'gacha_image': _it.get('gacha_image') or ''})
        return pools, active, (menu_id or ''), known

    def _gacha_pools_js(self, menu_id):
        """构造注入卡池列表的 JS：按归属页面过滤，并剥掉物品池（不下发到页面）"""
        pools, active, mid, known = self._gacha_pools_args(menu_id)
        return ('window.setPools && window.setPools(' +
                json.dumps(pools, ensure_ascii=False) + ',' +
                json.dumps(active, ensure_ascii=False) + ',' +
                json.dumps(mid, ensure_ascii=False) + ',' +
                json.dumps(known, ensure_ascii=False) + ')')

    def _inject_page_js(self, menu_id, editable=False):
        """一次性注入页面所需的全部数据（元信息 + 卡池列表 + 编辑模式）。

        带就绪重试：页面脚本可能比 loadFinished 稍晚才定义 setPools/applyPoolMeta，
        旧写法 `window.setPools && window.setPools(...)` 在未就绪时会**静默什么都不做**，
        结果卡池永远注入不进去 —— 表现为"公开了还是显示无卡池、还挂着旧封面、按钮文案改不动"。
        """
        meta = dict(getattr(self, 'gacha_config', {}) or {})
        for _k in ('pools', 'rules', 'five_star_items', 'four_star_items', 'three_star_items'):
            meta.pop(_k, None)
        # 管理员编辑模式：未公开的卡池也要注入（否则保存后新卡池从自己页面上消失）
        pools, active, mid, known = self._gacha_pools_args(menu_id,
                                                            include_unpublished=bool(editable))
        # 顶层 meta 也要带上当前卡池的本地封面名（applyPoolMeta 先按顶层套一次）
        for _p in pools:
            if _p.get('id') == active and _p.get('_local_banner'):
                meta['_local_banner'] = _p['_local_banner']
                break
        return (
            '(function(){var n=0;'
            'function ready(){return typeof window.applyPoolMeta==="function"'
            '&&typeof window.setPools==="function"&&typeof window.setEditMode==="function";}'
            'function go(){'
            'if(ready()){'
            'try{window.applyPoolMeta(' + json.dumps(meta, ensure_ascii=False) + ');}catch(e){}'
            'try{window.setPools(' + json.dumps(pools, ensure_ascii=False) + ','
            + json.dumps(active, ensure_ascii=False) + ','
            + json.dumps(mid, ensure_ascii=False) + ','
            + json.dumps(known, ensure_ascii=False) + ');}catch(e){}'
            'try{window.setEditMode(' + ('true' if editable else 'false') + ','
            + json.dumps(mid, ensure_ascii=False) + ','
            + ('true' if editable else 'false') + ');}catch(e){}'
            'return;}'
            'if(++n<40)setTimeout(go,100);}go();})()')

    def on_gacha_config_save_result(self, data):
        """页面内保存卡池的结果 → 回推页面"""
        ok = bool(data.get('success'))
        views = getattr(self, '_gacha_webviews', None) or {}
        js = ('window.onConfigSaved && window.onConfigSaved(' +
              ('true' if ok else 'false') + ',' +
              json.dumps(data.get('reason') or '', ensure_ascii=False) + ')')
        for _mid, _v in list(views.items()):
            try:
                if _v is not None:
                    _v.page().runJavaScript(js)
            except Exception:
                pass
        if ok:
            print("[卡池] 页面编辑保存成功（已热更新）")

    def on_gacha_image_result(self, data):
        """页面上传图片的结果 → 回推页面"""
        views = getattr(self, '_gacha_webviews', None) or {}
        js = ('window.onImageUploaded && window.onImageUploaded(' +
              json.dumps(data.get('pool_id') or '', ensure_ascii=False) + ',' +
              json.dumps(data.get('kind') or '', ensure_ascii=False) + ',' +
              json.dumps(data.get('url') or '', ensure_ascii=False) + ',' +
              ('true' if data.get('success') else 'false') + ',' +
              json.dumps(data.get('reason') or '', ensure_ascii=False) + ')')
        for _mid, _v in list(views.items()):
            try:
                if _v is not None:
                    _v.page().runJavaScript(js)
            except Exception:
                pass

    def _refresh_open_gacha_views(self):
        """卡池配置变更时：先同步各池封面/物品图到页面目录（按 URL 变化覆盖），
        再给已打开的卡池页面重新注入元信息——保证切换卡池时封面互相独立且为最新"""
        views = getattr(self, '_gacha_webviews', None)
        if not views:
            return
        for _mid in list(views.keys()):
            try:
                self._sync_item_images(_mid)
            except Exception as e:
                print(f"[卡池] 封面同步失败 {_mid}: {e}")
        meta = dict(getattr(self, 'gacha_config', {}) or {})
        for k in ('rules', 'five_star_items', 'four_star_items', 'three_star_items'):
            meta.pop(k, None)
        js_meta = 'window.applyPoolMeta && window.applyPoolMeta(' + json.dumps(meta, ensure_ascii=False) + ')'
        for mid, view in list(views.items()):
            try:
                if view is not None:
                    _mc = getattr(self, 'mp_client', None)
                    _cu = getattr(self, 'current_user', None) or {}
                    _editable = bool(getattr(_mc, 'connected', False) and
                                     _cu.get('permission') == 'admin')
                    # 一次性注入（含 meta + 卡池列表 + 本地封面名），不再单独发一次 applyPoolMeta
                    view.page().runJavaScript(self._inject_page_js(mid, _editable))
            except Exception as e:
                print(f"[卡池] 刷新已打开页面失败 {mid}: {e}")

    def _find_resource_menu_item(self, menu_id):
        """在自定义菜单树里按 id 找到需要资源的菜单项"""
        def walk(items):
            for it in items or []:
                if it.get('id') == menu_id:
                    return it
                found = walk(it.get('children'))
                if found:
                    return found
            return None
        try:
            return walk(self.server_custom_menu)
        except Exception:
            return None

    def _push_gacha_notice(self, text, kind='info', progress=None):
        """在卡池页面内显示提示条（替代 Qt 弹窗）"""
        views = getattr(self, '_gacha_webviews', None) or {}
        if not views:
            if kind != 'progress':
                show_notification_async("卡池", text, duration=4)
            return
        js = ('window.gachaNotice && window.gachaNotice(' +
              json.dumps(text, ensure_ascii=False) + ',' +
              json.dumps(kind, ensure_ascii=False) + ',' +
              ('null' if progress is None else str(int(progress))) + ')')
        for mid, view in list(views.items()):
            try:
                if view is not None:
                    view.page().runJavaScript(js)
            except Exception:
                pass

    def _sync_outdated_resources(self, sigs):
        """对比服务端资源包签名，本地过期/缺失就静默重下（保证新页面/新封面必达）"""
        if not sigs:
            return
        updated = 0
        for mid, sig in (sigs or {}).items():
            if not mid or not sig:
                continue
            sig_path = os.path.join(self.server_resource_dir, f'{mid}.sig')
            local = ''
            if os.path.exists(sig_path):
                try:
                    with open(sig_path, 'r', encoding='utf-8') as f:
                        local = f.read().strip()
                except Exception:
                    local = ''
            if local == sig:
                continue
            item = self._find_resource_menu_item(mid)
            if not item:
                continue
            # 记录新签名（下载失败下次登录会再重试）
            try:
                os.makedirs(self.server_resource_dir, exist_ok=True)
                with open(sig_path, 'w', encoding='utf-8') as f:
                    f.write(sig)
            except Exception:
                pass
            self._silent_sync_pack(mid, item)
            updated += 1
        if updated:
            print(f"[资源握手] {updated} 个资源包已过期，正在后台更新")

    def _silent_sync_pack(self, menu_id, item):
        """后台静默下载并解压资源包（不弹任何 Qt 窗口，进度显示在页面内）"""
        url = item.get('resource_url') or ''
        if url.startswith('/'):
            host = getattr(self, 'server_host', '127.0.0.1')
            port = getattr(self, 'server_web_port', 8080)
            url = f"http://{host}:{port}{url}"
        if not url:
            self._push_gacha_notice('❌ 资源地址无效，无法同步', 'error')
            return
        zip_path = os.path.join(self.server_resource_dir, f'{menu_id}.zip')
        extract_to = os.path.join(self.server_resource_dir, menu_id)
        self._push_gacha_notice('正在同步卡池资源…', 'progress', 0)
        try:
            worker = _GachaPackSync(url, zip_path, extract_to, self,
                                    key=pack_key(menu_id, self._pack_secret()))
        except Exception as e:
            self._push_gacha_notice(f'❌ 同步失败：{e}', 'error')
            return

        def _on_progress(p):
            self._push_gacha_notice('正在同步卡池资源…', 'progress', p)

        def _on_done(ok, msg):
            self._push_gacha_notice(('✅ ' if ok else '❌ ') + msg,
                                    'info' if ok else 'error')
            if ok:
                for _v in (getattr(self, '_gacha_webviews', None) or {}).values():
                    try:
                        _v.reload()
                    except Exception:
                        pass
                try:
                    self.create_menu()
                except Exception:
                    pass

        worker.progress.connect(_on_progress)
        worker.finished_ok.connect(_on_done)
        worker.start()
        self._gacha_sync_worker = worker  # 持有引用，避免被回收

    def on_gacha_resource_updated(self, menu_id):
        """服务端卡池资源包（如封面）变更：失效本地缓存；若正在浏览则重下并刷新"""
        if not menu_id:
            return
        pack_dir = os.path.join(self.server_resource_dir, menu_id)
        zip_path = os.path.join(self.server_resource_dir, f'{menu_id}.zip')
        view = (getattr(self, '_gacha_webviews', None) or {}).get(menu_id)
        try:
            if os.path.isdir(pack_dir):
                shutil.rmtree(pack_dir, ignore_errors=True)
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception as e:
            print(f"[卡池] 清理旧资源失败: {e}")
        item = self._find_resource_menu_item(menu_id)
        if not item:
            self._push_gacha_notice('卡池资源已更新，重新打开卡池生效', 'info')
            return
        if view is None:
            self._push_gacha_notice('卡池资源已更新，正在后台同步…', 'progress', 0)
        self._silent_sync_pack(menu_id, item)

    def _load_auto_download_pref(self):
        """从本地文件读取自动下载偏好"""
        pref_file = os.path.join(USER_DATA_DIR, "auto_download.json")
        if os.path.exists(pref_file):
            try:
                with open(pref_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._auto_download_resources = data.get('enabled', False)
            except:
                pass

    def _save_auto_download_pref(self):
        """保存自动下载偏好到本地文件"""
        pref_file = os.path.join(USER_DATA_DIR, "auto_download.json")
        try:
            with open(pref_file, 'w', encoding='utf-8') as f:
                json.dump({'enabled': self._auto_download_resources}, f)
        except:
            pass

    def _gacha_base_dir(self):
        """内置卡池基底（index.html + mp4 + 默认 img）：随客户端打包，无需服务端推送"""
        base = os.path.join(get_base_dir(), 'gacha_base')
        if os.path.isdir(base):
            return base
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            alt = os.path.join(sys._MEIPASS, 'gacha_base')
            if os.path.isdir(alt):
                return alt
        return None

    def _gacha_log(self, note):
        """卡池相关诊断日志：写入 gacha_debug.log（排查"公开了却显示无卡池"这类问题）"""
        try:
            p = os.path.join(get_writable_dir(), 'gacha_debug.log')
            with open(p, 'a', encoding='utf-8') as f:
                f.write("[%s] %s%s" % (datetime.now().strftime('%H:%M:%S'), note, chr(10)))
        except Exception:
            pass


    # ==================== 资源包完整性：服务端清单 + 自动修复 ====================
    def _pack_secret(self):
        return getattr(self, 'pack_secret', '') or ''

    def _http_get(self, rel, timeout=8):
        """从服务端 Web 取一个小文件（清单用）；失败返回 None"""
        url = self._custom_server_url(rel)
        if not url or not self._web_port_alive():
            return None
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FireflyPet'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            print(f"[完整性] 取 {rel} 失败: {e}")
            return None

    def _fetch_pack_manifest(self, menu_id):
        """取服务端清单并验签；通过返回 {相对路径: sha256}，否则 None。

        清单形如 {"files": {...}, "sig": HMAC}：先验签（别人改了包/清单都过不了），
        再拿它去校验本地解压出来的文件。
        """
        if not menu_id:
            return None
        raw = self._http_get(f'/resources/{menu_id}.manifest')
        if not raw:
            return None
        try:
            data = json.loads(raw.decode('utf-8', 'ignore') or '{}')
        except Exception as e:
            print(f"[完整性] 清单解析失败: {e}")
            return None
        files = data.get('files')
        sig = str(data.get('sig') or '')
        if not isinstance(files, dict) or not files:
            return None
        try:
            want = pack_manifest_sig(files, pack_key(menu_id, self._pack_secret()))
        except Exception as e:
            print(f"[完整性] 清单验签异常: {e}")
            return None
        if sig != want:
            print(f"[完整性] {menu_id} 清单签名不匹配（可能被中间人改动），忽略该清单")
            return None
        return files

    def _verify_page_files(self, page_dir, expect):
        """逐个校验本地文件：返回问题列表（missing: / tampered: / extra:）"""
        problems = []
        if not isinstance(expect, dict) or not expect:
            return problems
        for rel, want in expect.items():
            rel_os = str(rel).replace('/', os.sep)
            p = os.path.join(page_dir, rel_os)
            if not os.path.exists(p):
                problems.append('missing:' + str(rel))
                continue
            try:
                with open(p, 'rb') as f:
                    got = hashlib.sha256(f.read()).hexdigest()
            except Exception:
                problems.append('unreadable:' + str(rel))
                continue
            if got != want:
                problems.append('tampered:' + str(rel))
        local = dir_file_hashes(page_dir)
        for rel in local:
            if rel not in expect:
                problems.append('extra:' + rel)
        return problems

    def _local_page_manifest(self, page_dir):
        """本地清单（服务端没有包时用；用于发现本地被改动/丢失）"""
        try:
            p = os.path.join(page_dir, '.integrity.json')
            if os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as f:
                    return (json.load(f) or {}).get('files') or {}
        except Exception:
            pass
        return {}

    def _write_local_page_manifest(self, page_dir):
        try:
            with open(os.path.join(page_dir, '.integrity.json'), 'w', encoding='utf-8') as f:
                json.dump({'files': dir_file_hashes(page_dir), 'src': 'base'},
                          f, ensure_ascii=False)
        except Exception as e:
            print(f"[完整性] 写本地清单失败: {e}")

    def _repair_page_files(self, menu_id, page_dir, problems):
        """修复被改动/丢失的页面文件：加密包重解 → 内置基底重铺。返回是否修好。"""
        print(f"[完整性] {menu_id} 发现 {len(problems)} 处异常，开始修复：{problems[:5]}")
        # ① 本地还有加密资源包（修复源）→ 解密重解
        zp = os.path.join(self.server_resource_dir, f'{menu_id}.zip')
        if os.path.exists(zp):
            try:
                with open(zp, 'rb') as f:
                    blob = f.read()
                key = pack_key(menu_id, self._pack_secret())
                plain = pack_decrypt(blob, key) if blob.startswith(PACK_MAGIC) else blob
                if plain.startswith(b'PK'):
                    for _p in list(problems):
                        _rel = _p.split(':', 1)[1].replace('/', os.sep)
                        try:
                            os.remove(os.path.join(page_dir, _rel))
                        except Exception:
                            pass
                    safe_extract_zip(plain, page_dir)
                    self._write_local_page_manifest(page_dir)
                    print(f"[完整性] {menu_id} 已用本地资源包恢复")
                    return True
            except Exception as e:
                print(f"[完整性] 用本地包恢复失败: {e}")
        # ② 用内置基底重新铺开（删掉签名强制重铺）
        try:
            sig_path = os.path.join(page_dir, '.gacha_base_sig')
            if os.path.exists(sig_path):
                os.remove(sig_path)
            if self._ensure_gacha_base(menu_id):
                # 顺手清掉"多出来的文件"（被注入的脚本等）：以刚写好的清单为准
                try:
                    man = self._local_page_manifest(page_dir)
                    if man:
                        for rel in list(dir_file_hashes(page_dir).keys()):
                            if rel not in man:
                                try:
                                    os.remove(os.path.join(page_dir, rel.replace('/', os.sep)))
                                    print(f"[完整性] 已清除多余文件: {rel}")
                                except Exception:
                                    pass
                except Exception as _pe:
                    print(f"[完整性] 清除多余文件失败: {_pe}")
                self._write_local_page_manifest(page_dir)
                print(f"[完整性] {menu_id} 已用内置基底恢复")
                return True
        except Exception as e:
            print(f"[完整性] 用内置基底恢复失败: {e}")
        return False

    def verify_and_repair_page(self, menu_id):
        """打开页面之前调用：校验本地页面完整性，必要时自动修复。返回 (ok, 说明)"""
        try:
            page_dir = os.path.join(self.server_resource_dir, menu_id)
            if not os.path.isdir(page_dir):
                return True, ''
            # 服务端清单优先（权威，且验签过）；没有就退回本地清单
            expect = self._fetch_pack_manifest(menu_id)
            source = '服务端'
            if not expect:
                expect = self._local_page_manifest(page_dir)
                source = '本地'
            if not expect:
                return True, ''
            problems = self._verify_page_files(page_dir, expect)
            if not problems:
                return True, ''
            ok = self._repair_page_files(menu_id, page_dir, problems)
            # 修复后再校验一次（本地清单此时已刷新）
            expect2 = self._fetch_pack_manifest(menu_id) or self._local_page_manifest(page_dir)
            left = self._verify_page_files(page_dir, expect2) if expect2 else []
            if ok and not left:
                return True, f'本地资源被改动（{source}清单），已自动恢复'
            return (not left), '资源被改动且自动恢复失败，请点「🔧 资源修复」'
        except Exception as e:
            print(f"[完整性] 校验页面异常: {e}")
            return True, ''

    def _gacha_base_sig(self):
        """内置卡池基底的签名（内容一变就重新铺开）。用于识别"本地页面是旧版本"。"""
        try:
            src = self._gacha_base_dir()
            if not src:
                return ''
            p = os.path.join(src, 'index.html')
            if not os.path.exists(p):
                return ''
            with open(p, 'rb') as f:
                data = f.read()
            return hashlib.md5(data).hexdigest()[:16] + ':' + str(len(data))
        except Exception:
            return ''

    def _ensure_gacha_base(self, menu_id):
        """确保卡池页面本地目录与内置基底一致：
        - 目录/页面缺失 → 从内置基底生成；
        - 内置基底已更新（签名与本地记录不同）→ 重新铺开 index.html/mp4/基底自带的 img，
          **保留**已下载的按物品 id / 卡池 id 命名的图片（img/<id>.png、img/banner__<pid>.png）。

        历史上这里是"index.html 存在就直接返回"，导致老用户的页面永远是第一次生成的旧版：
        公开卡池后仍显示旧静态内容、上传的封面不显示、按钮文案改了没反应 —— 全是这个原因。
        """
        page_dir = os.path.join(self.server_resource_dir, menu_id)
        index_path = os.path.join(page_dir, 'index.html')
        src = self._gacha_base_dir()
        sig = self._gacha_base_sig()
        sig_path = os.path.join(page_dir, '.gacha_base_sig')
        local_sig = ''
        try:
            if os.path.exists(sig_path):
                with open(sig_path, 'r', encoding='utf-8') as f:
                    local_sig = f.read().strip()
        except Exception:
            local_sig = ''
        if os.path.exists(index_path) and sig and local_sig == sig:
            return True                      # 已是最新
        if not src:
            return os.path.exists(index_path)
        existed = os.path.exists(index_path)
        try:
            os.makedirs(page_dir, exist_ok=True)
            for root, dirs, files in os.walk(src):
                rel = os.path.relpath(root, src)
                target_root = page_dir if rel == '.' else os.path.join(page_dir, rel)
                os.makedirs(target_root, exist_ok=True)
                for fn in files:
                    shutil.copy2(os.path.join(root, fn), os.path.join(target_root, fn))
            # 旧布局遗留目录（img/ 与 mp4/ 已改名为 imgs/ 与 videos/）：刷新时清掉，
            # 避免旧图与旧视频一直占着且被误当作当前资源
            if existed:
                for _legacy in ('img', 'mp4'):
                    _lp = os.path.join(page_dir, _legacy)
                    if os.path.isdir(_lp):
                        try:
                            shutil.rmtree(_lp, ignore_errors=True)
                            print(f"[卡池基底] 已清理旧目录 {menu_id}/{_legacy}")
                        except Exception:
                            pass
            if sig:
                with open(sig_path, 'w', encoding='utf-8') as f:
                    f.write(sig)
            # 记一份本地清单：之后发现本地被改动/丢失时，靠它 + 内置基底自动恢复
            try:
                self._write_local_page_manifest(page_dir)
            except Exception:
                pass
            print(f"[卡池基底] 已{'更新' if existed else '生成'} {menu_id} 的本地页面")
            return os.path.exists(index_path)
        except Exception as e:
            print(f"[卡池基底] 生成失败: {e}")
            return os.path.exists(index_path)

    def _sync_item_images(self, menu_id):
        """把自定义物品的 抽到展示图/物品图片 下载进卡池页面目录 img/<id>.png
        （结果展示与右上角图标按物品 id 自动匹配；物品 ver 变化时强制刷新）。
        同时把各卡池封面按 URL 同步为 img/banner__<poolid>.png（切换卡池封面跟随）。"""
        img_dir = os.path.join(self.server_resource_dir, menu_id, 'imgs')
        # ---- 卡池封面（按 URL 变化覆盖，天然支持同位置换图）----
        try:
            cfg = dict(getattr(self, 'gacha_config', {}) or {})
            for pool in (cfg.get('pools') or []):
                if not isinstance(pool, dict):
                    continue
                pid = pool.get('id')
                banner = str(pool.get('banner') or '')
                if not pid or not banner.startswith('/gacha_images/'):
                    continue
                # 用**带版本号的文件名**：固定路径换图会被浏览器缓存住，
                # 热更新换了封面却看不到变化（这是"改了封面热更新没效果"的真因）
                _h = hashlib.md5(banner.encode('utf-8')).hexdigest()[:8]
                dest = os.path.join(img_dir, f'banner__{pid}__{_h}.png')
                sig_file = os.path.join(img_dir, f'banner__{pid}.sig')
                cur = ''
                if os.path.exists(sig_file):
                    try:
                        with open(sig_file, 'r', encoding='utf-8') as f:
                            cur = f.read().strip()
                    except Exception:
                        cur = ''
                if os.path.exists(dest) and cur == banner:
                    continue
                try:
                    os.makedirs(img_dir, exist_ok=True)
                    # 清掉该池旧版本的封面文件，避免越攒越多
                    _base = os.path.basename(dest)
                    for _fn in os.listdir(img_dir):
                        if _fn.startswith(f'banner__{pid}__') and _fn.endswith('.png') \
                                and _fn != _base:
                            try:
                                os.remove(os.path.join(img_dir, _fn))
                            except Exception:
                                pass
                    if self._download_custom_asset(banner, dest):
                        with open(sig_file, 'w', encoding='utf-8') as f:
                            f.write(banner)
                except Exception as e:
                    print(f"[卡池封面] 同步失败 {pid}: {e}")
        except Exception as e:
            print(f"[卡池封面] 同步异常: {e}")
        # ---- 自定义物品图片 ----
        items = getattr(self, 'server_custom_items', None) or []
        if not items:
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = it.get('id')
            if not iid:
                continue
            for key in ('gacha_image', 'image'):
                rel = it.get(key)
                if not rel:
                    continue
                dest = os.path.join(img_dir, f'{iid}.png')
                ver_file = os.path.join(img_dir, f'{iid}.{key}.ver')
                ver = str(it.get('ver') or '')
                cur = ''
                if os.path.exists(ver_file):
                    try:
                        with open(ver_file, 'r', encoding='utf-8') as f:
                            cur = f.read().strip()
                    except Exception:
                        cur = ''
                if os.path.exists(dest) and cur == ver:
                    continue
                try:
                    os.makedirs(img_dir, exist_ok=True)
                    if os.path.exists(dest):
                        os.remove(dest)
                    if self._download_custom_asset(rel, dest):
                        with open(ver_file, 'w', encoding='utf-8') as f:
                            f.write(ver)
                except Exception as e:
                    print(f"[物品图片] 下载失败 {iid}:{key}: {e}")

    def _resource_exists(self, menu_id):
        """检查指定菜单项的资源是否已下载（存在同名文件夹或文件）"""
        if not menu_id:
            return False
        path = os.path.join(self.server_resource_dir, menu_id)
        return os.path.exists(path)

    def _mark_resource_downloaded(self, menu_id):
        """标记资源为已下载（创建文件夹作为标记）"""
        if not menu_id:
            return
        path = os.path.join(self.server_resource_dir, menu_id)
        os.makedirs(path, exist_ok=True)

    class _NullProgress:
        """静默下载用的假进度条：所有调用都吞掉，不创建任何窗口"""

        def __init__(self, *a, **k):
            pass

        def setWindowModality(self, *a):
            pass

        def setMinimumDuration(self, *a):
            pass

        def move(self, *a):
            pass

        def setStyleSheet(self, *a):
            pass

        def setLabelText(self, *a):
            pass

        def setRange(self, *a):
            pass

        def setValue(self, *a):
            pass

        def close(self):
            pass

        def wasCanceled(self):
            return False

        def width(self):
            return 0

        def height(self):
            return 0

    def _resource_notice(self, text, kind='info', progress=None):
        """资源下载的提示：卡池页面开着就在页面里提示，否则用托盘通知"""
        try:
            views = getattr(self, '_gacha_webviews', None) or {}
            if views:
                self._push_gacha_notice(text, kind, progress)
                return
        except Exception:
            pass
        if kind != 'progress':
            show_notification_async("资源包", text, duration=4)

    def download_resource(self, menu_item, silent=False):
        url = menu_item.get('resource_url')
        if not url:
            return
        menu_id = menu_item.get('id')
        if not menu_id:
            return

        if url.startswith('/'):
            host = getattr(self, 'server_host', '127.0.0.1')
            port = getattr(self, 'server_web_port', 8080)
            full_url = f"http://{host}:{port}{url}"
        else:
            full_url = url

        local_zip = os.path.join(self.server_resource_dir, f"{menu_id}.zip")

        if silent:
            progress = self._NullProgress()
            self._resource_notice(f"正在下载资源包：{menu_item.get('label', '资源包')}",
                                  'progress', 0)
        else:
            progress = QProgressDialog(
                f"正在下载: {menu_item.get('label', '资源包')}",
                "取消",
                0,
                100,
                self
            )
            progress.setWindowModality(Qt.WindowModal)
            progress.setMinimumDuration(0)

        # 居中
        parent_geo = self.geometry()
        x = parent_geo.x() + (parent_geo.width() - progress.width()) // 2
        y = parent_geo.y() + (parent_geo.height() - progress.height()) // 2
        progress.move(x, y)

        # 深色样式
        progress.setStyleSheet("""
            QProgressDialog {
                background-color: #2D2D2D;
                color: #E0E0E0;
                border: 1px solid #555;
                border-radius: 6px;
            }
            QLabel {
                color: #E0E0E0;
                font-size: 13px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton:disabled {
                background-color: #666;
            }
            QProgressBar {
                border: 1px solid #555;
                border-radius: 4px;
                background-color: #1E1E1E;
                text-align: center;
                color: #E0E0E0;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                border-radius: 4px;
            }
        """)
        progress.show()

        class CancelException(Exception):
            pass

        def report_hook(count, block_size, total_size):
            if progress.wasCanceled():
                raise CancelException("下载已取消")
            if total_size > 0:
                percent = min(100, int(count * block_size * 100 / total_size))
                progress.setValue(percent)

        try:
            import zipfile
            import shutil

            req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp, open(local_zip, "wb") as out:
                total_size = int(resp.headers.get("Content-Length", 0) or 0)
                downloaded = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    if progress.wasCanceled():
                        raise CancelException("下载已取消")
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        progress.setValue(min(100, int(downloaded * 100 / total_size)))
                progress.setValue(100)

            progress.setLabelText("正在解压...")
            progress.setRange(0, 0)

            extract_to = os.path.join(self.server_resource_dir, menu_id)
            if os.path.exists(extract_to):
                shutil.rmtree(extract_to)
            os.makedirs(extract_to, exist_ok=True)

            # 服务端加密下发的包：先解密再内存安全解压（防路径穿越/防篡改用）
            with open(local_zip, 'rb') as _zf:
                _blob = _zf.read()
            if _blob.startswith(PACK_MAGIC):
                _plain = pack_decrypt(_blob, pack_key(menu_id, self._pack_secret()))
                if not _plain.startswith(b'PK'):
                    progress.close()
                    self.show_centered_message("下载失败", "资源包解密失败（密钥不匹配或文件被改动）",
                                               QMessageBox.Warning)
                    return
            else:
                _plain = _blob
            safe_extract_zip(_plain, extract_to)
            try:
                self._write_local_page_manifest(extract_to)
            except Exception:
                pass
            os.remove(local_zip)

            progress.setRange(0, 100)
            progress.setValue(100)

            index_path = os.path.join(extract_to, 'index.html')
            if not os.path.exists(index_path):
                if silent:
                    self._resource_notice(
                        f"❌ 资源包 {menu_item.get('label')} 缺少 index.html", 'error')
                else:
                    QMessageBox.warning(
                        self,
                        "资源包无效",
                        f"资源包 {menu_item.get('label')} 中缺少 index.html 文件，请检查资源包内容。"
                    )
                return

            if silent:
                self._resource_notice(f"✅ {menu_item.get('label')} 资源包已下载完成", 'ok')
            else:
                QMessageBox.information(
                    self,
                    "下载完成",
                    f"{menu_item.get('label')} 资源包已下载并解压"
                )
            self.create_menu()

        except CancelException:
            if os.path.exists(local_zip):
                try:
                    os.remove(local_zip)
                except:
                    pass
            if silent:
                self._resource_notice("资源包下载已取消")
            else:
                QMessageBox.information(self, "下载取消", "下载已取消。")

        except urllib.error.URLError as e:
            if silent:
                self._resource_notice(f"❌ 下载失败：网络错误 {e}", 'error')
            else:
                QMessageBox.warning(self, "下载失败", f"网络错误：{str(e)}")

        except Exception as e:
            if silent:
                self._resource_notice(f"❌ 下载失败：{e}", 'error')
            else:
                QMessageBox.warning(self, "下载失败", str(e))

        finally:
            progress.close()

    def check_missing_resources(self):
        """登录后检查是否有需要下载的资源，弹窗询问（深色，居中）"""
        missing = [item for item in self.server_custom_menu
                   if item.get('resource_required') and item.get('type') != 'gacha'
                   and not self._resource_exists(item.get('id'))]
        if not missing:
            return
        if self._auto_download_resources:
            for item in missing:
                self.download_resource(item, silent=True)
            return

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("资源下载")
        msg_box.setText(f"当前服务器 {self.server_display_name} 存在未下载的资源包。\n是否现在下载？")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.Yes)

        check_box = QCheckBox("不再提醒自动下载")
        msg_box.setCheckBox(check_box)

        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                color: #E0E0E0;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
                background-color: #1E1E1E;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton:disabled {
                background-color: #666;
            }
            QCheckBox {
                color: #E0E0E0;
                background-color: transparent;
            }
            QCheckBox::indicator {
                border: 1px solid #555;
                background-color: #2D2D2D;
            }
            QCheckBox::indicator:checked {
                background-color: #4CAF50;
            }
        """)

        parent_geo = self.geometry()
        x = parent_geo.x() + (parent_geo.width() - msg_box.width()) // 2
        y = parent_geo.y() + (parent_geo.height() - msg_box.height()) // 2
        msg_box.move(x, y)

        reply = msg_box.exec_()
        if reply == QMessageBox.Yes:
            for item in missing:
                self.download_resource(item, silent=True)
            if check_box.isChecked():
                self._auto_download_resources = True
                self._save_auto_download_pref()

    def _on_server_menu_clicked(self, item):
        # 按钮有效期：**只提示、不拦截**（用户要求"过了之后不要影响按钮的点击"）。
        # 抽卡菜单的"卡池有效期"存在卡池上，由抽卡页面按秒倒计时、到点自动下架。
        _expiry = item.get('expiry')
        if _expiry:
            try:
                _dt = resolve_expiry_dt(_expiry)
                if _dt is not None and datetime.now(_dt.tzinfo) >= _dt:
                    print(f"[菜单] {item.get('label')} 的按钮有效期已过（不影响点击）")
            except Exception:
                pass
        print(f"[菜单点击] item={item}")
        itype = item.get('type')
        data = item.get('data')
        menu_id = item.get('id', '')

        # ★ 类型缺失时直接报错
        if not itype:
            self.show_centered_message("错误", "菜单项类型无效", QMessageBox.Warning)
            return

        if itype == 'url':
            webbrowser.open(data)

        elif itype == 'gacha':
            # 卡池页面由客户端内置基底直接生成（不再推送整个资源包）
            if not self._resource_exists(menu_id):
                self._ensure_gacha_base(menu_id)
            self._open_gacha_page(menu_id)

        elif itype == 'command':
            if self.mp_client.connected:
                self.mp_client.send_command(data)
            else:
                self.show_centered_message("提示", "未连接到服务器", QMessageBox.Warning)

        elif itype == 'download':
            if not self._resource_exists(menu_id):
                self.download_resource(item, silent=True)
            else:
                self.show_centered_message("提示", "资源已下载", QMessageBox.Information)

        elif itype == 'action':
            self.show_centered_message("服务器功能", f"执行 {item.get('label')}", QMessageBox.Information)

        elif itype == 'html':
            res_path = os.path.join(self.server_resource_dir, menu_id)
            index_path = os.path.join(res_path, 'index.html')
            if os.path.exists(index_path):
                webbrowser.open(index_path)
            else:
                self.show_centered_message("错误", "资源未下载或缺少 index.html", QMessageBox.Warning)

        elif itype == 'webview':
            if data:
                from PyQt5.QtWebEngineWidgets import QWebEngineView
                from PyQt5.QtCore import QUrl
                view = QWebEngineView()
                try:
                    _p = getattr(Qt, 'PreventContextMenu', None)
                    if _p is not None:
                        view.page().setContextMenuPolicy(_p)
                except Exception:
                    pass
                view.setWindowTitle(item.get('label', '网页'))
                # 页面加载完成后注入卡池元信息（标题/封面/配色/字体/描述）

                def _inject_pool_meta(ok):

                    try:

                        if ok:

                            try:
                                _can_edit = bool(self.mp_client.connected and
                                                 (self.current_user or {}).get('permission') == 'admin')
                            except Exception:
                                _can_edit = False

                            # 元信息 + 卡池列表 + 编辑模式一次性注入（带就绪重试）
                            view.page().runJavaScript(
                                self._inject_page_js(menu_id, _can_edit))

                    except Exception:

                        traceback.print_exc()

                try:

                    view.loadFinished.connect(_inject_pool_meta)

                except Exception:

                    pass

                try:
                    if not hasattr(self, '_gacha_webviews'):
                        self._gacha_webviews = {}
                    self._gacha_webviews[menu_id] = view
                    # 页面内编辑需要知道归属菜单；是否能编辑由权限决定
                    self._current_gacha_menu_id = menu_id
                    view.destroyed.connect(
                        lambda *a, _m=menu_id: self._gacha_webviews.pop(_m, None))
                except Exception:
                    pass
                try:
                    from PyQt5.QtGui import QColor
                    view.page().setBackgroundColor(QColor("#0d1020"))   # 打开时不再白屏
                except Exception:
                    pass
                view.load(QUrl(data))
                view.resize(*fit_4_3(800, 600))
                view.show()
            else:
                self.show_centered_message("错误", "无效的URL", QMessageBox.Warning)


        elif itype == 'folder':

            # ★ 增加 menu_id 校验，防止空路径误触

            if not menu_id:
                self.show_centered_message("错误", "无效的文件夹菜单", QMessageBox.Warning)

                return

            folder = os.path.join(self.server_resource_dir, menu_id)

            if os.path.exists(folder):

                if sys.platform == 'win32':

                    os.startfile(folder)

                else:

                    subprocess.Popen(['open', folder])

            else:

                self.show_centered_message("错误", "文件夹不存在", QMessageBox.Warning)
        elif itype == 'none':
            # 「无（仅显示）」：点了就是这个菜单本身，没有动作可做 —— 不弹错误框
            print(f"[菜单点击] {item.get('label')} 是仅显示菜单，无动作")
        else:
            self.show_centered_message("提示", "未知菜单类型", QMessageBox.Warning)

    def _create_server_menu_item(self, item):
        label = item.get('label', '未知')
        icon_type = item.get('icon_type', 'emoji')
        icon_str = item.get('icon', '')
        action = QAction(label, self)
        # 短字符串一律当 emoji 前缀（兼容旧数据 icon_type=image 却存 emoji 的情况）
        if icon_str and len(str(icon_str)) <= 4 and not str(icon_str).startswith(('http', '/')):
            action.setText(f"{icon_str} {label}")
        elif icon_type == 'emoji' and icon_str:
            action.setText(f"{icon_str} {label}")
        elif icon_type == 'image' and icon_str:
            pixmap = self._load_image_from_server(icon_str)
            if pixmap:
                action.setIcon(QIcon(pixmap))
        else:
            # 默认给个图标（根据类型或标签）
            default_icon = "📦"
            if item.get('type') == 'gacha':
                default_icon = "🎲"
            elif item.get('type') == 'url':
                default_icon = "🔗"
            elif item.get('type') == 'html':
                default_icon = "🌐"
            action.setText(f"{default_icon} {label}")
        action.setToolTip(item.get('tooltip', ''))
        return action

    def _start_website_check(self):
        import threading
        def check():
            title = None
            status = False
            try:
                import urllib.request
                req = urllib.request.Request(
                    'https://blog.mar7th.cn/?ref=23550',
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.getcode() == 200:
                            status = True
                            content = resp.read().decode('utf-8', errors='ignore')
                            import re
                            match = re.search(r'<h1[^>]*>(.*?)</h1>', content, re.IGNORECASE | re.DOTALL)
                            title = match.group(1).strip() if match else "公测已延期"
                        else:
                            status = False
                            title = f"状态码 {resp.getcode()}"
                except urllib.error.HTTPError as e:
                    print(f"[网站检测] HTTPError: {e.code} {e.reason}")
                    # 重定向视为网站可用（实际浏览器能访问）
                    if e.code in (301, 302, 303, 307, 308):
                        status = True
                        title = "小三月日记"
                    else:
                        status = False
                        title = f"HTTP {e.code}"
            except Exception as e:
                print(f"[网站检测] 异常: {e}")
                status = False
                title = "异常"
            self.website_check_done.emit(status, title)

        threading.Thread(target=check, daemon=True).start()

    def _on_website_check_done(self, is_available, title):
        print(f"[网站检测] 回调主线程，结果: {is_available}, 标题: {title}")
        self._website_status = is_available
        self._website_title = title
        # 仅在单机模式下刷新菜单（联机模式下该菜单项已被隐藏）
        if self.tray_icon and not self.mp_client.connected:
            self.create_menu()

    def _on_check_name_result(self, available, reason):
        # 优先处理强制改名对话框
        if self._rename_dialog and self._rename_dialog.isVisible():
            self._rename_status_label.setText(reason if not available else "昵称可用")
            self._rename_status_label.setStyleSheet("color: #FF5555;" if not available else "color: #00FF00;")
            self._rename_ok_btn.setEnabled(available)
        elif self._pending_rename_dialog and self._pending_rename_dialog.isVisible():
            self._pending_rename_dialog.on_check_name_result(available, reason)

    def is_valid_nickname(name):
        import unicodedata
        stripped = name.strip()
        if not stripped:
            return False
        # 检查不可见字符
        for ch in stripped:
            if unicodedata.category(ch).startswith('C'):  # Cc, Cf, Cn, Co, Cs
                return False
            if ord(ch) < 0x20:  # 控制字符
                return False
        # 检查是否以“开拓者”开头且不等于“开拓者”
        if stripped.startswith("开拓者") and stripped != "开拓者":
            return False
        # 不允许冒充已注销账号的显示名
        if stripped.startswith("账号已注销"):
            return False
        # 只允许中英文、数字、下划线（长度1-16）
        if not re.match(r'^[\w\u4e00-\u9fff]{1,16}$', stripped):
            return False
        return True

    def on_common_mute_status(self, enabled):
        self.common_mute = enabled
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.update_common_mute_status(enabled)

    def on_chat_status(self, enabled):
        """处理聊天功能状态变更"""
        self.chat_enabled = enabled
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.update_chat_status(enabled)

    # ---------- 注销账号 ----------
    def _destroy_phrase_ok(self, text):
        """注销确认短语判定（宽松）：接受中英文两套写法，忽略大小写与所有空白"""
        t = ''.join((text or '').split())
        if t in ('我确定注销并了解结果', '我确认'):
            return True
        tl = t.lower().rstrip('.。')
        return tl in ('iconfirmthecancellationandacknowledgetheconsequences',
                      'iconfirm')

    def _destroy_remaining_text(self):
        until = getattr(self, 'destroy_pending_until', None)
        if not until:
            return '00:00'
        try:
            delta = until - datetime.now(until.tzinfo)
        except Exception:
            return '00:00'
        secs = max(0, int(delta.total_seconds()))
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        if days > 0:
            return f"{days}天{hours:02d}:{minutes:02d}"
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def start_destroy_countdown(self):
        """注销冷却期：每秒刷新菜单按钮上的剩余时间"""
        if getattr(self, '_destroy_timer', None) is None:
            self._destroy_timer = QTimer(self)
            self._destroy_timer.timeout.connect(self._destroy_tick)
        if not self.destroy_pending_until:
            return
        self._destroy_timer.start(1000)

    def stop_destroy_countdown(self):
        t = getattr(self, '_destroy_timer', None)
        if t:
            t.stop()

    def _destroy_tick(self):
        until = getattr(self, 'destroy_pending_until', None)
        # 退出/注销流程中 current_user 已清空：立即停表，避免访问空用户
        if not until or not self.current_user:
            self.stop_destroy_countdown()
            return
        try:
            if datetime.now(until.tzinfo) >= until:
                # 冷却结束由服务端自动销号并踢出；本地停止倒计时
                self.stop_destroy_countdown()
                return
        except Exception:
            self.stop_destroy_countdown()
            return
        # 刷新频率自适应：剩余 >1 小时每 30 秒、>1 分钟每 5 秒、否则每秒
        try:
            remain_s = (until - datetime.now(until.tzinfo)).total_seconds()
            want = 1000 if remain_s <= 60 else (5000 if remain_s <= 3600 else 30000)
            if self._destroy_timer.interval() != want:
                self._destroy_timer.setInterval(want)
        except Exception:
            pass
        # 动态刷新：只改菜单项文字（菜单开着也能实时看到倒计时），不重建整个菜单
        act = getattr(self, '_destroy_action', None)
        if act is not None:
            try:
                act.setText(f"❌ 取消注销(剩余时间{self._destroy_remaining_text()})")
                menu = act.parent()
                if menu is not None:
                    menu.update()
                return
            except Exception:
                self._destroy_action = None
        self.create_menu()

    def _ask_destroy_phrase(self, warn_text):
        """注销确认窗口（居中于屏幕）：须原样输入中文或英文确认短语"""
        dlg = QDialog(self)
        dlg.setWindowTitle("注销账号")
        dlg.setFixedSize(470, 275)
        dlg.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        try:
            set_window_emoji_icon(dlg, "🗑️")
        except Exception:
            pass
        dlg.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border: 1px solid #444444; border-radius: 10px; }
            QLabel { color: #E0E0E0; font-size: 13px; background: transparent; }
            QLabel#danger { color: #FF6B6B; font-weight: bold; font-size: 14px; }
            QLabel#phrase { color: #FFD700; font-weight: bold; }
            QLineEdit { background: #2D2D2D; color: white; border: 1px solid #555555;
                        border-radius: 6px; padding: 8px; font-size: 13px; }
            QLineEdit:focus { border-color: #B03A3A; }
            QPushButton { background-color: #3C3C3C; color: #E8E8E8; border: 1px solid #5A5A5A;
                          border-radius: 8px; padding: 8px 16px; font-size: 14px; }
            QPushButton:hover { background-color: #4C4C4C; }
            QPushButton#danger { background-color: #B03A3A; color: white; border: none; }
            QPushButton#danger:hover { background-color: #C94848; }
        """)
        layout = QVBoxLayout(dlg)
        warn = QLabel(warn_text)
        warn.setObjectName("danger")
        warn.setWordWrap(True)
        layout.addWidget(warn)
        phrase_lbl = QLabel("请输入以下内容确认（中文或英文）：\n"
                            "我确定注销并了解结果\n"
                            "I confirm the cancellation and acknowledge the consequences.")
        phrase_lbl.setObjectName("phrase")
        phrase_lbl.setWordWrap(True)
        layout.addWidget(phrase_lbl)
        edit = QLineEdit()
        layout.addWidget(edit)
        row = QHBoxLayout()
        row.addStretch()
        ok_btn = QPushButton("确认销毁")
        ok_btn.setObjectName("danger")
        cancel_btn = QPushButton("取消")
        row.addWidget(ok_btn)
        row.addWidget(cancel_btn)
        layout.addLayout(row)

        def _confirm():
            if not self._destroy_phrase_ok(edit.text()):
                QMessageBox.warning(dlg, "确认失败", "输入不正确，请原样输入确认内容")
                return
            dlg.accept()

        ok_btn.clicked.connect(_confirm)
        cancel_btn.clicked.connect(dlg.reject)
        # 居中于屏幕
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dlg.move(screen_center - dlg.rect().center())
        return dlg.exec_() == QDialog.Accepted

    def start_account_destroy(self):
        """注销账号入口：单机本地销毁；联机发送销号请求（服务端校验 + 5 分钟冷却）"""
        if self.mp_client.connected:
            # 联机同样必须先输入确认短语；提交后进入 5 分钟冷却期，每周最多申请 3 次
            if not self._ask_destroy_phrase(
                    "确定注销本账号？此操作不可逆！\n"
                    "（提交后进入 7 天冷却期，期间可在多人联机菜单中取消；每周最多申请 3 次）"):
                return
            try:
                self.mp_client._send({'type': 'account_destroy_request'})
            except Exception:
                traceback.print_exc()
            return
        # ---------- 单机：确认短语后本地销毁 ----------
        if not self._ask_destroy_phrase("⚠️ 此操作将销毁本机账号的所有文件和记录，且不可恢复！"):
            return
        self.destroy_local_account()

    def destroy_local_account(self):
        """单机：销毁本机账号的所有相关文件并立即退出登录"""
        try:
            userid = str((self.current_user or {}).get('userid', ''))
            username = str((self.current_user or {}).get('username', ''))
            prefixes = tuple(x for x in (f"{userid}.", f"{userid}_", f"{username}.", f"{username}_")
                             if x not in ('.', '_'))
            if os.path.isdir(USER_DATA_DIR):
                for fn in list(os.listdir(USER_DATA_DIR)):
                    if fn == 'user_login.json':
                        continue
                    if fn.startswith(prefixes):
                        try:
                            os.remove(os.path.join(USER_DATA_DIR, fn))
                        except OSError:
                            pass
            # 自动登录配置一并销毁
            try:
                if os.path.exists(USER_CONFIG):
                    os.remove(USER_CONFIG)
            except OSError:
                pass
        except Exception:
            traceback.print_exc()
        # 立即退出登录：先停掉注销倒计时定时器（否则退出前 1 秒内会触发 create_menu 访问已清空的用户）
        self.destroy_pending_until = None
        self.stop_destroy_countdown()
        # 清空当前用户（防止退出流程把已删除的档案又写回），
        # 再走标准登出流程（启动全新实例 + 结束本进程，不残留旧对象与定时器）
        self.current_user = None
        try:
            self._perform_logout()
        except Exception:
            traceback.print_exc()
        return

    def cancel_account_destroy(self):
        if not self.mp_client.connected:
            self.destroy_pending_until = None
            self.stop_destroy_countdown()
            self.create_menu()
            return
        try:
            self.mp_client._send({'type': 'account_destroy_cancel'})
        except Exception:
            traceback.print_exc()

    def on_account_destroy_status(self, data):
        state = data.get('state')
        if state == 'pending':
            until = data.get('until')
            try:
                dt = datetime.fromisoformat(until)
                self.destroy_pending_until = dt if dt.tzinfo else dt.astimezone()
            except Exception:
                self.destroy_pending_until = None
            self.start_destroy_countdown()
            self.create_menu()
            self.show_centered_message("注销申请已提交",
                                       "已进入 7 天注销冷却期，期间可随时在多人联机菜单中取消。")
        elif state == 'cancelled':
            self.destroy_pending_until = None
            self.stop_destroy_countdown()
            self.create_menu()
            self.show_centered_message("注销已取消", data.get('reason') or "注销已取消",
                                       QMessageBox.Information)
        elif state == 'processing':
            self.show_centered_message("提示", data.get('reason') or '此请求正在被处理',
                                       QMessageBox.Warning)
        elif state == 'rejected':
            self.show_centered_message("无法注销", data.get('reason') or '存在限制，无法注销账号',
                                       QMessageBox.Warning)
        elif state == 'already_destroyed':
            self.show_centered_message("提示", "该账号已注销", QMessageBox.Warning)

    def apply_pm_ban_self(self, ban):
        """保存自己的私信封禁（全局禁言）状态，并刷新横幅与输入锁"""
        self.self_pm_ban = ban if (isinstance(ban, dict) and ban.get('banned')) else None
        self.refresh_effective_mute()

    def refresh_effective_mute(self):
        """计算生效中的禁言：服内禁言或全局禁言任一有效，即与 mute 一样锁定输入框/发送按钮。

        全局禁言（私信封禁）同样禁止发送聊天，因此输入体验应与普通禁言完全一致，
        否则用户会反复尝试发送、每按一次回车弹一个模态提示框。"""
        ban = getattr(self, 'self_pm_ban', None)
        eff_until = getattr(self, 'server_muted_until', None)
        eff_perm = bool(getattr(self, 'server_muted_perm', False))
        if isinstance(ban, dict) and ban.get('banned'):
            if ban.get('permanent'):
                eff_until, eff_perm = None, True
            else:
                eff_until = ban.get('until') or eff_until
                eff_perm = False
            self.self_pm_ban_text = ban.get('until_text') or '永久'
        else:
            self.self_pm_ban_text = ''
        self.muted_until = eff_until
        self.muted_perm = eff_perm
        cw = self.chat_window
        if cw is not None:
            try:
                cw.update_mute_status(eff_until, eff_perm)
                cw.update_ban_banner()
            except Exception:
                traceback.print_exc()

    def apply_local_unmute(self, uid):
        """管理员解除某用户禁言后，本地立即清除其禁言缓存（服务端广播随后校正）"""
        self.user_pm_bans.pop(uid, None)
        self.user_server_mutes.pop(uid, None)
        for src in (getattr(self, 'latest_players', None), getattr(self, 'all_players', None),
                    getattr(self, 'friends', None), getattr(self, 'pending_strangers', None)):
            for p in (src or []):
                if isinstance(p, dict) and p.get('uid') == uid:
                    p['pm_ban'] = {'banned': False}
                    p['mute_server'] = {'muted': False}
        if self.chat_window is not None:
            try:
                self.chat_window.update_ban_banner()
            except Exception:
                traceback.print_exc()

    def merge_user_ban_info(self, entries):
        """合并一批玩家的私信封禁/服内禁言状态（player_list / all_players / friends_data）"""
        changed = False
        for p in (entries or []):
            if not isinstance(p, dict):
                continue
            uid = p.get('uid')
            if not uid:
                continue
            if uid == _my_uid(self) and 'pm_ban' in p:
                # 在线名单包含自己：作为自身禁言状态的兜底同步
                # （ping 每 10s 刷新一次，即使 mute_status 丢失也能自动解锁）
                self.apply_pm_ban_self(p.get('pm_ban'))
            ban = p.get('pm_ban')
            if isinstance(ban, dict) and ban.get('banned'):
                text = ban.get('until_text') or ('永久' if ban.get('permanent') else '')
                if self.user_pm_bans.get(uid) != text:
                    self.user_pm_bans[uid] = text
                    changed = True
            elif uid in self.user_pm_bans:
                self.user_pm_bans.pop(uid, None)
                changed = True
            sm = p.get('mute_server')
            if isinstance(sm, dict) and self.user_server_mutes.get(uid) != sm:
                self.user_server_mutes[uid] = sm
                changed = True
        if changed and self.chat_window is not None:
            try:
                self.chat_window.update_ban_banner()
            except Exception:
                traceback.print_exc()

    def on_mute_status(self, data):
        if isinstance(data, dict):
            muted_until = data.get('muted_until')
            muted_perm = bool(data.get('muted_perm'))
            ban = data.get('pm_ban')
        else:
            muted_until = data
            muted_perm = False
            ban = None
        if muted_until == "":
            muted_until = None
        self.server_muted_until = muted_until
        self.server_muted_perm = muted_perm
        self.self_pm_ban = ban if (isinstance(ban, dict) and ban.get('banned')) else None
        print(f"[禁言] 收到 mute_status: muted_until={muted_until} perm={muted_perm} "
              f"global={bool(self.self_pm_ban)}")
        self.refresh_effective_mute()

    def on_ai_status(self, enabled):
        print(f"[DEBUG] on_ai_status called with enabled={enabled}")
        self.ai_enabled = enabled
        # 刷新托盘菜单（如果存在）
        if self.tray_icon:
            self.create_menu()

    def _save_single_state_backup(self):
        """将单机状态保存到磁盘，用于强制结束后的恢复"""
        backup = {
            'persistent_state': self.persistent_state,
            'allow_move': self.allow_move,
            'cursor_style': self.current_user.get('cursor_style', 'None'),
            'pet_size': self.current_user.get('pet_size', 'normal'),
            'last_feed_time': self.current_user.get('last_feed_time'),
            'last_interaction_time': self.last_interaction_time,
            'emo_random_delay': self.emo_random_delay,
            'inventory_backup': self.local_inventory_backup  # 加入联机前保存的库存
        }
        path = os.path.join(USER_DATA_DIR, 'single_state_backup.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(backup, f, indent=2)
        except Exception as e:
            print(f"[备份] 保存单机状态失败: {e}")

    def _is_expire_format(self, s):
        if not s:
            return False
        s = s.strip()
        if s.isdigit():
            return True
        if re.match(r'^(\d+[YMD])+$', s.upper()):
            return True
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except:
            pass
        return False

    def _parse_items_spec(self, items_spec):
        """
        解析物品列表，返回 items 列表，失败返回 None。
        支持 cake, credit, ticket, star_rail_tickets, jade, 时装ID。
        """
        items = []
        for part in items_spec.split('&'):
            part = part.strip()
            if not part:
                continue
            if ':' in part:
                itype, qty = part.split(':', 1)
                try:
                    qty = int(qty)
                except:
                    self.show_centered_message("错误", f"数量错误: {part}", QMessageBox.Warning)
                    return None
                if qty < 1 or qty > 9999:
                    self.show_centered_message("错误", f"数量必须在1-9999: {part}", QMessageBox.Warning)
                    return None
            else:
                itype = part
                qty = 1

            # ★ 关键修改：增加 jade 和 star_rail_tickets 支持
            if itype == 'cake':
                items.append({'type': 'cake', 'amount': qty})
            elif itype == 'credit':
                items.append({'type': 'credit', 'amount': qty})
            elif itype == 'ticket' or itype == 'star_rail_tickets':
                items.append({'type': 'star_rail_ticket', 'amount': qty})
            elif itype == 'jade':
                items.append({'type': 'jade', 'amount': qty})
            else:
                # 时装（保持原逻辑）
                items.append({'type': 'clothes', 'id': itype})

        if not items:
            self.show_centered_message("错误", "未指定有效物品", QMessageBox.Warning)
            return None
        return items

    def _restore_from_backup_if_needed(self):
        """程序启动时检查是否存在备份，若有则恢复单机状态（强制结束后）"""
        backup_path = os.path.join(USER_DATA_DIR, 'single_state_backup.json')
        if not os.path.exists(backup_path):
            return
        try:
            with open(backup_path, 'r', encoding='utf-8') as f:
                backup = json.load(f)

            # 恢复状态
            self.persistent_state = backup.get('persistent_state', 'Standby')
            self.allow_move = backup.get('allow_move', False)
            if 'cursor_style' in backup:
                self.current_user['cursor_style'] = backup['cursor_style']
                self.apply_cursor_style()
            if 'pet_size' in backup:
                self.current_user['pet_size'] = backup['pet_size']
                self.apply_pet_size()
            if 'last_feed_time' in backup:
                self.current_user['last_feed_time'] = backup['last_feed_time']
            if 'last_interaction_time' in backup:
                self.last_interaction_time = backup['last_interaction_time']
            if 'emo_random_delay' in backup:
                self.emo_random_delay = backup['emo_random_delay']

            # 恢复库存备份
            inv_backup = backup.get('inventory_backup')
            if inv_backup:
                self.local_inventory_backup = inv_backup
                self.current_user['balance'] = inv_backup['balance']
                self.current_user['credit'] = inv_backup['credit']
                self.current_user['owned_clothes'] = inv_backup['owned_clothes']
                self.current_user['star_rail_tickets'] = inv_backup['star_rail_tickets']
                self.local_inventory_backup = None  # 恢复后清除内存引用

            # 恢复动画
            if self.persistent_state == "Discomfort":
                self.changeToDiscomfort()
            else:
                self.changeToStandby()

            # 保存用户数据
            self._save_current_user()
            # 删除备份文件
            os.remove(backup_path)
            print("[恢复] 已从备份恢复单机状态")
        except Exception as e:
            print(f"[恢复] 从备份恢复失败: {e}")
            # 出错时删除备份文件，避免反复尝试
            try:
                os.remove(backup_path)
            except:
                pass

    def change_username_single(self):
        """单机模式下修改本地账号的用户名（不检测重名）"""
        if self.mp_client.connected:
            self.show_centered_message("提示", "请先断开服务器连接再修改用户名", QMessageBox.Warning)
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("✏️修改用户名")
        dialog.setFixedSize(350, 150)
        dialog.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        dialog.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit { 
                background-color: #2D2D2D; 
                color: white; 
                border: 1px solid #555; 
                border-radius: 5px; 
                padding: 9px 8px;   /* 原 padding: 8px，现上下增加1px */
                font-size: 14px;
            }
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 5px; padding: 8px 16px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("新用户名:"))
        name_edit = QLineEdit()
        name_edit.setText(self.current_user["username"])
        layout.addWidget(name_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)

        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())

        if dialog.exec_() != QDialog.Accepted:
            return

        new_name = name_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self, "提示", "用户名不能为空")
            return
        if new_name == self.current_user["username"]:
            return

        self.current_user["username"] = new_name
        self._save_current_user()
        self.create_menu()
        self.show_centered_message("成功", f"用户名已修改为「{new_name}」")

    def on_server_shutdown(self, reason):
        """服务端关闭或维护模式踢出时调用"""
        # 避免在已经断开的情况下重复弹窗
        if not self.mp_client.connected:
            return
        # 本弹窗之后还会走一次断线处理：抑制随后的"与服务器断开连接"弹窗，避免连续两个弹窗
        self._suppress_disconnect_message = True
        # 显示弹窗（深色风格，与服务端风格一致）
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("服务器通知")
        msg_box.setText(reason)
        msg_box.setIcon(QMessageBox.Information)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                color: #E0E0E0;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
                background-color: #1E1E1E;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)

        # 居中显示
        msg_box.adjustSize()  # 确保尺寸正确
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg_box.move(screen_center - msg_box.rect().center())

        msg_box.exec_()
        # 断开连接并恢复单机（disconnect_server 内部已包含恢复逻辑）
        self.disconnect_server()

    def _restore_single_player_state(self):
        """从备份恢复单机状态（不发送网络请求）"""
        if self.single_player_state:
            self.persistent_state = self.single_player_state['persistent_state']
            self.last_interaction_time = self.single_player_state['last_interaction_time']
            self.emo_random_delay = self.single_player_state['emo_random_delay']
            self.current_user['last_feed_time'] = self.single_player_state.get('last_feed_time')

            # 恢复动画状态
            if self.single_player_state['current_animation_state'] == "Discomfort":
                self.changeToDiscomfort()
            else:
                self.changeToStandby()

            # 恢复移动开关
            self.allow_move = self.single_player_state.get('allow_move', False)
            if self.allow_move:
                delay = random.randint(5 * 60, 15 * 60) * 1000
                self.move_timer.start(delay)
            else:
                self.move_timer.stop()
                if self.is_moving:
                    self.stop_move()

            # 恢复光标样式
            cursor_style = self.single_player_state.get('cursor_style')
            if cursor_style is not None:
                self.current_user['cursor_style'] = cursor_style
                self.apply_cursor_style()

            # 恢复宠物大小
            pet_size = self.single_player_state.get('pet_size')
            if pet_size is not None:
                self.current_user['pet_size'] = pet_size
                self.apply_pet_size()

            # ★ 恢复单机时装
            if 'current_clothes' in self.single_player_state:
                saved_clothes = self.single_player_state['current_clothes']
                owned = self.current_user.get('owned_clothes', ['normal'])
                # 如果单机不拥有该时装，强制改为 normal
                if saved_clothes not in owned or saved_clothes not in AVAILABLE_CLOTHES:
                    saved_clothes = 'normal'
                self.current_user['current_clothes'] = saved_clothes
                self._save_current_user()
                # 应用时装（静默）
                self.apply_clothes(saved_clothes, silent=True)
            else:
                # 如果没有保存，则执行有效性检查（兜底）
                self.apply_current_clothes()

            self.single_player_state = None
            self._save_current_user()

        if self.local_inventory_backup:
            # 恢复库存...
            self._save_current_user()
            self.local_inventory_backup = None

        self.update_bag_display()
        self.apply_current_clothes()
        self.create_menu()

    def on_server_banned(self, message):
        if self._ban_dialog_showing:
            return
        self._ban_dialog_showing = True
        try:
            self.banned = True
            self.show_centered_message("封禁通知", message, QMessageBox.Warning)
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.close()
                self.chat_window = None
        finally:
            self._ban_dialog_showing = False

    def _bring_chat_to_front(self):
        if not self.chat_enabled:
            self.show_centered_message("提示", "❌ 聊天功能已被管理员关闭", QMessageBox.Warning)
            return
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.raise_()
            self.chat_window.activateWindow()
        else:
            self.open_chat_window()

    def showEvent(self, event):
        super().showEvent(event)
        # 修复：showEvent 是 Qt 虚函数，异常冒泡回 C++ 会让 PyQt 直接 abort
        # （进程无日志消失）。这里整体兜住并记录。
        try:
            if not self._initialized:
                try:
                    self.apply_cursor_style()
                    self.apply_pet_size()
                    self._init_movie_from_file()
                    self.hi_music()
                    self._initialized = True
                except Exception as e:
                    print(f"初始化资源失败: {e}")

            # === 统计打开次数（仅单机模式统计） ===
            if self.current_user:
                self.check_month_reset()
                self.current_user["total_open_count"] += 1
                # 联机模式下，monthly_open_count 由加入服务器时增加，此处不再增加
                if not self.mp_client.connected:
                    self.current_user["monthly_open_count"] += 1
                self._save_current_user()
        except Exception:
            _log_exception(sys.exc_info())

    def on_focus_changed(self, old, new):
        if old == self and self.draggable:
            self.draggable = False
            # 恢复当前应有的动画
            if self.persistent_state == "Discomfort":
                self.changeToDiscomfort()
            else:
                self.changeToStandby()

    def load_volume(self):
        path = os.path.join(USER_DATA_DIR, 'volume.json')
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f).get('volume', 0.5)
            except:
                pass
        return 0.5

    def save_volume(self):
        path = os.path.join(USER_DATA_DIR, 'volume.json')
        try:
            with open(path, 'w') as f:
                json.dump({'volume': self.volume}, f)
        except:
            pass

    def set_volume(self, value):
        self.volume = max(0.0, min(1.0, value))
        if pygame.mixer.get_init():
            pygame.mixer.music.set_volume(self.volume)

        # 保存到当前用户数据
        if self.current_user is not None:
            self.current_user["volume"] = self.volume
            self._save_current_user()  # 写入用户 JSON 文件
            print(f"[DEBUG] 音量已保存: {self.volume} 到用户 {self.current_user.get('userid')}")
        else:
            print("[WARN] 未登录，无法保存音量")

    def on_chat_history_ready(self, history):
        self.chat_history = normalize_chat_history(history)
        QTimer.singleShot(100, self.refresh_chat_window)

    def on_mails_ready(self, mails):
        self.online_mailbox = mails
        if self._pending_mail_request == 'open':
            self._pending_mail_request = None
            # 在主线程中创建邮箱窗口
            mailbox_win = MailboxWindow(self, self, online_mails=mails)
            mailbox_win.exec_()

    def on_claim_mail_result(self, success, message):
        if success:
            self.mp_client._send({'type': 'get_mails'})
            QTimer.singleShot(0, lambda: self.show_centered_message("邮件", "物品已领取成功！"))
            # === 新增：统计领取的信用点（需从之前保存的 _pending_credit 获取） ===
            if hasattr(self, '_pending_credit') and self._pending_credit:
                self.check_month_reset()
                self.current_user["monthly_credit_gained"] += self._pending_credit
                self._save_current_user()
                self._pending_credit = 0
        else:
            QTimer.singleShot(0, lambda: self.show_centered_message("领取失败", message or "未知错误",
                                                                    QMessageBox.Warning))

    def on_sign_result(self, success, message):
        if success:
            self.show_centered_message("签到成功", message)
            self.online_signed_today = True
            # === 新增统计（联机签到） ===
            self.check_month_reset()
            self.current_user["total_sign_days"] += 1
            self.current_user["monthly_sign_days"] += 1
            self.current_user["total_interactions"] += 1
            self.current_user["monthly_interactions"] += 1
            # 联机签到获得 7200 信用点
            self.current_user["monthly_credit_gained"] += 7200
            self._save_current_user()
            self.create_menu()
        else:
            self.show_centered_message("签到提示", message, QMessageBox.Warning)

    def on_redeem_result(self, success, message):
        if success:
            self.show_centered_message("兑换成功", message, QMessageBox.Information)
        else:
            self.show_centered_message("兑换失败", message, QMessageBox.Warning)

    def on_command_result(self, success, message):
        print(f"[DEBUG] on_command_result: success={success}, message={message}")
        if success:
            if '撤回成功' in message:
                return
            if '改名成功' in message:
                import re
                match = re.search(r'你现在的名字是 (.+)$', message)
                if match:
                    new_name = match.group(1).strip()
                else:
                    new_name = self._pending_new_name
                if new_name:
                    self.online_name = new_name
                    # ★ 设置保护标志，防止后续玩家列表更新覆盖新名字
                    self._name_just_changed = True
                    # 关闭强制改名对话框（如果存在）
                    if self._rename_dialog and self._rename_dialog.isVisible():
                        self._rename_dialog.accept()
                        self._rename_dialog = None
                        if hasattr(self, '_rename_list_connection'):
                            self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
                            self._rename_list_connection = None
                        self.rename_required = False
                    self._pending_new_name = None
                    # 刷新托盘菜单
                    self.create_menu()
                    # 弹出成功提示
                    QTimer.singleShot(0, lambda: self.show_centered_message(
                        "改名成功",
                        f"改名成功！你现在的名字是 {new_name}"
                    ))
                    # 请求玩家列表以同步（但会触发覆盖，但保护标志会阻止覆盖）
                    self.mp_client.request_player_list()
                    self.mp_client._send({'type': 'get_chat_history'})
                else:
                    self.create_menu()
            elif '禁言' in message:
                # 禁言/解禁结果不再弹模态框（聊天窗口已有系统广播）：
                # 用自动消失的托盘气泡提示，避免桌面上反复出现弹窗
                show_notification_async(title="禁言操作", message=message, duration=3)
            elif '兑换码净获得统计' in message or '兑换码统计' in message or message.count('\n') >= 3:
                # 兑换统计/对话记录等长文本：居中窗口展示，支持滚轮上下滑动
                self.show_scroll_text_dialog("命令结果", message)
            else:
                QTimer.singleShot(0, lambda: self.show_centered_message("命令结果", message, QMessageBox.Information))
        else:
            if self._rename_dialog and self._rename_dialog.isVisible():
                self._rename_dialog.setEnabled(True)
                QMessageBox.warning(self._rename_dialog, "改名失败", message)
            elif '禁言' in message:
                show_notification_async(title="禁言操作失败", message=message, duration=3)
            else:
                QTimer.singleShot(0, lambda: self.show_centered_message("命令失败", message, QMessageBox.Warning))
            self._pending_new_name = None

    def check_month_reset(self):
        if not self.current_user:
            return
        now = datetime.now()
        current_month = now.strftime("%Y-%m")
        if self.current_user.get("current_month") != current_month:
            monthly_keys = [
                "monthly_interactions", "monthly_open_count", "monthly_sign_days",
                "monthly_affection_up", "monthly_sleep_count", "monthly_chat_count",
                "monthly_credit_gained", "monthly_cake_gained",
                "monthly_ai_click_count",
                "monthly_jade_gained", "monthly_ticket_gained"
            ]
            for key in monthly_keys:
                self.current_user[key] = 0
            self.current_user["current_month"] = current_month
            self._save_current_user()

    def generate_monthly_report(self):
        # 防止重复调用
        if getattr(self, '_report_generating', False):
            return
        self._report_generating = True

        try:
            # 确定使用哪个数据源
            if self.mp_client.connected and self.online_stats:
                stats = self.online_stats
            else:
                if not self.current_user:
                    return
                self.check_month_reset()
                stats = {
                    'monthly_interactions': self.current_user.get('monthly_interactions', 0),
                    'monthly_open_count': self.current_user.get('monthly_open_count', 0),
                    'monthly_sign_days': self.current_user.get('monthly_sign_days', 0),
                    'monthly_affection_up': self.current_user.get('monthly_affection_up', 0),
                    'monthly_sleep_count': self.current_user.get('monthly_sleep_count', 0),
                    'monthly_chat_count': self.current_user.get('monthly_chat_count', 0),
                    'monthly_credit_gained': self.current_user.get('monthly_credit_gained', 0),
                    'monthly_cake_gained': self.current_user.get('monthly_cake_gained', 0),
                }
                if self.mp_client.connected and not self.online_stats:
                    self.mp_client._send({'type': 'get_stats'})
                    pass

            import calendar, re, os
            now = datetime.now()
            days = calendar.monthrange(now.year, now.month)[1]

            interactions = stats.get('monthly_interactions', 0)
            open_count = stats.get('monthly_open_count', 0)
            sign_days = stats.get('monthly_sign_days', 0)
            affection = stats.get('monthly_affection_up', 0)
            sleep_count = stats.get('monthly_sleep_count', 0)
            credit_gained = stats.get('monthly_credit_gained', 0)
            cake_gained = stats.get('monthly_cake_gained', 0)

            # === 根据联机状态决定在线对话次数来源 ===
            if self.mp_client.connected:
                chat_count = stats.get('monthly_chat_count', 0)
                chat_label = "多人对话次数"
            else:
                chat_count = self.current_user.get('monthly_ai_click_count', 0)
                chat_label = "在线对话次数"

            # === 根据联机状态决定打开次数标签 ===
            open_label = "加入服务器次数" if self.mp_client.connected else "本月打开桌宠次数"

            # 计算进度条
            credit_target = 7200 * days
            credit_percent = int((credit_gained / credit_target * 100) if credit_target > 0 else 0)
            credit_percent = min(100, credit_percent)
            credit_display = f"{credit_gained} / {credit_target}（{credit_percent}%）"

            sign_percent = int((sign_days / days * 100) if days > 0 else 0)
            sign_percent = min(100, sign_percent)
            sign_display = f"{sign_days} / {days}（{sign_percent}%）"

            interact_percent = int((interactions / 500 * 100) if interactions > 0 else 0)
            interact_percent = min(100, interact_percent)
            interact_display = f"{interactions} / 500（{interact_percent}%）"

            cake_percent = int((cake_gained / 72 * 100) if cake_gained > 0 else 0)
            cake_percent = min(100, cake_percent)
            cake_display = f"{cake_gained} / 72（{cake_percent}%）"

            monthly_jade = self.current_user.get('monthly_jade_gained', 0)
            monthly_ticket = self.current_user.get('monthly_ticket_gained', 0)
            # 进度条相关
            jade_target = 1600
            jade_percent = min(100, int(monthly_jade / jade_target * 100)) if jade_target > 0 else 0
            jade_display = f"{monthly_jade} / {jade_target}（{jade_percent}%）"

            # 获取显示名称和UID
            if self.mp_client.connected and self.online_name:
                display_name = self.online_name
            else:
                display_name = self.current_user.get('username', '开拓者') if self.current_user else '开拓者'

            if self.mp_client.connected and self.mp_client.server_uid:
                display_uid = self.mp_client.server_uid
            else:
                display_uid = self.current_user.get('uid', '未知') if self.current_user else '未知'

            data = {
                "monthly_jade_gained": monthly_jade,
                "monthly_ticket_gained": monthly_ticket,
                "jade_display": jade_display,
                "jade_percent": jade_percent,
                "monthly_interactions": interactions,
                "monthly_open_count": open_count,
                "open_label": open_label,  # 新增
                "monthly_sign_days": sign_days,
                "monthly_affection_up": affection,
                "monthly_sleep_count": sleep_count,
                "monthly_chat_count": chat_count,
                "chat_label": chat_label,
                "monthly_credit_gained": credit_gained,
                "monthly_cake_gained": cake_gained,
                "days_in_month": days,
                "current_year_month": now.strftime("%Y年%m月"),
                "mode_text": "联机模式" if self.mp_client.connected else "单机模式",
                "credit_display": credit_display,
                "credit_percent": credit_percent,
                "sign_display": sign_display,
                "sign_percent": sign_percent,
                "interact_display": interact_display,
                "interact_percent": interact_percent,
                "cake_display": cake_display,
                "cake_percent": cake_percent,
                "display_name": display_name,
                "display_uid": display_uid,
            }

            template_path = os.path.join(get_base_dir(), "html", "month.html")
            if not os.path.exists(template_path):
                self.show_centered_message("错误", "报告模板文件 month.html 不存在", QMessageBox.Warning)
                return

            with open(template_path, "r", encoding="utf-8") as f:
                html = f.read()

            for key, value in data.items():
                pattern = r'{{\s*' + re.escape(key) + r'\s*}}'
                html = re.sub(pattern, str(value), html)

            report_dir = "report"
            if not os.path.exists(report_dir):
                os.makedirs(report_dir)

            report_path = os.path.join(report_dir, "firefly_report.html")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(html)

            webbrowser.open(report_path)

        except Exception as e:
            import traceback
            print(f"[报告生成] 异常: {e}")
            traceback.print_exc()
            self.show_centered_message("错误", f"生成报告失败：{str(e)}", QMessageBox.Warning)

        finally:
            self._report_generating = False

    def on_pm_ack(self, data):
        """私信发送回执"""
        if self.chat_window is not None:
            try:
                self.chat_window.on_pm_ack(data)
            except Exception:
                traceback.print_exc()

    def on_pm_recv(self, data):
        """收到私信（窗口未创建时缓存，等窗口打开回放）"""
        # 好友消息默认系统通知（与被 @ 时一致）；特别关注始终提醒；免打扰/陌生人跳过
        try:
            self._notify_pm_message(data)
        except Exception:
            traceback.print_exc()
        if self.chat_window is not None:
            try:
                self.chat_window.on_pm_recv(data)
            except Exception:
                traceback.print_exc()
        else:
            self._pm_backlog.append(data)

    def _remark_for(self, uid):
        """优先返回该玩家的备注（通知/显示用）"""
        if not uid:
            return ''
        cw = self.chat_window
        if cw is not None:
            convo = cw.convos.get('pm:' + uid)
            if convo is not None and convo.get('remark'):
                return convo['remark']
        for f in (getattr(self, 'friends', []) or []):
            if f.get('uid') == uid and f.get('remark'):
                return f['remark']
        return ''

    def show_scroll_text_dialog(self, title, text):
        """居中窗口展示长文本（QPlainTextEdit 支持滚轮上下滑动）"""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(*fit_4_3(560, 420))
        dlg.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        dlg.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QPlainTextEdit { background-color: #2D2D2D; color: #E0E0E0; border: 1px solid #555;
                             border-radius: 6px; padding: 10px; font-size: 13px; }
            QPushButton { background-color: #4CAF50; color: white; border: none;
                          border-radius: 6px; padding: 8px 20px; font-size: 14px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text or '')
        layout.addWidget(view, 1)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn, alignment=Qt.AlignCenter)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dlg.move(screen_center - dlg.rect().center())
        dlg.exec_()

    def _notify_pm_message(self, data):
        """私信系统通知规则：
        - 特别关注：始终提醒（即使之前开启了免打扰）
        - 好友：默认提醒，除非对该会话开启了消息免打扰
        - 陌生人 / 已拉黑：不提醒
        - 正在查看该会话时不打扰
        """
        uid = data.get('from_uid') or ''
        if not uid:
            return
        if uid in (self.blocked or []):
            return  # 已拉黑用户的消息服务端已拦截，此处双保险
        special = uid in (self.pm_special_focus or [])
        muted = uid in (self.pm_muted or [])
        is_friend = bool(data.get('is_friend'))
        if not (special or (is_friend and not muted)):
            return
        cw = self.chat_window
        if cw is not None and cw.isVisible() and getattr(cw, 'current_conv', None) == 'pm:' + uid:
            return  # 正在看这个会话，不弹通知
        from_name = data.get('from_name') or uid
        # 通知优先使用备注作为主要称呼
        display = self._remark_for(uid) or from_name
        text = data.get('text') or ''
        show_notification_async(
            title=f"来自 {display} 的私信",
            message=text if text else f"{display} 发来一条消息",
            icon_path=asset_path("images", "firefly", "Sadness", "abandoned.ico"),
            duration=5
        )

    def apply_chat_prefs(self, prefs):
        """应用服务端下发的会话状态（服务端为权威来源）"""
        if not isinstance(prefs, dict):
            return
        self.pm_special_focus = list(prefs.get('focus') or [])[:3]
        self.pm_muted = list(prefs.get('muted') or [])
        self.pm_pinned = list(prefs.get('pinned') or [])
        self.pm_hidden = list(prefs.get('hidden') or [])
        if self.chat_window is not None:
            try:
                self.chat_window.on_chat_prefs_data()
            except Exception:
                traceback.print_exc()

    def save_chat_prefs(self, pinned=None, hidden=None):
        """把会话状态提交到服务端保存（服务端校验后回发权威副本）"""
        if pinned is not None:
            self.pm_pinned = list(pinned)
        if hidden is not None:
            self.pm_hidden = list(hidden)
        payload = {
            'focus': list(self.pm_special_focus or [])[:3],
            'muted': list(self.pm_muted or []),
            'pinned': list(self.pm_pinned or []),
            'hidden': list(self.pm_hidden or []),
        }
        try:
            self.mp_client._send({'type': 'save_chat_prefs', 'data': payload})
        except Exception:
            traceback.print_exc()

    def on_chat_prefs_data(self, data):
        """服务端保存后的权威会话状态"""
        prefs = data.get('chat_prefs') if isinstance(data, dict) else None
        if isinstance(prefs, dict):
            self.apply_chat_prefs(prefs)

    def on_friends_data(self, data):
        """好友列表同步（含服务端保存的会话状态）"""
        self.friends = [p for p in (data.get('friends', []) or [])
                        if not is_placeholder_player_name((p or {}).get('name'))]
        self.pending_strangers = data.get('pending', []) or []
        self.blocked = data.get('blocked', []) or []
        self.destroyed_users = data.get('destroyed_users', {}) or {}
        if data.get('destroy_pending_until'):
            try:
                _dt = datetime.fromisoformat(data.get('destroy_pending_until'))
                self.destroy_pending_until = _dt if _dt.tzinfo else _dt.astimezone()
            except Exception:
                pass
        self.start_destroy_countdown()
        self.merge_user_ban_info(self.friends)
        self.merge_user_ban_info(self.pending_strangers)
        if 'self_pm_ban' in data:
            self.apply_pm_ban_self(data.get('self_pm_ban'))
        prefs = data.get('chat_prefs')
        if isinstance(prefs, dict):
            self.apply_chat_prefs(prefs)
            # 迁移：服务端为空但本地有旧设置时，推送一次
            legacy = getattr(self, '_legacy_prefs_pending', None)
            if legacy is not None:
                if (legacy.get('focus') or legacy.get('muted')) \
                        and not self.pm_special_focus and not self.pm_muted:
                    self.pm_special_focus = list(legacy.get('focus') or [])[:3]
                    self.pm_muted = list(legacy.get('muted') or [])
                    self.save_chat_prefs()
                self._legacy_prefs_pending = None
        if self.chat_window is not None:
            try:
                self.chat_window.on_friends_data()
            except Exception:
                traceback.print_exc()

    def send_block(self, target_uid, block):
        """拉黑/取消拉黑"""
        try:
            self.mp_client._send({'type': 'block_user',
                                  'data': {'target_uid': target_uid, 'block': bool(block)}})
        except Exception:
            traceback.print_exc()

    def on_friend_delete_result(self, data):
        """删除好友结果"""
        if self.chat_window is not None:
            try:
                self.chat_window.on_friend_delete_result(data)
            except Exception:
                traceback.print_exc()

    def on_block_result(self, data):
        """拉黑操作结果：更新本地拉黑名单"""
        if data.get('ok'):
            self.blocked = data.get('list', []) or []
        if self.chat_window is not None:
            try:
                self.chat_window.on_block_result(data)
            except Exception:
                traceback.print_exc()

    def on_all_players(self, players):
        """全部玩家名单（含离线）"""
        self.all_players = [p for p in (players or [])
                            if not is_placeholder_player_name((p or {}).get('name'))]
        self.merge_user_ban_info(self.all_players)
        cb = self._all_players_cb
        self._all_players_cb = None
        if cb is not None:
            try:
                cb(self.all_players)
            except Exception as e:
                traceback.print_exc()
                # 不再静默吞掉：把错误弹出来，避免表现为"点击无反应"
                try:
                    QMessageBox.warning(None, "发起聊天", f"打开玩家选择框失败：{e}")
                except Exception:
                    pass

    def request_all_players(self, callback=None):
        """请求服务端下发全部玩家名单；收到后（GUI 线程）回调"""
        self._all_players_cb = callback
        if self.mp_client.connected:
            try:
                self.mp_client._send({'type': 'get_all_players'})
            except Exception:
                traceback.print_exc()
        elif callback is not None:
            callback(self.all_players)

    def cancel_all_players_request(self):
        """取消待回调的名单请求（超时兜底后，迟到的回包只更新缓存不再弹框）"""
        self._all_players_cb = None

    def on_pm_history_data(self, data):
        """私信历史回包"""
        if self.chat_window is not None:
            try:
                self.chat_window.on_pm_history_data(data)
            except Exception:
                traceback.print_exc()

    def on_friend_remark_result(self, data):
        """好友备注保存结果"""
        if data.get('ok'):
            uid = data.get('target_uid')
            remark = data.get('remark', '') or ''
            for f in self.friends:
                if f.get('uid') == uid:
                    f['remark'] = remark
                    break
        if self.chat_window is not None:
            try:
                self.chat_window.on_friend_remark_result(data)
            except Exception:
                traceback.print_exc()

    def refresh_chat_window(self):
        if self.chat_window is None:
            return
        # 清空现有消息
        for i in reversed(range(self.chat_window.msg_layout.count())):
            item = self.chat_window.msg_layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
        # 重新加载历史消息
        for msg in self.chat_history:
            self.chat_window.add_message_obj(msg)  # 改为调用 add_message_obj
        self.chat_window._request_scroll_bottom()

    def _safe_open_chat(self):
        try:
            self.open_chat_window()
        except Exception as e:
            print(f"重新打开聊天窗口失败: {e}")

    def _handle_kick(self, reason):
        """在主线程中处理踢出（独立模态弹窗，等待用户点击）"""
        self._kick_pending = True
        # 父对象设为 None，使其成为独立窗口，避免被 _close_all_windows 关闭
        msg_box = QMessageBox(None)
        msg_box.setWindowTitle("提示")
        _destroy_text = getattr(self, '_account_destroyed_text', None)
        if _destroy_text:
            # 账号注销：用统一的深色居中弹窗
            self._account_destroyed_text = None
            self._kick_pending = False
            self.show_centered_message("账号注销", _destroy_text, QMessageBox.Information)
            return
        else:
            msg_box.setText(f"与服务器断开连接！\n{reason}")
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.Dialog)
        msg_box.setModal(True)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                color: #E0E0E0;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
                background-color: #1E1E1E;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        # 居中于屏幕
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg_box.move(screen_center - msg_box.rect().center())
        # 模态阻塞，等待用户点击
        msg_box.exec_()
        # 用户点击后，手动断开连接（不显示额外弹窗）
        self._kick_pending = False
        self.disconnect_server(show_message=False)

    def on_permission_changed(self):
        userid = self.current_user['userid']
        filepath = os.path.join(USER_DATA_DIR, f'{userid}.json')
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                self.current_user = json.load(f)
        if self.mp_client.server_uid:
            self.server_uid = self.mp_client.server_uid
        self.create_menu()
        if self.online_overlay and self.online_overlay.isVisible():
            self.mp_client.request_player_list()

    def on_global_message(self, msg):
        """
        处理服务端推送的全局消息（聊天、系统、提及、踢出等）
        """
        if not isinstance(msg, dict):
            return

        mtype = msg.get('type')
        data = msg.get('data', {})

        # ---------- 新增：服务端配置热更新 ----------
        if mtype == 'update_custom_config':
            self.server_custom_menu = data.get('custom_menu', [])
            self.server_custom_items = data.get('custom_items', [])
            self.cache_custom_items(self.server_custom_items)   # 热更新后刷新缓存
            # 热更新包里带卡池配置时一并收下（老服务端不带，保持原值即可）
            try:
                _gc = data.get('gacha_config')
                if isinstance(_gc, dict) and _gc:
                    self.gacha_config = _gc
            except Exception:
                pass
            self.create_menu()
            self.check_missing_resources()
            # 登记自定义时装并刷新商店，使改动即时生效
            self.sync_custom_items()
            # 热更新时主动拉一次商店数据（双保险：即使服务端某条广播没到，也能刷到最新）
            try:
                _w = getattr(self, 'shop_window', None)
                if _w is not None:
                    _w.request_shop_goods()
            except Exception as _e:
                print(f"[商店] 热更新拉取失败: {_e}")
            # 打开中的卡池页面：同步物品图片（按版本覆盖）后**重新注入**，无需重进
            # 注意：不要用 reload()——reload 会清掉页面里的注入数据，
            #       而重新注入（setPools/applyPoolMeta）本来就是幂等的，效果更稳。
            try:
                views = getattr(self, '_gacha_webviews', None) or {}
                for _mid, _v in list(views.items()):
                    if _v is None:
                        continue
                    self._sync_item_images(_mid)
                self._refresh_open_gacha_views()
            except Exception as e:
                print(f"[卡池] 物品图即时刷新失败: {e}")
            return

        # ---------- 处理踢出 ----------
        if mtype == 'kick':
            reason = data.get('reason', '你已被管理员踢出服务器，可重新连接。')
            QTimer.singleShot(0, lambda: self._handle_kick(reason))
            return

        # ---------- 已注销名单更新（他人被注销时实时同步） ----------
        if mtype == 'destroyed_users_data':
            self.destroyed_users = data.get('destroyed_users', {}) or {}
            if self.chat_window is not None:
                try:
                    self.chat_window._mark_destroyed_convos()
                    self.chat_window._rebuild_conv_list()
                except Exception:
                    traceback.print_exc()
            return

        # ---------- 被注销提示（先弹窗，随后服务端踢出） ----------
        if mtype == 'account_destroyed':
            self._account_destroyed_text = (data.get('text') or '你的账户已被注销！')
            self._suppress_disconnect_message = True
            # 账号已注销：清空"记住登录状态"，避免下次自动登入已注销的账号
            try:
                if os.path.exists(USER_CONFIG):
                    os.remove(USER_CONFIG)
                    logging.info("[销号] 已清空记住的登录状态")
            except Exception:
                traceback.print_exc()
            return

        # ---------- 卡池配置更新（服务端改动即时下发） ----------
        if mtype == 'bind_result':
            _bd = data or {}
            if _bd.get('success'):
                try:
                    if _bd.get('bind_id'):
                        self.save_bind_id(_bd.get('bind_id'))
                    self.restricted = False
                except Exception:
                    pass
                self.show_centered_message("绑定成功", str(_bd.get('message') or '本机已绑定该账号'),
                                           QMessageBox.Information)
            else:
                self.show_centered_message("绑定失败", str(_bd.get('message') or '绑定失败'),
                                           QMessageBox.Warning)
            return

        if mtype == 'gacha_config':
            cfg = data.get('config')
            if isinstance(cfg, dict):
                self.gacha_config = cfg
                self._refresh_open_gacha_views()
            return

        # ---------- 商店商品热更新 ----------
        if mtype == 'shop_goods':
            try:
                # 先缓存并交给商店窗口统一应用（含本地缓存兜底）
                goods = data.get('goods') or []
                self.shop_goods = goods
                self._shop_goods_seen = True   # 服务端已明确下发（空清单也算）
                win = getattr(self, 'shop_window', None)
                if win is not None:
                    try:
                        # 能收到服务端下发就说明在线：顺便纠正窗口上可能过期的联机标志
                        # （否则离线时开的窗口会一直当成单机显示本地商品）
                        win.is_online = True
                        try:
                            win._shop_log("收到服务端商店数据：%d 件 %s"
                                          % (len(goods), [g.get('id') for g in goods][:12]))
                        except Exception:
                            pass
                        win.apply_shop_goods(goods, 'server')
                    except Exception as e:
                        print(f"[商店] 刷新失败: {e}")
                # 2) 再后台同步商品图片资源
                try:
                    self._sync_shop_images_local()
                except Exception as e:
                    print(f"[商店] 资源同步失败: {e}")
            except Exception as e:
                print(f"[商店] 热更新失败: {e}")
            return

        # ---------- 页面内编辑卡池的结果 ----------
        if mtype == 'gacha_config_save_result':
            self.on_gacha_config_save_result(data)
            return
        if mtype == 'gacha_image_result':
            self.on_gacha_image_result(data)
            return

        # ---------- 卡池资源包更新（封面等文件变了，需重下） ----------
        if mtype == 'gacha_resource_updated':
            try:
                self.on_gacha_resource_updated(data.get('menu_id'))
            except Exception as e:
                print(f"[卡池] 资源更新处理失败: {e}")
            return

        # ---------- 注销账号状态 ----------
        if mtype == 'account_destroy_status':
            self.on_account_destroy_status(data)
            return

        # ---------- 禁言状态 ----------
        if mtype == 'mute_status':
            self.on_mute_status(data)
            return

        # ---------- 聊天广播 ----------
        if mtype == 'chat_broadcast':
            from_name = data.get('from_name', '')
            text = data.get('text', '')
            is_system = data.get('is_system', False)
            if is_system and '撤回了一条消息' in text:
                return
            msg_id = data.get('msg_id', None)
            from_uid = data.get('from_uid', None)
            from_perm = data.get('from_perm', 'user')
            timestamp = data.get('time', datetime.now().isoformat())
            my_uid = self.mp_client.server_uid or self.server_uid
            if from_uid and my_uid:
                is_self = (from_uid == my_uid)
            else:
                is_self = (from_name == self.online_name or from_name == self.current_user.get('username', ''))
            msg_obj = {
                'id': msg_id,
                'sender': from_name,
                'text': text,
                'time': timestamp,
                'is_system': is_system,
                'is_self': is_self,
                'from_uid': from_uid,
                'from_perm': from_perm
            }
            self.chat_history.append(msg_obj)
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.add_message_obj(msg_obj)
            return

        # ---------- 撤回通知 ----------
        if mtype == 'recall_notification':
            # 撤回提示文案由服务端计算并下发（管理员撤回他人消息会带 [管理员] 与原作者）
            # —— 客户端只在收到服务端同意后的本条通知时才改动界面
            msg_id = data.get('msg_id')
            sender = data.get('sender', '某人')
            notice = data.get('text') or f"{sender}撤回了一条消息"
            for msg in self.chat_history:
                if (msg.get('id') or msg.get('msg_id')) == msg_id:
                    msg['text'] = notice
                    msg['is_system'] = True
                    msg['sender'] = '系统'
                    msg['revoked'] = True
                    break
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.refresh_all_messages()
                self.chat_window.remove_pm_bubble_by_id(msg_id, notice)
            return

        # ---------- 玩家列表更新 ----------
        if mtype == 'player_list':
            players = data.get('players', [])
            self.update_online_list_overlay(players)
            return

        # ---------- 库存更新 ----------
        if mtype == 'inventory_update':
            inv = data.get('inventory', {})
            self.online_inventory = inv
            today = datetime.now().strftime('%Y-%m-%d')
            self.online_signed_today = (inv.get('last_sign_date') == today)
            if 'custom_items' in inv:
                self.online_inventory['custom_items'] = inv['custom_items']
            self.update_bag_display()
            if hasattr(self, '_before_redeem_credit') and self._before_redeem_credit is not None:
                old_credit = self._before_redeem_credit
                new_credit = inv.get('credit', 0)
                diff = new_credit - old_credit
                if diff > 0:
                    self.check_month_reset()
                    self.current_user["monthly_credit_gained"] += diff
                    self._save_current_user()
                self._before_redeem_credit = None
            self.apply_current_clothes()
            return

        # ---------- 签到结果 ----------
        if mtype == 'sign_result':
            success = data.get('success', False)
            message = data.get('message', '')
            self.on_sign_result(success, message)
            return

        # ---------- 兑换结果 ----------
        if mtype == 'redeem_result':
            success = data.get('success', False)
            message = data.get('message', '')
            self.on_redeem_result(success, message)
            return

        # ---------- 命令执行结果 ----------
        if mtype == 'command_result':
            success = data.get('success', False)
            message = data.get('message', '')
            self.on_command_result(success, message)
            return

        # ---------- 邮件数据 ----------
        if mtype == 'mails_data':
            mails = data.get('mails', [])
            self.on_mails_ready(mails)
            return

        # ---------- 聊天历史 ----------
        if mtype == 'chat_history_data':
            history = data.get('history', [])
            self.on_chat_history_ready(history)
            return

        # ---------- 领取邮件结果 ----------
        if mtype == 'claim_mail_result':
            success = data.get('success', False)
            reason = data.get('reason', '')
            self.on_claim_mail_result(success, reason)
            return

        # ---------- 统计数据 ----------
        if mtype == 'stats_data':
            stats = data.get('stats', {})
            self.online_stats = stats
            if self.current_user:
                for key, value in stats.items():
                    if key in self.current_user:
                        self.current_user[key] = value
                self._save_current_user()
            return

        # ---------- 权限变更 ----------
        if mtype == 'permission_changed':
            self.server_permission = data.get('permission', 'user')
            self.server_uid = data.get('server_uid', self.server_uid)
            self.on_permission_changed()
            return

        # ---------- 服务器关闭通知 ----------
        if mtype == 'server_shutdown':
            reason = data.get('reason', '服务器已关闭')
            # 先断开连接并设置标志，避免在弹窗期间触发意外断开弹窗
            self.disconnect_server(show_message=False)
            # 再显示通知
            self.show_centered_message("服务器通知", reason, QMessageBox.Information)
            return

        # ---------- 系统消息 ----------
        if mtype == 'system_message':
            text = data.get('text', '')
            if "聊天记录已清空" in text:
                self.chat_history.clear()
                if self.chat_window and self.chat_window.isVisible():
                    self.chat_window.refresh_all_messages()
                    self.chat_window.add_message(
                        sender='系统',
                        text=text,
                        time_str=datetime.now().strftime('%m-%d %H:%M'),
                        is_system=True
                    )
                return
            if '封禁' in text or 'banned' in text.lower():
                self.banned = True
                self.show_centered_message("封禁通知", text, QMessageBox.Warning)
                self.disconnect_server(show_message=False)
                return
            if '已被解除禁言' in text:
                import re
                match = re.search(r'(\S+)\s*已被解除禁言', text)
                if match:
                    target = match.group(1)
                    my_name = self.online_name or self.current_user.get('username', '')
                    # 仅当解禁对象是自己时才本地清状态；服务端还会下发权威 mute_status。
                    # （原"只要本地有禁言就清"会在别人被解禁时误清自己的全局禁言状态）
                    if target == my_name:
                        self.on_mute_status(None)
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.add_message(
                    sender='系统',
                    text=text,
                    time_str=datetime.now().strftime('%m-%d %H:%M'),
                    is_system=True
                )
            return

        # ---------- 其他未知消息 ----------
        print(f"[未处理消息] type={mtype}, data={data}")

    def get_display_uid(self):
        if self.mp_client.connected and self.online_uid:
            # 显示 UID（已包含 admin/user 前缀），并附加管理员标识
            perm_suffix = " [管理员]" if self.is_admin else ""
            return f"{self.online_name} ({self.online_uid}){perm_suffix}"
        return f"{self.current_user['username']} (UID:{self.current_user['uid']})"

    def change_online_name(self):
        if not self.mp_client.connected:
            self.show_centered_message("提示", "请先加入服务器", QMessageBox.Warning)
            return
        if self.chat_window and self.chat_window.isVisible():
            QTimer.singleShot(0,
                              lambda: self.show_centered_message("提示", "请先关闭聊天窗口再改名", QMessageBox.Warning))
            return

        current_name = self.online_name or self.current_user['username']
        # 默认剩余次数为1（服务端会更新）
        dlg = ChangeNameDialog(self, current_name, 1, self.server_timezone_offset)
        self._pending_rename_dialog = dlg  # 保存引用

        # 请求服务端获取剩余次数
        self.mp_client._send({'type': 'get_rename_info', 'data': {}})

        # 连接玩家列表更新信号（用于重名校验）
        self.mp_client.online_list_updated.connect(dlg.update_player_list)

        if dlg.exec_() == QDialog.Accepted:
            name = dlg.get_new_name()
            if name:
                # 检查是否已过期（服务端会再次验证）
                self._pending_new_name = name
                self.mp_client._send({'type': 'change_name', 'data': {'new_name': name}})

        self.mp_client.online_list_updated.disconnect(dlg.update_player_list)
        self._pending_rename_dialog = None

    def on_login_success_mp(self, data):
        # 提取字段
        server_uid = data.get('server_uid')
        perm = data.get('permission')
        inv = data.get('inventory')
        server_version = data.get('server_version')
        stats = data.get('stats', {})
        require_rename = data.get('require_rename', False)
        uid = data.get('uid', '')
        chat_history = data.get('chat_history', [])

        # ----- 强制设置 online_name -----
        server_name = data.get('server_name') or data.get('name', '')
        self.online_name = server_name if server_name else self.current_user.get('username', '用户')
        print(f"[登录] 设置 online_name = '{self.online_name}' (来自服务端: '{server_name}')")
        # --------------------------------

        print(f"[登录成功] 收到历史消息 {len(chat_history)} 条")
        self.chat_history = normalize_chat_history(chat_history)

        # ===== 以下代码完全保持原样，只需将变量名替换为上面提取的变量 =====
        # 版本二次校验
        if server_version and not versions_compatible(CLIENT_VERSION, server_version):
            self._is_manual_disconnect = True
            self.show_centered_message("版本不兼容",
                                       f"客户端版本 {CLIENT_VERSION} 与服务器版本 {server_version} 不匹配",
                                       QMessageBox.Warning)
            self.mp_client.disconnect()
            return

        self.connecting = False
        # 预取全量玩家名单（发起聊天用）：正常路径下点击按钮时缓存已就绪，无需等待
        self.request_all_players()
        self.is_admin = (perm == 'admin')
        # 保存单机状态
        self.single_player_state = {
            'persistent_state': self.persistent_state,
            'current_animation_state': self.current_animation_state,
            'last_interaction_time': self.last_interaction_time,
            'emo_random_delay': self.emo_random_delay,
            'last_feed_time': self.current_user.get('last_feed_time'),
            'current_clothes': self.current_user.get('current_clothes', 'normal')
        }
        self.changeToStandby()
        self.persistent_state = "Standby"
        self.last_interaction_time = time.time()
        self.emo_random_delay = random.randint(0, 15 * 60)
        self.current_user['last_feed_time'] = None

        if inv is None:
            inv = {'balance': 0, 'credit': 0, 'owned_clothes': ['normal'], 'star_rail_tickets': 0}
        self.online_inventory = inv
        self.update_bag_display()

        # ===== 处理统计数据 =====
        if stats:
            self.online_stats = stats
            for key, value in stats.items():
                if key in self.current_user:
                    self.current_user[key] = value
            self._save_current_user()

        # ===== 联机模式：加入服务器计数 =====
        self.check_month_reset()
        self.current_user["monthly_open_count"] += 1
        self._save_current_user()

        # ---------- 修改点1：接收服务端自定义菜单和物品 ----------
        self.server_custom_menu = data.get('custom_menu', [])
        self.server_custom_items = data.get('custom_items', [])
        self.cache_custom_items(self.server_custom_items)   # 登录就存一份：单机/离线时显示名称与图片
        # 卡池配置：登录时必须收下！否则刚登录就打开卡池页面会拿到空列表 → 显示"无卡池"
        # （服务端登录包里有 gacha_config；以前这里漏了，只能等一次 gacha_config 广播才恢复）
        try:
            _gc = data.get('gacha_config')
            if isinstance(_gc, dict) and _gc:
                self.gacha_config = _gc
        except Exception:
            pass
        # 多卡池：各卡池的保底进度（页面切换卡池时显示）
        self.gacha_pity = data.get('gacha_pity') or {}
        # 商店商品：由服务端下发的商店管理内容（含内置商品改价与上架的自定义物品）
        try:
            self.shop_goods = data.get('shop_goods') or []
            if 'shop_goods' in data:
                self._shop_goods_seen = True   # 服务端明确给了清单（空清单也算）
        except Exception:
            self.shop_goods = []
        # 记录服务端版本（排查"服务端未响应商店请求"时一眼可见）
        try:
            self.server_version = data.get('server_version') or '未知'
        except Exception:
            self.server_version = '未知'
        if 'shop_goods' not in data:
            print('[商店] 注意：服务端未下发 shop_goods 字段（服务端版本可能过旧）')

        # 资源包版本握手：本地缓存与服务端签名不一致 → 静默重下（新模板/新封面必达）
        try:
            self._sync_outdated_resources(data.get('resource_sigs') or {})
        except Exception as e:
            print(f"[资源握手] 失败: {e}")
        # 延迟同步自定义时装资源与商店资源，避免登录阶段被资源下载拖住
        QTimer.singleShot(1200, self.sync_custom_items)
        QTimer.singleShot(1600, self._sync_shop_images_local)
        # 检查资源完整性（若需要下载则弹窗）
        self.check_missing_resources()

        # ---------- 修改点2：仅保留一次 create_menu() ----------
        self.create_menu()

        if self.chat_window:
            self.chat_window.update_mute_status(self.muted_until,
                                               bool(getattr(self, 'muted_perm', False)))

        if require_rename:
            QTimer.singleShot(300, self.show_forced_rename_dialog)
        else:
            QTimer.singleShot(100,
                              lambda: self.show_centered_message("成功", f"已连接到服务器\n你的服务器UID: {uid}"))

        # 应用时装
        self.apply_current_clothes()
        self._waiting_for_name = True
        self.mp_client.request_player_list()
        self.mp_client.ping()
        self.ping_timer.start()
        self.online_uid = uid

        # 确保打开窗口时加载最新历史
        if hasattr(self, 'chat_window') and self.chat_window and self.chat_window.isVisible():
            self.chat_window.refresh_all_messages()

        # ===== 新增：保存服务端下发的 gacha_history =====
        gacha_history = data.get('gacha_history', [])
        if gacha_history:
            self.current_user['gacha_history'] = gacha_history
            self._save_current_user()
            print(f"[登录] 已同步抽卡历史 {len(gacha_history)} 条")

    def _update_online_players_for_rename(self, players):
        # 如果对话框已关闭，断开连接并返回
        if not self._rename_edit or self._rename_edit is None:
            if hasattr(self, '_rename_list_connection'):
                self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
                self._rename_list_connection = None
            return
        self._online_players = [p.get('name', '') for p in players]
        if self._rename_edit:
            self._on_rename_text_changed(self._rename_edit.text())

    def show_forced_rename_dialog(self):
        # 请求在线玩家列表，用于重名检查
        self._online_players = []
        if hasattr(self, '_rename_list_connection'):
            self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
        self._rename_list_connection = self.mp_client.online_list_updated.connect(
            self._update_online_players_for_rename)
        self.mp_client.request_player_list()

        dialog = QDialog(self)
        dialog.setWindowTitle("设置昵称")
        dialog.setModal(True)
        dialog.setFixedSize(400, 250)
        dialog.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())

        dialog.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit { 
                background-color: #2D2D2D; 
                color: white; 
                border: 1px solid #555; 
                border-radius: 5px; 
                padding: 8px; 
            }
            QLineEdit::placeholder { color: #AAAAAA; }
            QPushButton { 
                background-color: #4CAF50; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                padding: 8px 16px; 
            }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; }
            QLabel#status_label { font-size: 11px; }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        layout.addWidget(QLabel("欢迎首次加入服务器！请设置你的昵称："))
        self._rename_edit = QLineEdit()
        self._rename_edit.setPlaceholderText("输入昵称（最多16字符）")
        self._rename_edit.setMaxLength(16)
        layout.addWidget(self._rename_edit)

        self._rename_status_label = QLabel()
        self._rename_status_label.setObjectName("status_label")
        self._rename_status_label.setStyleSheet("color: #AAAAAA;")
        layout.addWidget(self._rename_status_label)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok)
        btn_box.button(QDialogButtonBox.Ok).setText("确定")
        btn_box.accepted.connect(self._submit_forced_rename)
        dialog.rejected.connect(self._on_rename_dialog_rejected)
        layout.addWidget(btn_box)

        self._rename_edit.textChanged.connect(self._on_rename_text_changed)
        self._rename_ok_btn = btn_box.button(QDialogButtonBox.Ok)
        self._rename_ok_btn.setEnabled(False)

        self._rename_dialog = dialog
        dialog.exec_()

    def _center_dialog_on_parent_or_screen(self, dialog):
        if self.isVisible() and self.geometry().width() > 0:
            # 居中于当前窗口
            parent_rect = self.geometry()
            x = parent_rect.x() + (parent_rect.width() - dialog.width()) // 2
            y = parent_rect.y() + (parent_rect.height() - dialog.height()) // 2
            # 确保不超出屏幕
            screen = QApplication.primaryScreen().availableGeometry()
            x = max(screen.x(), min(screen.right() - dialog.width(), x))
            y = max(screen.y(), min(screen.bottom() - dialog.height(), y))
            dialog.move(x, y)
        else:
            # 屏幕居中
            screen = QApplication.primaryScreen().availableGeometry()
            x = (screen.width() - dialog.width()) // 2
            y = (screen.height() - dialog.height()) // 2
            dialog.move(x, y)

    def _on_rename_dialog_rejected(self):
        if self._rename_dialog is None:
            return
        dialog = self._rename_dialog
        self._rename_dialog = None
        # 断开玩家列表更新连接
        if hasattr(self, '_rename_list_connection'):
            self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
            self._rename_list_connection = None
        try:
            dialog.done(0)
        except Exception as e:
            print(f"关闭改名对话框异常: {e}")
        self.rename_required = False
        self.disconnect_server(show_message=True)

    def _on_rename_text_changed(self, text):
        new_name = text.strip()
        status_label = self._rename_status_label
        ok_btn = self._rename_ok_btn

        if not new_name:
            status_label.setText("昵称不能为空")
            status_label.setStyleSheet("color: #FF5555;")
            ok_btn.setEnabled(False)
            return

        # 格式校验
        if not Firefly.is_valid_nickname(new_name):
            if new_name.startswith("开拓者-") and new_name != "开拓者":
                status_label.setText("此昵称不合法")
            else:
                status_label.setText("昵称不合法")
            status_label.setStyleSheet("color: #FF5555;")
            ok_btn.setEnabled(False)
            return

        # 异步检查重名
        ok_btn.setEnabled(False)
        status_label.setText("检查中...")
        status_label.setStyleSheet("color: #AAAAAA;")
        self.mp_client._send({'type': 'check_name', 'data': {'new_name': new_name}})

    def _submit_forced_rename(self):
        if not self._rename_dialog:
            return
        new_name = self._rename_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self._rename_dialog, "错误", "昵称不能为空")
            return
        if not Firefly.is_valid_nickname(new_name):
            QMessageBox.warning(self._rename_dialog, "错误",
                                "此昵称不合法")
            return
        self._pending_new_name = new_name
        self.mp_client._send({'type': 'change_name', 'data': {'new_name': new_name}})
        self._rename_dialog.setEnabled(False)

    def on_mp_login_failed(self, reason):
        """
        处理服务端返回的登录失败消息（黑名单、白名单、人数已满、版本不兼容、账号数据异常等）。
        弹出错误对话框，并断开连接恢复单机状态。
        """
        print("[DEBUG] on_mp_login_failed 被调用，原因:", reason)
        self.connecting = False
        # 自动重连期间失败：不弹窗，交给重连循环决定是否再试
        if getattr(self, '_reconnect_active', False):
            print(f"[重连] 第 {getattr(self, '_reconnect_tries', 0)}/3 次失败：{reason}")
            QTimer.singleShot(0, self._reconnect_after_fail)
            return
        # 已经登录成功后的报错（多为网络/套接字问题，例如服务端被直接关掉）：
        # 不弹"连接失败"窗口，交给断开→自动重连流程
        if getattr(self, '_was_connected', False):
            print(f"[联机] 会话中出错，按断开处理：{reason}")
            try:
                if self.mp_client.connected:
                    self.mp_client.disconnect()
            except Exception:
                pass
            return
        self._is_manual_disconnect = True

        # 根据错误类型定制标题
        if "版本" in reason or "版本号" in reason:
            title = "版本不兼容"
        elif "封禁" in reason or "ban" in reason.lower():
            title = "封禁通知"
        elif "人数已满" in reason or "上限" in reason:
            title = "连接失败"
        elif "白名单" in reason:
            title = "访问被拒"
        elif "黑名单" in reason:
            title = "访问被拒"
        elif "账号数据异常" in reason:
            title = "账号错误"
        else:
            title = "连接失败"

        # 使用 QTimer 确保弹窗在主线程执行（即使当前在子线程，也能安全显示）
        def show_dialog():
            msg_box = QMessageBox()
            msg_box.setWindowTitle(title)
            msg_box.setText(reason)
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
            msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #1E1E1E;
                    color: #E0E0E0;
                }
                QMessageBox QLabel {
                    color: #E0E0E0;
                    background-color: #1E1E1E;
                }
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 5px 10px;
                }
                QPushButton:hover {
                    background-color: #45A049;
                }
                QPushButton:pressed {
                    background-color: #3D8B40;
                }
            """)
            self.center_window(msg_box)  # 使用现有的居中方法
            msg_box.exec_()

        # 在主线程空闲时执行
        QTimer.singleShot(0, show_dialog)

        # 标记状态并断开连接
        if "封禁" in reason or "白名单" in reason or "黑名单" in reason:
            self.banned = True

        if self.mp_client.connected:
            self.mp_client.disconnect()
        if hasattr(self, '_restore_single_player_state'):
            self._restore_single_player_state()
        else:
            self.disconnect_server(show_message=False)

    def _show_disconnect_dialog(self, text=None):
        msg_box = QMessageBox()
        msg_box.setWindowTitle("连接断开")
        msg_box.setText(text or "与服务器的连接意外断开！")
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                color: #E0E0E0;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
                background-color: #1E1E1E;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton:pressed {
                background-color: #3D8B40;
            }
        """)
        self.center_window(msg_box)  # 只移动位置，不调整尺寸
        msg_box.exec_()  # 模态显示

    # ---------- 自动重连（服务端重启等意外断线） ----------
    def _cancel_auto_reconnect(self):
        """结束自动重连（用户手动联机 / 点击取消 / 主动断开时调用）"""
        self._reconnect_active = False
        self._reconnect_tries = 0
        try:
            _d = getattr(self, '_reconnect_dialog', None)
            if _d is not None:
                _d.close()
                _d.deleteLater()
                self._reconnect_dialog = None
        except Exception:
            self._reconnect_dialog = None

    def _start_auto_reconnect(self):
        """服务端意外断开（多半是重启）：显示「服务器正在重启，重连中…」并自动重连，最多 3 次，可取消"""
        if getattr(self, '_reconnect_active', False):
            return
        mp = self.mp_client
        user = self.current_user or {}
        self._reconnect_params = (
            getattr(mp, 'server_host', '127.0.0.1'),
            getattr(mp, 'server_port', 20082),
            user.get('userid', ''),
            user.get('uid', ''),
            user.get('username', '') or user.get('name', ''),
            getattr(self, '_last_country', ''),
        )
        self._reconnect_active = True
        self._reconnect_tries = 0
        self._reconnect_attempt()

    def _reconnect_attempt(self):
        if not getattr(self, '_reconnect_active', False):
            return
        self._reconnect_tries += 1
        tries = self._reconnect_tries
        self._show_reconnect_dialog(tries)
        try:
            host, port, userid, uid, name, country = self._reconnect_params
            self.mp_client.connect_to_server(host, port, userid, uid, name, country)
        except Exception as e:
            print(f"[重连] 发起连接失败: {e}")
            QTimer.singleShot(1500, self._reconnect_after_fail)

    def _reconnect_after_fail(self):
        """一次尝试失败后的收尾：还有次数就再试，否则提示并结束"""
        if not getattr(self, '_reconnect_active', False):
            return
        if self._reconnect_tries >= 3:
            self._reconnect_active = False
            self._close_reconnect_dialog()
            QTimer.singleShot(0, lambda: self._show_disconnect_dialog(
                "自动重连 3 次均失败。\n请确认服务器已启动后再手动联机。"))
            return
        QTimer.singleShot(1500, self._reconnect_attempt)

    def _show_reconnect_dialog(self, tries):
        try:
            _d = getattr(self, '_reconnect_dialog', None)
            if _d is not None:
                try:
                    _d.label.setText(f"服务器正在重启，重连中…（第 {tries}/3 次）")
                    _d.show()
                    _d.raise_()
                except Exception:
                    pass
                return
            dlg = QDialog(self)
            dlg.setWindowTitle("重连中")
            dlg.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
            lay = QVBoxLayout(dlg)
            lbl = QLabel(f"服务器正在重启，重连中…（第 {tries}/3 次）")
            lbl.setAlignment(Qt.AlignCenter)
            lay.addWidget(lbl)
            btn = QPushButton("取消")
            btn.clicked.connect(self._cancel_auto_reconnect)
            lay.addWidget(btn)
            dlg.label = lbl
            dlg.setFixedSize(320, 120)
            dlg.setStyleSheet("""
                QDialog { background-color: #1E1E1E; color: #E0E0E0; }
                QLabel { color: #E0E0E0; font-size: 13px; }
                QPushButton { background-color: #555; color: white; border: none;
                               border-radius: 4px; padding: 5px 14px; }
                QPushButton:hover { background-color: #666; }
            """)
            self._center_dialog_on_parent_or_screen(dlg)
            dlg.show()
            self._reconnect_dialog = dlg
        except Exception as e:
            print(f"[重连] 对话框失败: {e}")

    def _close_reconnect_dialog(self):
        try:
            _d = getattr(self, '_reconnect_dialog', None)
            if _d is not None:
                _d.close()
                _d.deleteLater()
                self._reconnect_dialog = None
        except Exception:
            self._reconnect_dialog = None

    def on_mp_status_changed(self, connected):
        self.connecting = False
        if not connected:
            # 关闭所有子窗口（聊天、商店等），但不会关闭独立弹窗
            self._close_all_windows()
            self.chat_history = []
            if self.chat_window:
                try:
                    self.chat_window.close()
                    self.chat_window = None
                except:
                    pass

            # 判断是否为意外断开（非主动断开且非被踢出，且未抑制消息）
            if self._was_connected and not self._is_manual_disconnect and not self._kick_pending and not self._suppress_disconnect_message:
                # 服务器重启等意外断开：显示"服务器正在重启，重连中…"，自动重连最多 3 次（可取消）
                QTimer.singleShot(800, self._start_auto_reconnect)
                QTimer.singleShot(0, self._safe_restore_single_player_state)
            else:
                # 被踢出或主动断开，直接恢复单机状态
                QTimer.singleShot(0, self._safe_restore_single_player_state)

            # 清理联机状态
            self._suppress_disconnect_message = False
            self.online_uid = ''
            self.server_uid = None
            self.online_name = None
            self.online_inventory = None
            self._was_connected = False
            self.ai_enabled = True
            self.muted_until = None
            self.ping_timer.stop()
            if self.chat_window:
                self.chat_window.update_mute_status(None)
            self.server_custom_menu = []
            self.server_custom_items = []
            self.create_menu()

            if self._is_manual_disconnect:
                QTimer.singleShot(0, self._safe_restore_single_player_state)

            self._is_manual_disconnect = False
            self._kick_pending = False
        else:
            self._was_connected = True
            # 自动重连成功：关掉"重连中"对话框
            if getattr(self, '_reconnect_active', False):
                self._reconnect_active = False
                self._close_reconnect_dialog()
                print("[重连] 已重新连上服务器")
            self.ping_timer.start()

    def _safe_restore_single_player_state(self):
        """安全恢复单机状态，捕获所有异常以防止卡死"""
        try:
            self._restore_single_player_state()
        except Exception as e:
            print(f"[恢复单机状态] 异常: {e}")
            import traceback
            traceback.print_exc()

    def _get_mp_menu_text(self):
        if not self.mp_client.connected:
            return "🌐 多人联机"
        latency = self.mp_client._latest_latency_ms
        if latency < 0:
            status = "连接中..."
            color = "#888888"
        elif latency <= 40:
            status = f"已连接  延迟{latency}ms"
            color = "#00FF00"  # 绿
        elif latency <= 119:
            status = f"已连接  延迟{latency}ms"
            color = "#FFD700"  # 黄
        else:
            status = f"已连接  延迟{latency}ms"
            color = "#FF0000"  # 红
        # 信号强度：用点表示
        if latency <= 40:
            signal = "█████"  # 满格
        elif latency <= 80:
            signal = "███░░"
        elif latency <= 119:
            signal = "██░░░"
        else:
            signal = "█░░░░"
        # 但为了简洁，只显示延迟颜色，信号强度可以用文字表示
        # 这里我们返回 HTML 格式的文本（但 QMenu 不支持富文本，需要设置样式）
        # 所以直接返回带颜色标记的文本，实际菜单项可能不支持颜色，但可用 setStyleSheet 或 setIcon
        # 我们使用 setText 加上颜色标记？但 QMenu 的 item 不支持富文本，所以用 setIcon 表示信号？太复杂。
        # 简化：只用文字颜色，用 QAction 的 setStyleSheet 设置前景色，但 QAction 不支持直接设置颜色。
        # 我们可以创建自定义 QAction？但太复杂。
        # 实用方案：在菜单项名称中加 emoji 表示信号，延迟用数字显示，颜色无法实现。
        # 但需求是“带颜色划分”，所以我们可以在菜单项文本中用颜色标记，但 QAction 的文本不支持 HTML。
        # 可通过 setData 等方式，但最好直接用 QWidgetAction 自定义。
        # 为了快速实现，我们可以在菜单项旁边加子菜单显示状态？
        # 简单起见：延迟颜色用不同 emoji 表示：绿色 ✅，黄色 ⚠️，红色 ❌
        if latency <= 40:
            signal_emoji = "🟢"
        elif latency <= 119:
            signal_emoji = "🟡"
        else:
            signal_emoji = "🔴"
        return f"🌐 多人联机 ({signal_emoji} 已连接 延迟{latency}ms)"

    def _on_ping_timer(self):
        print(f"[PING] 触发, active={self.ping_timer.isActive()}, connected={self.mp_client.connected}")
        if self.mp_client.connected:
            self.mp_client.ping()
        else:
            print("[PING] 未连接，停止定时器？")  # 可选

    def open_chat_window(self):
        if self._ui_locked():
            return
        if self.banned:
            return
        if not self.mp_client.connected:
            return
        if not self.chat_enabled:
            self.show_centered_message("提示", "❌ 聊天功能已被管理员关闭", QMessageBox.Warning)
            return  # 阻止继续执行，避免打开窗口
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.raise_()
            return
        # 请求玩家列表以同步自己的名字
        self.mp_client.request_player_list()
        # 稍微延迟一点确保名字更新后再打开窗口
        QTimer.singleShot(200, self._really_open_chat_window)

    def _really_open_chat_window(self):
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.raise_()
            self.chat_window.activateWindow()
            return
        if self.chat_window and not self.chat_window.isVisible():
            # 窗口存在但隐藏（例如被 close 后 hide），可以重用
            self.chat_window.show()
            self.chat_window.raise_()
            # ★ 修复：重用窗口时同步最新禁言状态（防止窗口隐藏期间收到禁言消息导致状态不同步）
            self.chat_window.update_mute_status(self.muted_until,
                                               bool(getattr(self, 'muted_perm', False)))
            return
        name = self.online_name or self.current_user['username']
        self.chat_window = ChatWindow(self, name, None)
        self.chat_window.show()
        # 回放窗口创建前收到的私信
        if self._pm_backlog:
            for _pm in list(self._pm_backlog):
                try:
                    self.chat_window.on_pm_recv(_pm)
                except Exception:
                    traceback.print_exc()
            self._pm_backlog.clear()

    def on_mentioned(self, who):
        if not who:
            return
        show_notification_async(
            title="流萤桌宠多人聊天",
            message=f"{who} @了你",
            icon_path=asset_path("images", "firefly", "Sadness", "abandoned.ico"),
            duration=5
        )
        self.open_chat_window()
        if self.chat_window:
            self.chat_window.setWindowFlags(self.chat_window.windowFlags() | Qt.WindowStaysOnTopHint)
            self.chat_window.show()
            QTimer.singleShot(5000, lambda: self.chat_window.setWindowFlags(
                self.chat_window.windowFlags() & ~Qt.WindowStaysOnTopHint) if self.chat_window else None)

    # ★ 新增方法
    def on_mentioned_all(self, who):
        if not who:
            return
        show_notification_async(
            title="流萤桌宠多人聊天",
            message=f"{who} @了所有人",
            icon_path=asset_path("images", "firefly", "Sadness", "abandoned.ico"),
            duration=5
        )
        self.open_chat_window()
        if self.chat_window:
            self.chat_window.setWindowFlags(self.chat_window.windowFlags() | Qt.WindowStaysOnTopHint)
            self.chat_window.show()
            QTimer.singleShot(5000, lambda: self.chat_window.setWindowFlags(
                self.chat_window.windowFlags() & ~Qt.WindowStaysOnTopHint) if self.chat_window else None)

    def update_online_list_overlay(self, players):
        # 更新悬浮窗
        self.online_overlay.update_players(players)
        # 记录最新在线名单（好友在线状态/右键私信 uid 解析用）
        if players:
            self.latest_players = [p for p in (players or [])
                                   if not is_placeholder_player_name((p or {}).get('name'))]
        self.merge_user_ban_info(players)
        if self.chat_window is not None:
            try:
                self.chat_window.refresh_online_status()
            except Exception:
                pass

        if not self.mp_client.connected:
            return

        # ★ 如果刚改完名，暂时不更新 online_name，防止被旧数据覆盖
        if hasattr(self, '_name_just_changed') and self._name_just_changed:
            # 但可以检查玩家列表中是否已包含新名字，如果包含则解除保护
            my_id = self.current_user['userid']
            for p in players:
                if p.get('id') == my_id:
                    if p.get('name') == self.online_name:
                        # 服务器已确认新名字，解除保护
                        self._name_just_changed = False
                    break
            return  # 本次不更新

        # 在列表中查找自己（通过 userid）
        my_id = self.current_user['userid']
        found_name = None
        found_uid = None
        for p in players:
            if p.get('id') == my_id:
                found_name = p['name']
                found_uid = p.get('uid', '')
                break

        changed = False
        if found_name is not None:
            if found_name != self.online_name:
                self.online_name = found_name
                changed = True
            if found_uid and found_uid != getattr(self.mp_client, 'server_uid', ''):
                self.mp_client.server_uid = found_uid
                self.server_uid = found_uid
                changed = True
            if changed:
                if self._waiting_for_name:
                    self._waiting_for_name = False
                self.create_menu()  # 刷新右键菜单
        else:
            if self._waiting_for_name:
                pass

    def get_active_inventory(self):
        if self.mp_client.connected and self.online_inventory is not None:
            return self.online_inventory
        return self.current_user

    def toggle_online_list(self):
        if self.online_overlay.isVisible():
            self.online_overlay.hide()
        else:
            self.mp_client.request_player_list()
            self.online_overlay.show_online()

    def restore_last_position(self):
        if not self.current_user:
            return
        x = self.current_user.get("last_x")
        y = self.current_user.get("last_y")
        if x is not None and y is not None:
            screen = QApplication.primaryScreen().availableGeometry()
            if x < screen.x():
                x = screen.x()
            if y < screen.y():
                y = screen.y()
            if x > screen.right() - self.width():
                x = screen.right() - self.width()
            if y > screen.bottom() - self.height():
                y = screen.bottom() - self.height()
            self.move(x, y)

    def show_exit_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("退出桌宠")
        dialog.setFixedSize(350, 180)
        dialog.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        dialog.setStyleSheet("""
                    QDialog {
                        background-color: #1E1E1E;
                        border: 2px solid #4CAF50;
                        border-top-left-radius: 0px;
                        border-top-right-radius: 0px;
                        border-bottom-left-radius: 8px;
                        border-bottom-right-radius: 8px;
                    }
                    QLabel {
                        color: #CCCCCC;
                        font-size: 15px;
                        font-weight: bold;
                    }
                    QPushButton {
                        background-color: #3A3A3A;
                        color: #EEEEEE;
                        border: 1px solid #555555;
                        border-radius: 8px;
                        padding: 10px;
                        font-size: 14px;
                    }
                    QPushButton:hover {
                        background-color: #4CAF50;
                        color: white;
                        border: 1px solid #4CAF50;
                    }
                    QPushButton#logout_btn {
                        background-color: #D32F2F;
                        border: 1px solid #D32F2F;
                    }
                    QPushButton#logout_btn:hover {
                        background-color: #F44336;
                    }
                    QPushButton#exit_btn {
                        background-color: #4CAF50;
                        border: 1px solid #4CAF50;
                    }
                    QPushButton#exit_btn:hover {
                        background-color: #45A049;
                    }
                """)

        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 20)
        layout.setSpacing(20)

        label = QLabel("确定要退出桌宠吗？")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)

        exit_btn = QPushButton("退出")
        exit_btn.setObjectName("exit_btn")
        exit_btn.clicked.connect(lambda: self._handle_exit_choice(dialog, clear_login=False))

        logout_btn = QPushButton("退出并登出")
        logout_btn.setObjectName("logout_btn")
        logout_btn.clicked.connect(lambda: self._handle_exit_choice(dialog, clear_login=True))

        btn_layout.addWidget(exit_btn)
        btn_layout.addWidget(logout_btn)
        layout.addLayout(btn_layout)

        dialog.setLayout(layout)
        set_window_emoji_icon(dialog, "🚪")
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())
        dialog.exec_()

    def _open_gacha_page(self, menu_id):
        if self._ui_locked():
            return
        """
        打开服务器下发的抽卡页面（QWebEngineView）。

        有效期语义（第一百二十七批起）：
          · 菜单的 expiry = 「按钮有效期」：到期只记日志，**不拦截打开、不自动关窗**；
          · 卡池的 end_time = 「卡池有效期」：页面按秒倒计时，到点该卡池自动下架。
        
        """
        import os
        from PyQt5.QtCore import QUrl, Qt, QTimer
        from PyQt5.QtWebChannel import QWebChannel
        from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings
        from PyQt5.QtWidgets import QApplication, QMessageBox

        # ----- 1. 查找菜单项并检查有效期 -----
        def find_menu_item(menu_id, items):
            for item in items:
                if item.get('id') == menu_id:
                    return item
                if 'children' in item:
                    found = find_menu_item(menu_id, item['children'])
                    if found:
                        return found
            return None

        menu_item = find_menu_item(menu_id, self.server_custom_menu)
        if not menu_item:
            self.show_centered_message("错误", "菜单项不存在", QMessageBox.Warning)
            return

        # 按钮有效期：只提示、不拦截（抽卡菜单的"卡池有效期"在卡池上，由页面按秒倒计时）
        _mexp = menu_item.get('expiry')
        if _mexp:
            try:
                _mdt = resolve_expiry_dt(_mexp)
                if _mdt is not None and datetime.now(_mdt.tzinfo) >= _mdt:
                    print(f"[卡池] {menu_item.get('label')} 的按钮有效期已过（不拦截打开）")
            except Exception:
                pass

        # ----- 1.5 完整性：本地页面被改动/丢失 → 自动修复（服务端清单为准） -----
        try:
            _ok_pg, _msg_pg = self.verify_and_repair_page(menu_id)
            if _msg_pg:
                print(f"[卡池完整性] {_msg_pg}")
                try:
                    self._push_gacha_notice(_msg_pg, 'ok' if _ok_pg else 'error')
                except Exception:
                    pass
        except Exception as _e:
            print(f"[卡池完整性] 校验失败(忽略): {_e}")

        # ----- 2. 构建资源路径（缺页面先由内置基底生成；同步自定义物品图片） -----
        if not self._ensure_gacha_base(menu_id):
            pass  # 基底缺失时仍走原检查，提示页面文件不存在
        try:
            self._sync_item_images(menu_id)
        except Exception as e:
            print(f"[物品图片] 同步失败: {e}")
        index_path = os.path.join(self.server_resource_dir, menu_id)
        abs_path = os.path.abspath(os.path.join(index_path, 'index.html'))
        print(f"[Gacha页面] 路径:{abs_path}")
        try:
            _pl, _ac, _mi, _kn = self._gacha_pools_args(menu_id)
            _cfg = getattr(self, 'gacha_config', {}) or {}
            self._gacha_log(
                "打开卡池页 menu_id=%s 页面存在=%s 配置里的池=%d 可下发=%d active=%s 在线=%s"
                % (menu_id, os.path.exists(abs_path), len(_cfg.get('pools') or []),
                   len(_pl), _ac, bool(getattr(self.mp_client, 'connected', False))))
            if not _pl:
                # 逐池写明原因（published / 归属页面 / 到期），并主动向服务端重拉一次配置自愈
                for _p in (_cfg.get('pools') or []):
                    if isinstance(_p, dict):
                        self._gacha_log("  池 %s: published=%s menu_id=%s 结束时间=%s"
                                        % (_p.get('id'), repr(_p.get('published')),
                                           repr(_p.get('menu_id')), repr(_p.get('end_time')
                                                                         or _p.get('expiry'))))
                try:
                    if bool(getattr(self.mp_client, 'connected', False)):
                        self.mp_client._send({'type': 'request_gacha_config', 'data': {}})
                        self._gacha_log("  已向服务端请求最新卡池配置（request_gacha_config）")
                except Exception as _e:
                    self._gacha_log("  请求卡池配置失败: %s" % _e)
        except Exception:
            pass
        if not os.path.exists(abs_path):
            self.show_centered_message("错误", "页面文件不存在", QMessageBox.Warning)
            return

        # ----- 3. 清理旧窗口资源 -----
        if hasattr(self, "gacha_view") and self.gacha_view is not None:
            try:
                self.gacha_view.destroyed.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                self.gacha_view.deleteLater()
            except RuntimeError:
                pass
            self.gacha_view = None

        if hasattr(self, "_current_gacha_bridge"):
            delattr(self, "_current_gacha_bridge")

        # ----- 4. 创建新窗口和桥接 -----
        view = QWebEngineView()
        try:
            _p = getattr(Qt, 'PreventContextMenu', None)
            if _p is not None:
                page = view.page()
                if page is not None:
                    page.setContextMenuPolicy(_p)
        except Exception:
            pass
        view.setWindowTitle("群星跃迁")
        screen_geo = QApplication.primaryScreen().availableGeometry()
        # 抽卡窗口保持原有尺寸（按屏幕 80%），不套 4:3
        view.resize(int(screen_geo.width() * 0.8), int(screen_geo.height() * 0.8))
        view.setAttribute(Qt.WA_DeleteOnClose, True)
        # 不显示任务栏图标（Tool 窗口），保留关闭按钮
        try:
            view.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        except Exception:
            pass
        self.gacha_view = view
        page = view.page()
        # 页面背景用深色：加载/首帧渲染期间不再是刺眼的白屏
        try:
            from PyQt5.QtGui import QColor
            page.setBackgroundColor(QColor("#0d1020"))
        except Exception:
            pass

        bridge = GachaBridge(self)
        self._current_gacha_bridge = bridge
        bridge.set_webview(view)

        self._gacha_channel = QWebChannel()
        self._gacha_channel.registerObject("gacha_bridge", bridge)
        page.setWebChannel(self._gacha_channel)

        # 登记到已打开的卡池页面表：热更新、余额推送、页面内提示都要用它
        try:
            if not hasattr(self, '_gacha_webviews'):
                self._gacha_webviews = {}
            self._gacha_webviews[menu_id] = view
            view.destroyed.connect(lambda *a, _m=menu_id: self._gacha_webviews.pop(_m, None))
            self._current_gacha_menu_id = menu_id
        except Exception:
            traceback.print_exc()

        # 窗口销毁清理
        def on_win_destroy():
            bridge.set_webview(None)
            self._gacha_channel = None
            self._current_gacha_bridge = None
            if hasattr(self, '_gacha_win_info'):
                self._gacha_win_info = None

        view.destroyed.connect(on_win_destroy)

        settings = view.settings()
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.AllowRunningInsecureContent, True)
        settings.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebGLEnabled, False)

        page.javaScriptConsoleMessage = lambda lv, msg, line, src: (
            print(f"[JS日志] line{line}: {msg}"),
            self._gacha_log(f"页面JS line{line}: {msg}"))[0]

        self._gacha_page_init_called = False
        self._gacha_load_count = 0

        def on_load_finished(ok):
            print(f"页面加载完成:{ok}")
            if not ok:
                return
            # 每次加载完成都注入（注入是幂等的）：
            # reload 会丢掉页面里的注入数据，若只在"第一次"注入，
            # 热更新触发的 reload 就会让页面变回"无卡池"。
            self._gacha_page_init_called = True
            self._gacha_load_count = getattr(self, '_gacha_load_count', 0) + 1
            self._gacha_log(f"loadFinished ok={ok} 第{self._gacha_load_count}次 menu_id={menu_id}")

            def _after_bridge():
                try:
                    page.runJavaScript("if(window.initBridge) initBridge();")
                    # 元信息 + 卡池列表 + 编辑模式：一次性注入（带就绪重试，避免漏注入）
                    _editable = bool(self.mp_client.connected and
                                     (self.current_user or {}).get('permission') == 'admin')
                    _pools, _act, _mid, _known = self._gacha_pools_args(menu_id)
                    self._gacha_log("注入卡池 %d 个（active=%s, 可编辑=%s）"
                                    % (len(_pools), _act, _editable))
                    page.runJavaScript(self._inject_page_js(menu_id, _editable))
                    # 稍后再推一次余额/保底，确保页面内数据与服务器一致
                    QTimer.singleShot(900, self.push_gacha_data)
                except Exception:
                    traceback.print_exc()
                    self._gacha_log("注入异常: %s" % traceback.format_exc().splitlines()[-1])

            QTimer.singleShot(300, _after_bridge)

        view.loadFinished.connect(on_load_finished)

        def real_load_html():
            target_url = QUrl.fromLocalFile(abs_path)
            view.load(target_url)

        QTimer.singleShot(150, real_load_html)

        # ----- 5. 记录窗口信息并启动过期检查定时器 -----
        self._gacha_win_info = {
            'menu_id': menu_id,
            # 按钮有效期（只记录/只打日志，不关窗、不拦截）
            'expiry': _mexp,
            'window': view,
            'menu_label': menu_item.get('label', '抽卡')
        }

        if not hasattr(self, '_gacha_expiry_timer'):
            self._gacha_expiry_timer = QTimer(self)
            self._gacha_expiry_timer.timeout.connect(self._check_gacha_expiry)
            self._gacha_expiry_timer.start(1000)

        view.show()
        view.raise_()
        view.activateWindow()
        print("跃迁窗口已打开")

    def _check_gacha_expiry(self):
        """按钮有效期到点：只记日志，**不再自动关窗**（用户要求有效期不影响使用）。

        卡池到点后是否还能抽由卡池自己的 end_time 决定：抽卡页面按秒倒计时，
        到点该卡池自动下架（页面提示"卡池已到结束时间，已自动下架"），窗口照常可用。
        """
        info = getattr(self, '_gacha_win_info', None)
        if not info:
            return
        expiry = info.get('expiry')
        if expiry and not info.get('_expiry_logged'):
            try:
                dt = resolve_expiry_dt(expiry)
                if dt is not None and datetime.now(dt.tzinfo) >= dt:
                    info['_expiry_logged'] = True
                    print(f"[卡池] {info.get('menu_label', '抽卡')} 的按钮有效期已过"
                          f"（不关窗、不拦截，卡池到点由卡池自身下架）")
            except Exception:
                pass

    def _inject_gacha_data(self, view):
        bridge = self._current_gacha_bridge
        if not bridge:
            print("[Gacha] 桥接对象不存在，无法注入数据")
            return
        try:
            data_str = bridge.getUserInfo()
            # 直接操作 DOM，更新显示
            js = f"""
            (function() {{
                console.log('开始注入数据');
                var data = {data_str};
                var ticketEl = document.getElementById('ticketCount');
                var jadeEl = document.getElementById('jadeCount');
                var pityEl = document.getElementById('pityCount');
                var uidEl = document.getElementById('uidDisplay');
                if (ticketEl) ticketEl.textContent = data.tickets || 0;
                if (jadeEl) jadeEl.textContent = data.jade || 0;
                if (pityEl) pityEl.textContent = data.pity || 0;
                if (uidEl) uidEl.textContent = 'UID: ' + (data.uid || '--');
                // 更新保底样式
                var badge = document.getElementById('pityBadge');
                if (badge) {{
                    var span = badge.querySelector('span');
                    if (span) {{
                        span.textContent = data.pity || 0;
                        if (data.pity >= 80) {{
                            badge.style.borderColor = 'rgba(247,201,72,0.4)';
                            span.style.color = '#f7c948';
                            badge.style.background = 'rgba(247,201,72,0.08)';
                        }} else if (data.pity >= 70) {{
                            badge.style.borderColor = 'rgba(247,201,72,0.2)';
                            span.style.color = '#e6c87a';
                            badge.style.background = 'rgba(0,0,0,0.5)';
                        }} else {{
                            badge.style.borderColor = 'rgba(255,255,255,0.06)';
                            span.style.color = '#e6c87a';
                            badge.style.background = 'rgba(0,0,0,0.5)';
                        }}
                    }}
                }}
                // 若前端需要更新其他数据，可继续添加
            }})();
            """
            view.page().runJavaScript(js)
            print(f"[Gacha] 数据注入完成，数据: {data_str}")
        except Exception as e:
            print(f"[Gacha] 注入失败: {e}")

    def _handle_exit_choice(self, dialog, clear_login=False):
        dialog.accept()
        if clear_login:
            self._perform_logout()  # 清除配置并重启
        else:
            self.out_win()  # 仅退出，不重启

    def _perform_logout(self):
        # 保存当前状态和位置
        if self.current_user:
            self.current_user["last_state"] = self.persistent_state
            pos = self.pos()
            self.current_user["last_x"] = pos.x()
            self.current_user["last_y"] = pos.y()
            self._save_current_user()

        # 删除自动登录配置
        if os.path.exists(USER_CONFIG):
            os.remove(USER_CONFIG)

        # 断开服务器连接（如果有）
        self.disconnect_server()

        # 停止音乐播放
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except Exception:
            pass

        # 隐藏窗口和托盘图标
        self.hide()
        if self.tray_icon:
            self.tray_icon.setVisible(False)

        # 注意：这里**不能**提前结束 _MEI 内的子进程（QtWebEngine 等）——
        # 旧进程还要继续运行约 1 秒，Qt 若访问已结束的渲染进程会原生崩溃（无日志）。
        # 子进程统一在 _quit_app → _hard_exit 里、真正退出的瞬间前清理。

        # 确定重启命令
        if getattr(sys, 'frozen', False):
            program = sys.executable
            args = []
        else:
            program = sys.executable
            args = [os.path.abspath(sys.argv[0])]

        # 启动新进程，使其独立于当前进程（工作目录锚定到 exe 所在目录）
        workdir = os.path.dirname(program) or None
        new_proc = None
        try:
            spawn_env = _spawn_env()
            if sys.platform == 'win32':
                new_proc = subprocess.Popen(
                    [program] + args,
                    cwd=workdir,
                    env=spawn_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                )
            else:
                new_proc = subprocess.Popen([program] + args, cwd=workdir,
                                            env=spawn_env, start_new_session=True)
            logging.info(f"[退出登录] 已启动新实例 pid={getattr(new_proc, 'pid', '?')}")
        except Exception as e:
            # 关键修复：启动失败不能静默退出，否则用户会"什么都没了"
            logging.error(f"[退出登录] 启动新实例失败: {e}")
            try:
                self.show()
                if self.tray_icon:
                    self.tray_icon.setVisible(True)
                QMessageBox.warning(self, "退出登录",
                                    f"重启桌宠失败：{e}\n请手动重新打开桌宠。")
            except Exception:
                pass
            return

        # 校验新实例确实起来了（最多等 3 秒），避免旧进程退出后什么都不剩
        started = False
        try:
            import psutil as _ps
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    if new_proc.poll() is None and _ps.pid_exists(new_proc.pid):
                        started = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
        except Exception:
            started = True  # 无法校验时不阻塞正常退出
        if not started:
            logging.error("[退出登录] 新实例未成功启动，保留当前进程")
            try:
                self.show()
                if self.tray_icon:
                    self.tray_icon.setVisible(True)
                QMessageBox.warning(self, "退出登录",
                                    "重启桌宠失败（新进程未启动）。\n请手动重新打开桌宠。")
            except Exception:
                pass
            return

        # 延迟退出旧进程，给新进程足够的初始化时间
        QTimer.singleShot(1000, self._quit_app)

    def _quit_app(self):
        """彻底退出当前应用程序（清理 _MEI 子进程后立即退出）"""
        _hard_exit(0)

    def record_interaction(self, extra_seconds=0):
        self.last_interaction_time = time.time() + extra_seconds
        self.emo_random_delay = random.randint(0, 15 * 60)
        if self.current_animation_state == "Discomfort":
            self.changeToStandby()
            self.last_animation_state = "Standby"

    def check_emo(self):
        if self.current_animation_state == "Sleep":
            return
        if self.current_animation_state == "Discomfort":
            return

        elapsed = time.time() - self.last_interaction_time
        threshold = 20 * 60 + self.emo_random_delay
        if elapsed >= threshold:
            self.changeToDiscomfort()

    def apply_cursor_style(self):
        if not self.current_user:
            return
        style = self.current_user.get("cursor_style", "None")
        self._apply_cursor_by_style(style)

    def _apply_cursor_by_style(self, style):
        if style == "Firefly":
            pixmap_path = os.path.join(get_base_dir(), "mouse", "Firefly", "p1.gif")
            pixmap = QPixmap(pixmap_path)
            if not pixmap.isNull():
                self.setCursor(QCursor(pixmap))
            else:
                self.setCursor(QCursor(Qt.ArrowCursor))
        elif style == "Sam":
            pixmap_path = os.path.join(get_base_dir(), "mouse", "Sam", "p2.gif")
            pixmap = QPixmap(pixmap_path)
            if not pixmap.isNull():
                self.setCursor(QCursor(pixmap))
            else:
                self.setCursor(QCursor(Qt.ArrowCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

    def set_cursor_style(self, style):
        if not self.current_user:
            return
        self._apply_cursor_by_style(style)
        self.current_user["cursor_style"] = style
        self._save_current_user()

    def _set_pet_size(self, size, save=True):
        target = size
        existing = None
        for name in list_size_markers():
            existing = name
            break
        if existing and existing != target:
            try:
                if os.path.exists(get_size_marker_path(target)):
                    os.remove(get_size_marker_path(target))  # 先删除已存在的目标文件
                os.rename(get_size_marker_path(existing), get_size_marker_path(target))
            except Exception as e:
                print(f"重命名大小标记文件失败: {e}")
        elif not existing:
            try:
                with open(get_size_marker_path(target), "w") as f:
                    f.write("")
            except Exception as e:
                print(f"创建大小标记文件失败: {e}")
        self._init_movie_from_file()
        if save and self.current_user:
            self.current_user["pet_size"] = target
            self._save_current_user()

    def apply_pet_size(self):
        if not self.current_user:
            return
        size = self.current_user.get("pet_size", "normal")
        self._set_pet_size(size, save=False)

    def _init_movie_from_file(self):
        state = "Standby"
        if self.current_user and "last_state" in self.current_user:
            if self.current_user["last_state"] == "Discomfort":
                state = "Discomfort"
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            self.movie = QMovie(asset_path("images", "firefly", "actions", state, f"{state}_Large.gif"))
        elif 'small' in files:
            self.movie = QMovie(asset_path("images", "firefly", "actions", state, f"{state}_small.gif"))
        elif 'normal' in files:
            self.movie = QMovie(asset_path("images", "firefly", "actions", state, f"{state}.gif"))
        elif 'medium' in files:
            self.movie = QMovie(asset_path("images", "firefly", "actions", state, f"{state}_medium.gif"))
        else:
            self.movie = QMovie(asset_path("images", "firefly", "actions", state, f"{state}.gif"))
        self.label.setMovie(self.movie)
        self.movie.start()
        self.current_animation_state = state
        self.persistent_state = state

    def init_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = os.path.join(get_base_dir(), "icon", "setting.ico")
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setBrush(QBrush(QColor(0, 123, 255)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(0, 0, 16, 16, 4, 4)
            painter.end()
            self.tray_icon.setIcon(QIcon(pixmap))
        self.create_menu()
        self.tray_icon.show()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and obj is self:
            self.wheelEvent(event)
            return True
        return super().eventFilter(obj, event)

    def get_existing_file_name(self):
        for name in self.possible_file_names:
            if os.path.exists(name):
                return name
        return None

    def play_music(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def open_settings_tool(self):
        import threading
        import http.server
        import socketserver
        import webbrowser
        import json
        import os
        import sys
        import subprocess
        from urllib.parse import unquote, urlparse, parse_qs
        import traceback

        project_root = get_base_dir()
        log_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.getcwd(),
                                'firefly_client.log')

        def log_error(msg):
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"[工具页错误] {msg}\n")
            except:
                pass

        class MyHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                # 重写日志，输出到文件
                try:
                    with open(log_path, 'a', encoding='utf-8') as f:
                        f.write(f"[HTTP] {format % args}\n")
                except:
                    pass

            def translate_path(self, path):
                # 去除查询参数
                path = path.split('?', 1)[0]
                path = path.lstrip('/')
                if '..' in path or path.startswith('/'):
                    return os.path.join(project_root, '')
                return os.path.join(project_root, path)

            def do_GET(self):
                try:
                    # API: 获取音乐列表
                    if self.path == '/api/music':
                        music_dir = os.path.join(project_root, 'music')
                        files = []
                        if os.path.exists(music_dir):
                            for f in os.listdir(music_dir):
                                if f.lower().endswith(('.mp3', '.wav', '.ogg')):
                                    files.append(f)
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps(files).encode('utf-8'))
                        return

                    # API: 音频文件
                    if self.path.startswith('/music/'):
                        filename = unquote(self.path[7:])
                        filepath = os.path.join(project_root, 'music', filename)
                        if not os.path.exists(filepath):
                            filepath = os.path.join(project_root, 'Large_Music', filename)
                        if os.path.exists(filepath):
                            self.send_response(200)
                            ext = os.path.splitext(filename)[1].lower()
                            mime = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg'}.get(ext,
                                                                                                        'application/octet-stream')
                            self.send_header('Content-Type', mime)
                            self.end_headers()
                            with open(filepath, 'rb') as f:
                                self.wfile.write(f.read())
                            return
                        self.send_response(404)
                        self.end_headers()
                        return

                    # API: 打开文件夹
                    if self.path.startswith('/open_folder?'):
                        parsed = urlparse(self.path)
                        params = parse_qs(parsed.query)
                        folder = params.get('path', [''])[0]
                        if folder:
                            abs_path = os.path.join(project_root, folder)
                            if os.path.exists(abs_path):
                                try:
                                    if sys.platform == 'win32':
                                        os.startfile(abs_path)
                                    else:
                                        subprocess.Popen(['explorer', abs_path])
                                    self.send_response(200)
                                    self.send_header('Content-Type', 'application/json')
                                    self.end_headers()
                                    self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))
                                    return
                                except Exception as e:
                                    self.send_response(500)
                                    self.end_headers()
                                    self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))
                                    return
                            else:
                                self.send_response(404)
                                self.end_headers()
                                self.wfile.write(json.dumps({'error': 'Folder not found'}).encode('utf-8'))
                                return
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': 'Missing path'}).encode('utf-8'))
                        return

                    # API: 安装鼠标主题
                    if self.path.startswith('/install_theme?'):
                        parsed = urlparse(self.path)
                        params = parse_qs(parsed.query)
                        folder = params.get('path', [''])[0]
                        if not folder:
                            self.send_response(400)
                            self.end_headers()
                            self.wfile.write(json.dumps({'error': 'Missing path'}).encode('utf-8'))
                            return
                        abs_path = os.path.join(project_root, folder)
                        if not os.path.exists(abs_path):
                            self.send_response(404)
                            self.end_headers()
                            self.wfile.write(json.dumps({'error': 'Folder not found'}).encode('utf-8'))
                            return
                        inf_files = [f for f in os.listdir(abs_path) if f.lower().endswith('.inf')]
                        if not inf_files:
                            self.send_response(404)
                            self.end_headers()
                            self.wfile.write(
                                json.dumps({'error': 'No .inf file found in theme folder'}).encode('utf-8'))
                            return
                        inf_file = None
                        for f in inf_files:
                            if f == "右键安装.inf":
                                inf_file = f
                                break
                        if not inf_file:
                            inf_file = inf_files[0]
                        inf_full = os.path.join(abs_path, inf_file)
                        try:
                            import ctypes
                            ret = ctypes.windll.shell32.ShellExecuteW(
                                None, "install", inf_full, None, abs_path, 1
                            )
                            if ret > 32:
                                self.send_response(200)
                                self.send_header('Content-Type', 'application/json')
                                self.end_headers()
                                self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))
                            else:
                                error_code = ctypes.GetLastError()
                                self.send_response(500)
                                self.end_headers()
                                self.wfile.write(
                                    json.dumps({'error': f'ShellExecute failed, error code: {error_code}'}).encode(
                                        'utf-8')
                                )
                        except Exception as e:
                            self.send_response(500)
                            self.end_headers()
                            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))
                        return

                    # 其他静态文件：调用父类
                    super().do_GET()
                except Exception as e:
                    # 捕获所有异常，记录日志并返回 500
                    log_error(f"do_GET 异常: {traceback.format_exc()}")
                    try:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(b"Internal Server Error")
                    except:
                        pass

        # 自动选择端口
        port = 8765
        max_attempts = 30
        httpd = None
        for attempt in range(max_attempts):
            try:
                httpd = socketserver.TCPServer(("", port), MyHandler)
                break
            except OSError:
                port += 1
        if httpd is None:
            QMessageBox.critical(self, "服务器启动失败", "无法在可用端口启动 HTTP 服务，请检查防火墙或端口占用。")
            return

        def serve():
            try:
                httpd.serve_forever()
            except Exception as e:
                log_error(f"serve_forever 异常: {traceback.format_exc()}")

        threading.Thread(target=serve, daemon=True).start()

        webbrowser.open(f"http://localhost:{port}/html/tools.html")

    def play_music2(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file2)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def play_music3(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file3)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def play_music4(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file4)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def stop_music(self):
        self.changeToStandby()
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
        self.music = None

    def change_GUI_to_Large(self):
        self._set_pet_size("Large", save=True)

    def change_GUI_to_normal(self):
        self._set_pet_size("normal", save=True)

    def change_GUI_to_medium(self):
        self._set_pet_size("medium", save=True)

    def change_GUI_to_small(self):
        self._set_pet_size("small", save=True)

    # ====================== 自定义物品 / 时装 ======================
    def _custom_asset_dir(self):
        d = os.path.join(get_writable_dir(), 'custom_assets')
        os.makedirs(d, exist_ok=True)
        return d

    def _custom_server_url(self, rel):
        if not rel:
            return None
        if rel.startswith('http'):
            return rel
        host = getattr(self, 'server_host', '127.0.0.1') or '127.0.0.1'
        port = getattr(self, 'server_web_port', 8080) or 8080
        return f"http://{host}:{port}{rel}"

    def _web_port_alive(self):
        """快速探测服务端 Web 端口是否可达，避免下载长时间阻塞界面"""
        try:
            port = int(getattr(self, 'server_web_port', 0) or 0)
            host = getattr(self, 'server_host', '') or ''
            if not port or not host:
                return False
            import socket as _socket
            with _socket.create_connection((host, port), timeout=0.6):
                return True
        except Exception:
            return False

    def _asset_registry_path(self):
        return os.path.join(self._custom_asset_dir(), '.integrity.json')

    def _asset_registry(self):
        try:
            with open(self._asset_registry_path(), 'r', encoding='utf-8') as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _asset_register(self, dest, blob=None):
        """登记自定义资源的哈希（之后用它发现本地被改动）"""
        try:
            if blob is None:
                with open(dest, 'rb') as f:
                    blob = f.read()
            rel = os.path.relpath(dest, self._custom_asset_dir()).replace('\\', '/')
            reg = self._asset_registry()
            reg[rel] = hashlib.sha256(blob).hexdigest()
            with open(self._asset_registry_path(), 'w', encoding='utf-8') as f:
                json.dump(reg, f, ensure_ascii=False)
        except Exception as e:
            print(f"[完整性] 登记资源失败: {e}")

    def _asset_ok(self, dest):
        """本地资源是否与登记的一致；被改动/登记缺失时删掉它（下次会自动重下）"""
        try:
            if not os.path.exists(dest):
                return False
            rel = os.path.relpath(dest, self._custom_asset_dir()).replace('\\', '/')
            want = self._asset_registry().get(rel)
            if not want:
                return True            # 没有登记记录：按原逻辑放行（保持兼容）
            with open(dest, 'rb') as f:
                got = hashlib.sha256(f.read()).hexdigest()
            if got == want:
                return True
            print(f"[完整性] 本地资源被改动，已删除待重下: {rel}")
            os.remove(dest)
            return False
        except Exception:
            return True

    def _download_custom_asset(self, rel, dest):
        """下载服务端自定义资源到本地（已存在且未被改动则跳过）"""
        if not rel:
            return False
        if os.path.exists(dest) and self._asset_ok(dest):
            return True
        if os.path.exists(dest):
            try:
                os.remove(dest)        # 被改动/校验失败：删掉重下
            except Exception:
                pass
        url = self._custom_server_url(rel)
        if not url:
            return False
        if not self._web_port_alive():
            return False
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            req = urllib.request.Request(url, headers={'User-Agent': 'FireflyPet'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                blob = resp.read()
            with open(dest, 'wb') as f:
                f.write(blob)
            self._asset_register(dest, blob)
            return True
        except Exception as e:
            print(f"[自定义资源] 下载失败 {url}: {e}")
            return False

    def _custom_items_cache_path(self):
        return os.path.join(get_writable_dir(), 'custom_items_cache.json')

    def cache_custom_items(self, items):
        """把服务端下发的物品定义缓存到本地：单机/离线时靠它显示名称与图片"""
        try:
            if not isinstance(items, list):
                return
            with open(self._custom_items_cache_path(), 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False)
        except Exception as e:
            print(f"[物品缓存] 写入失败: {e}")
        # 顺手把物品图片也拉下来：背包/邮件一开就能显示真图，不用等点开菜单现下
        self.sync_custom_item_images()

    def sync_custom_item_images(self, items=None):
        """预取所有自定义物品的展示图到 custom_assets/img/<id>_<ver>.png。

        放在登录/热更新/商店同步时做（那时网络与端口状态最可靠），
        这样背包里永远不会因为"当时没下到"而退化成 emoji。
        """
        try:
            if self.mp_client is not None and not getattr(self.mp_client, 'connected', False):
                return
        except Exception:
            pass
        try:
            for it in (items if isinstance(items, list) else self.known_custom_items()):
                if not isinstance(it, dict) or not it.get('id'):
                    continue
                try:
                    # ver 没变就直接命中缓存，不会重复联网
                    self.custom_item_image_path(it)
                except Exception:
                    continue
        except Exception as e:
            print(f"[物品图片] 预取失败: {e}")

    def known_custom_items(self):
        """当前可用的物品定义：优先服务端下发的，其次本地缓存（单机模式）"""
        try:
            live = getattr(self, 'server_custom_items', None)
            if isinstance(live, list) and live:
                return live
        except Exception:
            pass
        try:
            with open(self._custom_items_cache_path(), 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if isinstance(cached, list):
                return cached
        except Exception:
            pass
        return []

    def custom_item_name(self, item_id):
        """自定义物品的名称（找不到就用 id，绝不显示成空）"""
        try:
            _iid = str(item_id or '')
            for it in self.known_custom_items():
                if it.get('id') == _iid:
                    return it.get('name') or _iid
        except Exception:
            pass
        return str(item_id or '')

    def item_icon_path(self, item_type, item_id=None, info=None):
        """物品的真实图片路径（本地没有就返回 ''）——背包/邮件用「图片+文本」显示。

        内置物品用客户端自带图；自定义物品用已下载的展示图；
        取不到时调用方回退 emoji+文本。
        """
        try:
            t = str(item_type or '')
            if t in ('cake', 'balance'):
                p = os.path.join(get_base_dir(), 'assets', 'images', 'foods', 'Cake.png')
                return p if os.path.exists(p) else ''
            if t in ('star_rail_ticket', 'ticket', 'star_rail_tickets'):
                p = os.path.join(get_base_dir(), 'assets', 'images', 'star_rail_ticket.png')
                return p if os.path.exists(p) else ''
            if t == 'credit':
                # 用户指定：信用点用自带的 assets/images/Credit/Item_Credit.png
                for p in (os.path.join(get_base_dir(), 'assets', 'images', 'Credit', 'Item_Credit.png'),
                          os.path.join(get_base_dir(), 'gacha_base', 'imgs', 'credit.png')):
                    if os.path.exists(p):
                        return p
                return ''
            if t == 'jade':
                for p in (os.path.join(get_base_dir(), 'assets', 'images', 'Jade', 'Item_Jade.png'),
                          os.path.join(get_base_dir(), 'gacha_base', 'imgs', 'stellar_jade.png'),
                          os.path.join(get_base_dir(), 'gacha_base', 'imgs', 'jade.png')):
                    if os.path.exists(p):
                        return p
                return ''
            # 自定义物品（服务端新增）：type 可能是 item / clothes / custom ——
            # 以前只认 custom/clothes，服务端下发的 type='item' 一律解析失败，
            # 背包里的「古老梦华」就一直显示成原 emoji。现在只要有 id 就按 id 找图片
            # （内置的 cake / credit / 专票 / 星琼 上面已经 return 了）
            if item_id:
                _iid = str(item_id)
                _info = info if isinstance(info, dict) and info else next(
                    (x for x in self.known_custom_items() if x.get('id') == _iid), None)
                # ① 版本化缓存（登录/热更新时已预取，命中就不联网）
                try:
                    _ver = (_info or {}).get('ver') or 0
                    p = os.path.join(self._custom_asset_dir(), 'img', '%s_%s.png' % (_iid, _ver))
                    if os.path.exists(p):
                        return p
                except Exception:
                    pass
                # ② 没有就现下（按物品自己的 image 字段，ver 变即失效）
                if _info:
                    try:
                        p = self.custom_item_image_path(_info)
                        if p and os.path.exists(p):
                            return p
                    except Exception:
                        pass
                # ③ 再兜一层：不管 ver 直接找该物品的最新缓存图
                #    ⚠️ 这里绝不能去 server/<码>/shop/self_area_img 找 ——
                #    那是"商店物品图标"（商品可能挂的是抽到展示图），
                #    背包要的是物品自己上传的那张（用户反馈过用错图）。
                try:
                    _dir = os.path.join(self._custom_asset_dir(), 'img')
                    _hits = [f for f in os.listdir(_dir)
                             if f.startswith(_iid + '_') and f.endswith('.png')]
                    if _hits:
                        _hits.sort(key=lambda f: os.path.getmtime(os.path.join(_dir, f)))
                        return os.path.join(_dir, _hits[-1])
                except Exception:
                    pass
        except Exception as e:
            print(f"[图标] 解析失败({item_type}/{item_id}): {e}")
        return ''

    def _srv_shop_asset_dir(self):
        """server/<服务器码>/shop/self_area_img 目录（取不到返回 ''）

        只给商店窗口算路径用；**背包/邮件不要用它**（第138批：商店目录里可能是
        抽到展示图，物品图必须用物品自己上传的那张）。商店窗口没开时用
        ShopMainWindow 的临时实例算（纯路径计算，不做 UI）。
        """
        try:
            helper = getattr(self, 'shop_window', None)
            if helper is not None and hasattr(helper, 'shop_asset_dir'):
                return helper.shop_asset_dir() or ''
            tmp = ShopMainWindow.__new__(ShopMainWindow)
            tmp.firefly = self
            return ShopMainWindow.shop_asset_dir(tmp) or ''
        except Exception:
            return ''

    def custom_item_image_path(self, item):
        """确保商店商品图片已下载，返回本地路径或 None（按 ver 版本化，换图即失效）"""
        rel = item.get('image')
        if not rel:
            return None
        ver = item.get('ver') or 0
        dest = os.path.join(self._custom_asset_dir(), 'img', f"{item.get('id')}_{ver}.png")
        if self._download_custom_asset(rel, dest):
            # 清理该物品的旧版本图片，避免缓存目录无限增长
            try:
                _dir = os.path.join(self._custom_asset_dir(), 'img')
                for _f in os.listdir(_dir):
                    if _f.startswith(f"{item.get('id')}_") and _f != f"{item.get('id')}_{ver}.png":
                        os.remove(os.path.join(_dir, _f))
            except Exception:
                pass
            return dest
        return None

    def ensure_custom_clothes(self, item):
        """下载自定义时装的四尺寸 GIF，返回可用的 source_dir 或 None（按 ver 版本化）"""
        cid = item.get('id')
        gifs = item.get('gifs') or {}
        if not cid:
            return None
        ver = item.get('ver') or 0
        ckey = f'{cid}_{ver}'
        base = os.path.join(get_writable_dir(), 'clothes_custom', ckey, 'assets',
                            'images', 'firefly', 'actions', 'Standby')
        rel_dir = f'clothes_custom/{ckey}/assets'
        for key, rel in gifs.items():
            self._download_custom_asset(rel, os.path.join(base, f'{key}.gif'))
        if not (os.path.isdir(base) and os.listdir(base)):
            return None
        # 清理同一时装的其他版本目录
        try:
            _root = os.path.join(get_writable_dir(), 'clothes_custom')
            for _d in os.listdir(_root):
                if _d != ckey and _d.startswith(f'{cid}_') and os.path.isdir(os.path.join(_root, _d)):
                    shutil.rmtree(os.path.join(_root, _d), ignore_errors=True)
        except Exception:
            pass
        return rel_dir

    def sync_custom_items(self):
        """把服务端下发的自定义物品/时装登记到本地（时装可穿戴、商店可购买）"""
        if self.server_custom_items is None:
            # 状态未知（例如尚未收到服务端数据），不做任何注销
            return
        try:
            _seen = set()
            for it in (self.server_custom_items or []):
                # 单个条目异常不影响其余物品的登记
                try:
                    if not isinstance(it, dict):
                        continue
                    if (it.get('type') or 'item') != 'clothes':
                        continue
                    cid = it.get('id')
                    if not cid:
                        continue
                    src = self.ensure_custom_clothes(it)
                    AVAILABLE_CLOTHES[cid] = {
                        'name': it.get('name') or cid,
                        'source_dir': src or f'clothes_custom/{cid}/assets',
                        'description': it.get('description') or '自定义时装',
                        'custom': True,
                    }
                    _seen.add(cid)
                except Exception as _e:
                    print(f"[自定义物品] 跳过异常条目 {it!r}: {_e}")
            # 服务端已删除的自定义时装：注销登记并清理本地资源，避免陈旧时装仍可穿戴
            for _old in list(getattr(self, '_custom_clothes_ids', set())):
                if _old in _seen:
                    continue
                AVAILABLE_CLOTHES.pop(_old, None)
                self._custom_clothes_ids.discard(_old)
                try:
                    _root = os.path.join(get_writable_dir(), 'clothes_custom')
                    for _d in os.listdir(_root):
                        if _d == _old or _d.startswith(f'{_old}_'):
                            shutil.rmtree(os.path.join(_root, _d), ignore_errors=True)
                except Exception:
                    pass
                # 正在穿这件被删除的时装：回退普通时装
                try:
                    if self.current_user and self.current_user.get('current_clothes') == _old:
                        self.current_user['current_clothes'] = 'normal'
                        self._save_current_user()
                        self.apply_clothes('normal', silent=True)
                except Exception:
                    pass
            self._custom_clothes_ids = _seen
            if getattr(self, 'shop_window', None) is not None:
                try:
                    self.shop_window.reload_custom_goods()
                except Exception:
                    pass
        except Exception as e:
            print(f"[自定义物品] 同步失败: {e}")

    def apply_current_clothes(self):
        """启动时或库存更新后，检查并应用当前有效的时装（静默）"""
        if not self.current_user:
            return
        # 获取当前库存
        if hasattr(self, 'mp_client') and self.mp_client.connected and self.online_inventory is not None:
            inv = self.online_inventory
        else:
            inv = self.current_user
        owned = inv.get('owned_clothes', ['normal'])
        current = self.current_user.get('current_clothes', 'normal')
        # 自定义时装：本地资源缺失时视为不可用，回退普通时装（否则会静默失败）
        _cinfo = AVAILABLE_CLOTHES.get(current)
        if _cinfo and _cinfo.get('custom'):
            _src = os.path.join(get_base_dir(), _cinfo.get('source_dir') or '')
            _alt = os.path.join(get_writable_dir(), _cinfo.get('source_dir') or '')
            if not os.path.exists(_src) and not os.path.exists(_alt):
                current = 'normal'
                self.current_user['current_clothes'] = 'normal'
                self._save_current_user()
        # 检查是否有效
        if current not in owned or current not in AVAILABLE_CLOTHES:
            # 无效，切换到 normal
            if 'normal' not in owned:
                owned.append('normal')
                if hasattr(self, 'mp_client') and self.mp_client.connected and self.online_inventory is not None:
                    self.online_inventory['owned_clothes'] = owned
                    self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
                else:
                    self.current_user['owned_clothes'] = owned
                    self._save_current_user()
            current = 'normal'
            self.current_user['current_clothes'] = 'normal'
            self._save_current_user()
        # 静默应用时装
        self.apply_clothes(current, silent=True)

    def apply_clothes(self, clothes_id, silent=False):
        info = AVAILABLE_CLOTHES.get(clothes_id)
        if not info:
            if not silent:
                self.show_centered_message("错误", f"未知时装ID: {clothes_id}", QMessageBox.Warning)
            return
        source_dir = os.path.join(get_base_dir(), info["source_dir"])
        if not os.path.exists(source_dir):
            # 自定义时装存放在可写目录
            _alt = os.path.join(get_writable_dir(), info["source_dir"])
            if os.path.exists(_alt):
                source_dir = _alt
        target_dir = get_assets_overlay_dir()
        if not os.path.exists(source_dir):
            if not silent:
                self.show_centered_message("错误", f"时装资源目录不存在：{source_dir}\n请确保程序目录下有该文件夹。",
                                           QMessageBox.Warning)
            return
        try:
            if clothes_id == "normal":
                # 普通时装即内置默认资源：清空覆盖目录即可完整还原默认形象
                if os.path.isdir(target_dir):
                    shutil.rmtree(target_dir)
            else:
                for root, dirs, files in os.walk(source_dir):
                    relative_path = os.path.relpath(root, source_dir)
                    target_root = os.path.join(target_dir, relative_path)
                    os.makedirs(target_root, exist_ok=True)
                    for file in files:
                        src_file = os.path.join(root, file)
                        dst_file = os.path.join(target_root, file)
                        shutil.copy2(src_file, dst_file)
            if not silent:
                self.show_centered_message("成功", f"时装已切换为：{info['name']}")
            self.current_user["current_clothes"] = clothes_id
            self._save_current_user()
            self._refresh_animation_after_clothes_change()
        except Exception as e:
            if not silent:
                self.show_centered_message("错误", f"切换失败：{str(e)}", QMessageBox.Warning)

    def _refresh_animation_after_clothes_change(self):
        """切换时装后重新加载当前动画"""
        state = self.current_animation_state
        if state == "Standby":
            self.changeToStandby()
        elif state == "Discomfort":
            self.changeToDiscomfort()
        elif state == "Sleep":
            self.sleep()  # 重新播放睡眠动画
        else:
            # 处于 eat/love/sing 等临时状态，恢复待机
            self.changeToStandby()

    def copy_normal(self):
        self.apply_clothes("normal")

    def copy_new_year(self):
        self.apply_clothes("26_Newyear")

    # ====================== 用户数据操作 ======================
    def _save_current_user(self):
        """
        保存当前用户数据到本地文件，并在联机时上报统计数据到服务端。
        调用时机：任何用户数据（包括统计字段）发生变化后。
        """
        if not self.current_user:
            return

        userid = self.current_user.get("userid")
        if not userid:
            print("[ERROR] 当前用户缺少 userid，无法保存")
            return

        # ---------- 1. 保存到本地文件 ----------
        filepath = os.path.join(USER_DATA_DIR, f"{userid}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(self.current_user, f, ensure_ascii=False, indent=2)
            print(f"[DEBUG] 用户数据已保存到 {filepath}")
        except Exception as e:
            print(f"[ERROR] 保存用户数据失败: {e}")
            return

        if self.mp_client.connected:
            self._check_and_grant_achievement()

        # ---------- 2. 联机时上报统计数据到服务端 ----------
        if self.mp_client.connected and self.current_user:
            try:
                # 构建统计字典（与服务器字段对齐）
                stats = {
                    'total_interactions': self.current_user.get('total_interactions', 0),
                    'total_open_count': self.current_user.get('total_open_count', 0),
                    'total_sign_days': self.current_user.get('total_sign_days', 0),
                    'total_affection_up': self.current_user.get('total_affection_up', 0),
                    'total_sleep_count': self.current_user.get('total_sleep_count', 0),
                    'total_chat_count': self.current_user.get('total_chat_count', 0),
                    'monthly_interactions': self.current_user.get('monthly_interactions', 0),
                    'monthly_open_count': self.current_user.get('monthly_open_count', 0),
                    'monthly_sign_days': self.current_user.get('monthly_sign_days', 0),
                    'monthly_affection_up': self.current_user.get('monthly_affection_up', 0),
                    'monthly_sleep_count': self.current_user.get('monthly_sleep_count', 0),
                    'monthly_chat_count': self.current_user.get('monthly_chat_count', 0),
                    'monthly_credit_gained': self.current_user.get('monthly_credit_gained', 0),
                    'monthly_cake_gained': self.current_user.get('monthly_cake_gained', 0),
                    'current_month': self.current_user.get('current_month', None),
                }
                # 发送到服务端
                self.mp_client._send({'type': 'update_stats', 'data': {'stats': stats}})
                # 更新本地缓存（供报告使用）
                self.online_stats = stats
                print("[DEBUG] 统计数据已上报服务器")
            except Exception as e:
                print(f"[ERROR] 上报统计数据失败: {e}")

    def find_user_by_identifier(self, identifier):
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except:
                    continue
                if not isinstance(data, dict):
                    continue
                if (data.get("username") == identifier or
                        data.get("userid") == identifier or
                        data.get("uid") == identifier):
                    return data
        return None

    def find_users_by_identifiers(self, identifier):
        identifier = str(identifier).strip()
        if not identifier:
            return []
        matched_users = []
        for filename in os.listdir(USER_DATA_DIR):
            if filename == os.path.basename(CODES_FILE):
                continue
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(USER_DATA_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    user = json.load(f)
            except:
                continue
            if not isinstance(user, dict):
                continue
            name_match = str(user.get("name", "")).strip() == identifier
            id_match = str(user.get("userid", "")).strip() == identifier
            uid_match = str(user.get("uid", "")).strip() == identifier
            if name_match or id_match or uid_match:
                matched_users.append(user)
        return matched_users

    def save_user_data(self, user_data):
        userid = user_data.get("userid")
        if userid:
            filepath = os.path.join(USER_DATA_DIR, f"{userid}.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(user_data, f, ensure_ascii=False, indent=2)

    def send_mail_to_user(self, target_user, title, content, items):
        """
        发送邮件给目标用户（单机/联机通用）
        - 联机模式：通过服务端发送
        - 单机模式：直接添加到 target_user 的 mailbox
        """
        if self.mp_client.connected:
            # 联机模式：通过服务端发送
            target_uid = target_user.get('server_uid') or target_user.get('uid')
            if not target_uid:
                print("联机模式发邮件：缺少目标uid")
                return
            self.mp_client._send({
                'type': 'send_mail',
                'data': {
                    'target_uid': target_uid,
                    'title': title,
                    'content': content,
                    'items': items
                }
            })
        else:
            # 单机模式：直接添加到目标用户的 mailbox
            if 'mailbox' not in target_user:
                target_user['mailbox'] = []
            mail = {
                'id': str(uuid.uuid4()),  # 生成唯一ID（可选）
                'sender': '系统',
                'title': title,
                'content': content,
                'timestamp': datetime.now().isoformat(),
                'items': items,
                'read': False,
                'claimed': False
            }
            target_user['mailbox'].append(mail)
            # 保存目标用户数据
            if target_user.get('userid') == self.current_user.get('userid'):
                self._save_current_user()
            else:
                self.save_user_data(target_user)

    def show_centered_message(self, title, text, icon=QMessageBox.Information, buttons=QMessageBox.Ok):
        """
        显示一个居中的深色主题消息框，支持自定义图标和按钮。
        返回用户点击的按钮（如 QMessageBox.Yes/No 等）。
        """
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText(text)
        msg.setIcon(icon)
        msg.setStandardButtons(buttons)
        msg.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #888888;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton[text="No"] {
                background-color: #666666;
            }
            QPushButton[text="No"]:hover {
                background-color: #888888;
            }
        """)
        # 对 Yes/No 按钮单独调整颜色（可选）
        if buttons == (QMessageBox.Yes | QMessageBox.No):
            yes_btn = msg.button(QMessageBox.Yes)
            if yes_btn:
                yes_btn.setStyleSheet("background-color: #F44336;")
            no_btn = msg.button(QMessageBox.No)
            if no_btn:
                no_btn.setStyleSheet("background-color: #4CAF50;")
        # 居中于屏幕
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg.move(screen_center - msg.rect().center())
        return msg.exec_()

    # ---------- 信用点相关 ----------
    def give_credit(self, amount, target_identifiers_str=None):
        try:
            amount = int(amount)
            if amount <= 0 or amount > 99999999:
                raise ValueError
        except:
            self.show_centered_message("错误", "数量必须是 1-99999999 的整数", QMessageBox.Warning)
            return False
        if target_identifiers_str:
            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False
                current_uid = self.current_user.get("uid", "")
                is_current_admin = current_uid.lower().startswith("admin")
                for tu in target_users:
                    if is_current_admin and tu["uid"].lower().startswith("admin") and tu["userid"] != self.current_user["userid"]:
                        self.show_centered_message("错误", f"不能操作其他管理员用户: {tu['username']}", QMessageBox.Warning)
                        return False
                for tu in target_users:
                    self.send_mail_to_user(tu, "管理员赠送信用点",
                                           f"管理员 {self.current_user['username']} 赠送了 {amount} 信用点。",
                                           [{"type": "credit", "amount": amount}])
                self.show_centered_message("成功", f"已向 {len(target_users)} 个用户发送信用点邮件", QMessageBox.Information)
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return False
        else:
            self.send_mail_to_user(self.current_user, "管理员添加信用点",
                                   f"给自己添加了 {amount} 信用点。",
                                   [{"type": "credit", "amount": amount}])
            self.show_centered_message("成功", "已给自己发送信用点邮件", QMessageBox.Information)
        return True

    def set_credit(self, amount, target_identifiers_str=None):
        try:
            amount = int(amount)
            if amount < 0 or amount > 99999999:
                raise ValueError
        except:
            self.show_centered_message("错误", "数量必须是 0-99999999 的整数", QMessageBox.Warning)
            return False
        if target_identifiers_str:
            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False
                for tu in target_users:
                    tu["credit"] = amount
                    self.save_user_data(tu)
                self.show_centered_message("成功", f"已设置 {len(target_users)} 个用户的信用点", QMessageBox.Information)
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return False
        else:
            self.current_user["credit"] = amount
            self._save_current_user()
            self.show_centered_message("成功", "已设置自己的信用点", QMessageBox.Information)
        return True

    # ---------- 签到 ----------
    def sign_in(self):
        if self.mp_client.connected:
            self.mp_client._send({'type': 'sign_in', 'data': {}})
            return

        # ---------- 单机签到逻辑（原有代码） ----------
        now = datetime.now()
        today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if now < today_4am:
            reset_time = today_4am - timedelta(days=1)
        else:
            reset_time = today_4am

        last_sign = (self.current_user or {}).get("last_sign_date")
        if last_sign:
            try:
                last_sign_dt = datetime.fromisoformat(last_sign)
                if last_sign_dt >= reset_time:
                    self.show_centered_message("提示", "今日已经签到过了，凌晨4点后再来哦！", QMessageBox.Warning)
                    self.create_menu()
                    return
            except:
                pass

        uid = self.current_user.get("uid", "").lower()
        is_trailblazer = uid.startswith("trailblazer")
        base_credit = 36000 if is_trailblazer else 7200
        self.current_user["credit"] = self.current_user.get("credit", 0) + base_credit
        if self.current_user["credit"] > 99999999:
            self.current_user["credit"] = 99999999

        extra_msg = ""
        today_date = now.date()
        try:
            lunar = LunarDate.fromSolarDate(today_date.year, today_date.month, today_date.day)
            if today_date.year == 2026 and lunar.month == 1 and 1 <= lunar.day <= 15:
                self.current_user["credit"] = self.current_user.get("credit", 0) + 888
                if self.current_user["credit"] > 99999999:
                    self.current_user["credit"] = 99999999
                owned = self.current_user.get("owned_clothes", ["normal"])
                if "26_Newyear" not in owned:
                    owned.append("26_Newyear")
                    self.current_user["owned_clothes"] = owned
                    extra_msg = "，并获得新春时装[柿柿如意] + 888信用点"
                else:
                    extra_msg = "，并获得888信用点（已有时装不再赠送）"
        except:
            pass

        self.current_user["last_sign_date"] = now.isoformat()
        self._save_current_user()

        history = self.current_user.get("redeem_history", [])
        history.append(f"{now.strftime('%Y-%m-%d %H:%M:%S')} - 签到获得 {base_credit} 信用点{extra_msg}")
        self.current_user["redeem_history"] = history
        self._save_current_user()

        # === 新增统计 ===
        self.check_month_reset()
        self.current_user["total_sign_days"] += 1
        self.current_user["monthly_sign_days"] += 1
        self.current_user["total_interactions"] += 1
        self.current_user["monthly_interactions"] += 1
        self.current_user["monthly_credit_gained"] += base_credit
        self._save_current_user()

        self.create_menu()
        self.show_centered_message("成功", f"签到成功！获得 {base_credit} 信用点{extra_msg}。", QMessageBox.Information)

    def is_signed_today(self):
        if self.mp_client.connected:
            # 联机时以服务端状态为准
            return self.online_signed_today

        # ---------- 单机模式（原有代码） ----------
        last_sign = (self.current_user or {}).get("last_sign_date")
        if not last_sign:
            return False
        try:
            last_sign_dt = datetime.fromisoformat(last_sign)
            now = datetime.now()
            today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if now < today_4am:
                reset_time = today_4am - timedelta(days=1)
            else:
                reset_time = today_4am
            return last_sign_dt >= reset_time
        except:
            return False

    def open_shop(self):
        if self._ui_locked():
            return
        """打开商店窗口，并保存引用。
        只有"仍然可见"的窗口才复用；已关闭/已销毁的一律重建
        （修复：热更新后关闭商店，再点商店打不开）"""
        if self.shop_window is not None:
            try:
                if self.shop_window.isVisible():
                    # 已可见的窗口只 raise 不会触发 showEvent：这里补一次状态/数据刷新，
                    # 否则登录前后打开过商店会一直停在旧的联机状态
                    try:
                        self.shop_window.refresh_online_state()
                    except Exception:
                        pass
                    self.shop_window.raise_()
                    self.shop_window.activateWindow()
                    return
            except Exception:
                pass
            # 不可见或已销毁：清引用后重建
            try:
                self.shop_window.close()
            except Exception:
                pass
            self.shop_window = None
        try:
            shop = ShopMainWindow(self, self)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.show_centered_message("错误", f"商店打开失败：{e}", QMessageBox.Warning)
            self.shop_window = None
            return
        self.shop_window = shop
        shop.finished.connect(self._on_shop_closed)  # 窗口关闭时清空引用
        try:
            shop.sync_shop_images()
        except Exception:
            pass
        shop.open()

    def _on_shop_closed(self):
        """商店窗口关闭时的回调"""
        self.shop_window = None

    def _close_all_windows(self):
        """关闭所有可能打开的子窗口（商店、聊天、邮箱、历史记录等），但保留 QMessageBox 弹窗"""
        # 关闭商店
        if self.shop_window:
            try:
                self.shop_window.close()
                self.shop_window = None
            except Exception:
                pass

        # 关闭聊天窗口
        if self.chat_window:
            try:
                self.chat_window.close()
                self.chat_window = None
            except Exception:
                pass

        # 关闭在线列表悬浮窗
        if self.online_overlay:
            try:
                self.online_overlay.hide()
            except Exception:
                pass

        # 关闭所有模态对话框（除 QMessageBox 外）
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QDialog) and not isinstance(widget, QMessageBox):
                if widget.isVisible():
                    try:
                        widget.close()
                    except Exception:
                        pass
            # 如果是 QMessageBox，保留它（比如踢出弹窗）

    def give_item(self, amount, item_type, target_identifiers_str=None, clothes_id=None):
        if item_type not in ("cake", "clothes"):
            self.show_centered_message("错误", f"未知物品类型: {item_type}", QMessageBox.Warning)
            return False
        if item_type == "clothes" and not clothes_id:
            self.show_centered_message("错误", "时装必须指定 clothes_id", QMessageBox.Warning)
            return False
        try:
            amount = int(amount)
            if amount <= 0 or amount > 9999:
                raise ValueError
        except:
            self.show_centered_message("错误", "数量必须是 1-9999 的整数", QMessageBox.Warning)
            return False

        items = []
        if item_type == "cake":
            items.append({"type": "cake", "amount": amount})
        else:
            items.append({"type": "clothes", "id": clothes_id})

        if target_identifiers_str:
            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False
                current_uid = self.current_user.get("uid", "")
                is_current_admin = current_uid.lower().startswith("admin")
                for tu in target_users:
                    if is_current_admin and tu["uid"].lower().startswith("admin") and tu["userid"] != self.current_user["userid"]:
                        self.show_centered_message("错误", f"不能操作其他管理员用户: {tu['username']}", QMessageBox.Warning)
                        return False
                for tu in target_users:
                    self.send_mail_to_user(tu, "管理员赠送物品",
                                           f"管理员 {self.current_user['username']} 赠送了 {'橡木蛋糕卷' if item_type=='cake' else '时装['+AVAILABLE_CLOTHES[clothes_id]['name']+']'}。",
                                           items)
                self.show_centered_message("成功", f"已向 {len(target_users)} 个用户发送物品邮件", QMessageBox.Information)
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return False
        else:
            self.send_mail_to_user(self.current_user, "管理员添加物品",
                                   f"给自己添加了 {'橡木蛋糕卷' if item_type=='cake' else '时装['+AVAILABLE_CLOTHES[clothes_id]['name']+']'}。",
                                   items)
            self.show_centered_message("成功", "已给自己发送物品邮件", QMessageBox.Information)
        return True

    def show_join_dialog(self):
        """打开加入服务器对话框（连接逻辑由对话框内部处理）"""
        dlg = JoinServerDialog(self, self)
        dlg.exec_()  # 对话框内部会处理连接，无需额外操作

    class CountryFetcher(QThread):
        finished = pyqtSignal(str)

        def run(self):
            country = 'UNKNOWN'
            try:
                with urllib.request.urlopen('https://ipapi.co/country/', timeout=3) as resp:
                    country = resp.read().decode().strip()
                    if not country or len(country) != 2:
                        country = 'UNKNOWN'
            except Exception:
                try:
                    with urllib.request.urlopen('https://ipinfo.io/country', timeout=3) as resp:
                        country = resp.read().decode().strip()
                except Exception:
                    pass
            self.finished.emit(country)

    def join_server(self, host, port):
        """加入服务器（异步获取国家代码）"""
        # 手动进入联机：结束进行中的自动重连（用户自己接管连接）
        self._cancel_auto_reconnect()
        user = self.current_user
        self.local_inventory_backup = {
            'balance': user.get('balance', 0),
            'credit': user.get('credit', 0),
            'owned_clothes': user.get('owned_clothes', ['normal']),
            'star_rail_tickets': user.get('star_rail_tickets', 0)
        }
        self.connecting = True
        self.country_fetcher = self.CountryFetcher()
        self.country_fetcher.finished.connect(lambda country: self._do_connect(host, port, user, country))
        self.country_fetcher.start()

    def _do_connect(self, host, port, user, country):
        """实际执行连接（已获取到国家代码）"""
        self._last_country = country   # 自动重连时复用
        self.mp_client.connect_to_server(host, port, user['userid'], user['uid'], user['username'], country)
        # 可选：清除 fetcher 引用
        self.country_fetcher = None

    def disconnect_server(self, show_message=True):
        # 主动断开时结束进行中的自动重连
        self._cancel_auto_reconnect()
        self._close_gacha_window()
        if self._rename_dialog:
            try:
                self._rename_dialog.close()
            except:
                pass
            self._rename_dialog = None
        self._is_manual_disconnect = True
        self._suppress_disconnect_message = True
        self.banned = False
        self.is_admin = False
        self._waiting_for_name = False
        self.rename_required = False

        # 关闭所有子窗口
        self._close_all_windows()

        if self.mp_client.connected:
            if show_message:
                self.show_centered_message("提示", "已断开与服务器的连接", QMessageBox.Information)
            if self.online_inventory:
                self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
            self.mp_client.disconnect()
        # 恢复单机状态
        self._restore_single_player_state()
        # 删除备份文件
        backup_path = os.path.join(USER_DATA_DIR, 'single_state_backup.json')
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except:
                pass
        # 清理联机变量
        self.server_uid = None
        self.server_permission = 'user'
        self.online_uid = ''
        self.online_name = None
        self.online_redeemed_codes = []
        self.online_signed_today = False
        self._pending_mail_request = None
        self.ai_enabled = True
        self.ping_timer.stop()
        self.server_custom_menu = []
        self.server_custom_items = []
        self.create_menu()
        self.muted_until = None
        if self.chat_window:
            self.chat_window.update_mute_status(None)

    def update_bag_display(self):
        # 这个方法会被背包菜单、商店等调用，需要根据联机状态返回不同数据
        # 由于菜单是动态生成的，每次打开菜单时重新读取 balance/credit 即可
        # 这里只需重写菜单中获取背包数据的方式
        pass

    def clear_item(self, item_type, target_identifiers_str=None):
        """
        清空目标用户的特定物品（蛋糕卷或信用点）。
        支持 @s（自己）、单个用户名/用户ID，或逗号分隔的多个标识符（通过 find_users_by_identifiers 处理）。

        参数:
            item_type: 物品类型，支持 'cake'（橡木蛋糕卷）或 'credit'（信用点）
            target_identifiers_str: 用户标识符，可选。若为 None 或空，默认清空自己。
        """
        # 1. 验证物品类型
        if item_type not in ("cake", "credit"):
            self.show_centered_message("错误", f"当前仅支持清空 cake 或 credit，不支持 {item_type}", QMessageBox.Warning)
            return False

        # 2. 确定目标用户列表
        target_users = []
        if not target_identifiers_str or target_identifiers_str.strip() == "":
            # 默认为自己
            target_users = [self.current_user]
        elif target_identifiers_str.strip().lower() == "@s":
            target_users = [self.current_user]
        else:
            # 尝试按用户标识查找（支持逗号分隔）
            found = self.find_users_by_identifiers(target_identifiers_str)
            if found:
                target_users = found
            else:
                self.show_centered_message("错误", f"未找到用户 {target_identifiers_str}", QMessageBox.Warning)
                return False

        # 3. 清空物品并保存
        for user in target_users:
            if item_type == "cake":
                user["balance"] = 0
            elif item_type == "credit":
                user["credit"] = 0
            # 保存用户数据（单机/联机适配）
            if user.get("userid") == self.current_user.get("userid"):
                self._save_current_user()  # 更新当前用户内存并保存文件
            else:
                self.save_user_data(user)  # 保存其他用户

        # 4. 刷新当前用户界面
        self.update_bag_display()

        # 5. 提示成功
        self.show_centered_message("成功", f"已清空 {len(target_users)} 个用户的 {item_type}", QMessageBox.Information)
        return True

    def show_help(self):
        uid_prefix = self.current_user.get("uid", "").lower()
        is_admin = uid_prefix.startswith("admin") or (
                    self.mp_client.connected and self.mp_client.server_permission == 'admin')
        if is_admin:
            help_text = (
                "═══════════════════════════════════════════\n"
                "         流萤联机服务器 控制台帮助\n"
                "═══════════════════════════════════════════\n\n"
                "【服务器控制台专用命令】（仅在控制台有效）\n"
                "  /restart           重启服务器（断开所有玩家）\n"
                "  /close             关闭服务器（停止服务）\n"
                "  /change name <新名称>  修改服务器名称（最多15字符）\n"
                "  /fix on [消息]      开启维护模式并踢出所有玩家\n"
                "  /fix off            关闭维护模式\n"
                "  /All_Versions True/False  调试用，放行所有版本\n\n"
                "【常用管理命令】（客户端可用）\n"
                "  /give <用户> <物品id> <数量>\n"
                "      给指定玩家发放物品（邮件形式）\n"
                "      用户: @a(所有人) @s(自己) 或 玩家名\n"
                "      物品: cake, credit, ruyi(时装), ticket, jade, star_rail_tickets\n"
                "      示例: /give Gubwin credit 1000\n\n"
                "  /set <用户> <物品id> <数量>\n"
                "      直接设定玩家物品数量（覆盖）\n"
                "      示例: /set @s cake 50\n\n"
                "  /clear <用户> <物品或*>\n"
                "      清空指定玩家的物品（仅支持玩家名或@s）\n"
                "      物品: cake, credit, * (清空全部)\n"
                "      示例: /clear Gubwin cake\n\n"
                "  /revoke <用户> <物品:数量&物品...>\n"
                "      直接扣除玩家物品（允许负值，时装只能移除）\n"
                "      用户: @a(所有人) @s(自己) 或 玩家名\n"
                "      物品: cake, credit, ticket, 时装ID\n"
                "      示例: /revoke Gubwin cake:5&credit:1000\n"
                "      被扣除的玩家会收到邮件通知，不可对自己使用\n\n"
                "  /revoke code <兑换码> <用户>\n"
                "      撤销指定用户对兑换码的使用资格（撤回兑换码）\n"
                "      用户: 同 /revoke\n"
                "      示例: /revoke code GIFT2026 @a\n\n"
                "  /set newcode <兑换码> <物品列表>\n"
                "      创建新兑换码，物品列表用 & 分隔\n"
                "      格式: 物品:数量 (数量默认为1，上限9999)\n"
                "      示例: /set newcode 新春福利 cake:5&ruyi&credit:1000\n\n"
                "  /mute <用户> [global] [时长]\n"
                "      禁言玩家；省略时长=永久禁言\n"
                "      默认仅服内禁言；加 global 为全局禁言（同时禁止私信）\n"
                "      时长格式同封禁（如 30D、1H、2026-12-31）\n"
                "      示例: /mute Gubwin 1D   /mute Gubwin global 7D   /mute Gubwin global\n\n"
                "  /unmute <用户> [global]\n"
                "      解除禁言；加 global 解除全局禁言（含私信封禁）\n"
                "      示例: /unmute Gubwin   /unmute Gubwin global\n\n"
                "  /function AI on|off\n"
                "      启用或禁用服务器的 AI（在线对话）功能\n\n"
                "  /function Chat on|off\n"
                "      启用或禁用服务器的多人聊天功能\n\n"
                "【玩家管理】\n"
                "  /ban <玩家> [时长]         封禁玩家（时长如 30D, 2026-12-31）\n"
                "  /unban <玩家>              解封玩家\n"
                "  /op <玩家>                 提升为管理员\n"
                "  /deop <玩家>               取消管理员\n"
                "  /resetname <用户>          重置玩家的改名冷却时间\n\n"
                "【区域与时区封禁】\n"
                "  /ban area <国家代码>       封禁地区\n"
                "  /unban area <国家代码>     解封地区\n"
                "  /ban timezone <时区名>     封禁时区\n"
                "  /unban timezone <时区名>   解封时区\n\n"
                "【IP 封禁】（管理员专用）\n"
                "  /ban ip list              查看已封禁的 IP 列表\n"
                "  /ban ip <IP>              直接封禁指定 IP（支持 IPv4）\n"
                "  /ban ip <玩家名>          封禁在线玩家当前的 IP\n"
                "  /unban ip <IP>            解封指定 IP\n"
                "  ⚠️ 封禁 IP 后，所有使用该 IP 的非管理员玩家将被立即踢出。\n\n"
                "【访问控制】（管理员专用）\n"
                "  /maxplayers <人数>         设置服务器最大在线人数（默认10）\n"
                "  /access <whitelist|blacklist|none> <on|off>\n"
                "      开启/关闭白名单或黑名单模式，none 关闭所有限制\n"
                "  /whitelist add|remove|list [uuid]\n"
                "      管理白名单（uuid 为 server_uid 或 uid）\n"
                "  /blacklist add|remove|list [uuid]\n"
                "      管理黑名单（uuid 为 server_uid 或 uid）\n"
                "  ⚠️ 管理员（op）拥有完全豁免权，不受上述限制\n\n"
                "【其他】\n"
                "  /kick <玩家>               踢出玩家（需在线）\n"
                "  /recall <消息ID>           撤回消息（自己的2分钟内；管理员可撤回非管理员的消息）\n"
                "  私信/好友：右键玩家名字→私信；互发1条消息即成为好友（上限100），支持好友备注\n"
                "  /redeemstats               查看自己的兑换码净获得统计\n\n"
                "💡 提示：所有管理命令仅管理员可用。输入 /help 查看本帮助。"
            )
        else:
            help_text = "【普通用户命令】\n直接输入兑换码字符串即可兑换。\n可用命令：\n/help  - 显示此帮助\n签到、商店、邮箱请通过右键菜单操作。"

        dialog = QDialog(self)
        dialog.setWindowTitle("帮助")
        dialog.setMinimumSize(550, 400)
        dialog.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 12px;
            }
            QTextEdit {
                background-color: #2D2D2D;
                color: #CCCCCC;
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        text_edit = QTextEdit()
        text_edit.setPlainText(help_text)
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dialog.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignCenter)
        dialog.setLayout(layout)
        set_window_emoji_icon(dialog, "❓")
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())
        dialog.open()

    def process_admin_command(self, code):
        """单机模式下处理管理员命令（新版格式）"""
        if not code.startswith('/'):
            return False
        cmd_line = code[1:].strip()
        if not cmd_line:
            return False

        parts = cmd_line.split()
        cmd_name = parts[0].lower()

        # ---------- 控制台专用命令黑名单（客户端禁止） ----------
        console_only_commands = ['restart', 'close', 'change', 'fix', 'all_versions']
        if cmd_name in console_only_commands:
            self.show_centered_message("提示", "此命令只能在服务器控制台执行", QMessageBox.Warning)
            return True

        # ---------- 禁止客户端执行时区和地区封禁 ----------
        if cmd_name in ['ban', 'unban'] and len(parts) >= 3:
            sub_cmd = parts[1].lower()
            if sub_cmd in ['area', 'timezone']:
                self.show_centered_message("提示", "此命令只能在服务器控制台执行", QMessageBox.Warning)
                return True

        # ===== 帮助 =====
        if cmd_name == "help":
            self.show_help()
            return True

        # ===== 服务器专属命令（仅联机） =====
        server_only = ['ban', 'unban', 'op', 'deop', 'fix', 'all_versions', 'banarea', 'untimezone']
        if cmd_name in server_only or cmd_name.startswith('ban') or cmd_name.startswith('unban'):
            self.show_centered_message("提示", "该命令仅适用于联机模式，请先加入服务器。", QMessageBox.Warning)
            return True

        # ===== 移动命令 =====
        if cmd_name == "move":
            if not self.allow_move:
                self.show_centered_message("提示", "请先在右键菜单中勾选“允许移动”", QMessageBox.Warning)
            elif self.is_moving:
                self.show_centered_message("提示", "桌宠正在移动中", QMessageBox.Warning)
            elif self.current_animation_state != "Standby":
                self.show_centered_message("提示", "当前状态不是Standby，无法移动", QMessageBox.Warning)
            else:
                self.start_random_move()
            return True

        # ===== /set newcode 创建兑换码 =====
        if cmd_name == "set" and len(parts) >= 3 and parts[1].lower() == "newcode":
            if len(parts) < 4:
                self.show_centered_message("错误", "格式: /set newcode <兑换码> <物品:数量&物品...> [有效期]",
                                           QMessageBox.Warning)
                return True
            code = parts[2]
            args = parts[3:]
            expire_str = None
            # 使用 parse_time_spec 检测最后一个参数是否为有效期
            if args:
                delta, dt = parse_time_spec(args[-1])
                if delta is not None or dt is not None:
                    expire_str = args[-1]
                    items_spec = ' '.join(args[:-1])
                else:
                    items_spec = ' '.join(args)
            else:
                items_spec = ''
            items = self._parse_items_spec(items_spec)
            if items is None:
                self.show_centered_message("错误", "物品列表解析失败", QMessageBox.Warning)
                return True
            expire_time = None
            if expire_str:
                delta, until_dt = parse_time_spec(expire_str)
                if until_dt:
                    expire_time = until_dt.isoformat()
                elif delta:
                    expire_time = (datetime.now() + delta).isoformat()
                else:
                    self.show_centered_message("错误", f"无效的有效期格式: {expire_str}", QMessageBox.Warning)
                    return True
            all_codes = load_all_codes()
            if code in all_codes:
                self.show_centered_message("错误", f"兑换码 {code} 已存在", QMessageBox.Warning)
                return True
            all_codes[code] = {
                "max_uses": 0,
                "used_count": 0,
                "created_by": self.current_user["username"],
                "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "target_user": None,
                "items": items,
                "expire_time": expire_time,
                "revoked": False,
                "revoked_users": [],
                "revoked_by": None,
                "revoked_time": None
            }
            save_all_codes(all_codes)
            history = self.current_user.get("redeem_history", [])
            history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 创建兑换码 {code}")
            self.current_user["redeem_history"] = history
            self._save_current_user()
            self.show_centered_message("成功", f"兑换码 {code} 已创建", QMessageBox.Information)
            return True

        # ===== /give 发放物品 =====
        if cmd_name == "give":
            if len(parts) < 3:
                self.show_centered_message("错误", "格式: /give <用户> <物品> [数量] (数量默认为1，时装自动为1)",
                                           QMessageBox.Warning)
                return True
            user_spec = parts[1]
            # 大小写不敏感解析（自定义物品可能含大写，如 Oneiric_Shard）
            item_id = self._resolve_local_item_id(parts[2]) or parts[2].strip().lower()
            if len(parts) >= 4:
                try:
                    amount = int(parts[3])
                except:
                    self.show_centered_message("错误", "数量必须为整数", QMessageBox.Warning)
                    return True
                if amount <= 0:
                    self.show_centered_message("错误", "数量必须大于0", QMessageBox.Warning)
                    return True
            else:
                amount = 1
            # 时装强制数量为1；自定义"物品"尊重填写的数量（以前所有非内置物品都被强制成 1）
            if self._is_clothes_like(item_id) or item_id == 'ruyi':
                amount = 1

            # 获取目标用户
            target_users = []
            if user_spec == '@a':
                for filename in os.listdir(USER_DATA_DIR):
                    if filename == os.path.basename(CODES_FILE):
                        continue
                    if filename.endswith(".json"):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("userid"):
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", "没有找到任何用户", QMessageBox.Warning)
                    return True
            elif user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            # ★ 构造物品（必须包含 jade 和 star_rail_tickets）
            items = []
            if item_id == 'cake':
                items.append({'type': 'cake', 'amount': amount})
            elif item_id == 'credit':
                items.append({'type': 'credit', 'amount': amount})
            elif item_id == 'ticket' or item_id == 'star_rail_tickets':
                items.append({'type': 'star_rail_ticket', 'amount': amount})
            elif item_id == 'jade':
                items.append({'type': 'jade', 'amount': amount})
            elif item_id in AVAILABLE_CLOTHES:
                items.append({'type': 'clothes', 'id': item_id})
            else:
                # 自定义物品（若有）
                custom_items = self.server.load_custom_items() if hasattr(self, 'server') else []
                item_info = next((it for it in custom_items if it.get('id') == item_id), None)
                if item_info:
                    items.append(
                        {'type': 'custom', 'id': item_id, 'amount': amount, 'name': item_info.get('name', item_id)})
                else:
                    self.show_centered_message("错误", f"未知物品ID：{item_id}", QMessageBox.Warning)
                    return True

            # 调试：打印 items 确认非空
            print(f"[DEBUG] /give items = {items}")

            # 发送邮件给每个目标
            for u in target_users:
                self.send_mail_to_user(u, "管理员赠送物品", f"管理员 {self.current_user['username']} 赠送了你物品。",
                                       items)
            self.show_centered_message("成功", f"已向 {len(target_users)} 个用户发送物品邮件", QMessageBox.Information)
            return True

        # ===== /set 设置物品 =====
        if cmd_name == "set":
            if len(parts) < 4:
                self.show_centered_message("错误", "格式: /set <用户> <物品> <数量>", QMessageBox.Warning)
                return True
            user_spec = parts[1]
            # 大小写不敏感解析（自定义物品可能含大写）
            item_id = self._resolve_local_item_id(parts[2]) or parts[2].strip().lower()
            try:
                amount = int(parts[3])
            except:
                self.show_centered_message("错误", "数量必须为整数", QMessageBox.Warning)
                return True
            if amount < 0 or amount > 99999999:
                self.show_centered_message("错误", "数量必须在0-99999999之间", QMessageBox.Warning)
                return True

            # 获取目标用户
            target_users = []
            if user_spec == '@a':
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("userid"):
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", "没有找到任何用户", QMessageBox.Warning)
                    return True
            elif user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            # ★ 设置物品（增加 jade 和 star_rail_tickets）
            for u in target_users:
                inv = u  # 单机用户数据即 inventory
                if item_id == 'cake':
                    inv['balance'] = amount
                elif item_id == 'credit':
                    inv['credit'] = amount
                elif item_id == 'ticket' or item_id == 'star_rail_tickets':
                    inv['star_rail_tickets'] = amount
                elif item_id == 'jade':
                    inv['jade'] = amount
                elif self._is_clothes_like(item_id):
                    if amount == 0:
                        if item_id in inv.get('owned_clothes', []):
                            inv['owned_clothes'].remove(item_id)
                    elif amount == 1:
                        if item_id not in inv.get('owned_clothes', []):
                            inv['owned_clothes'].append(item_id)
                    else:
                        self.show_centered_message("错误", f"时装只能设置为 0(移除) 或 1(添加)，当前为 {amount}",
                                                   QMessageBox.Warning)
                        return True
                elif self._resolve_local_item_id(item_id):
                    # 自定义物品：直接设定数量（0 = 清空）
                    _ci = inv.setdefault('custom_items', {})
                    if amount <= 0:
                        _ci.pop(item_id, None)
                    else:
                        _ci[item_id] = amount
                else:
                    self.show_centered_message("错误", f"未知物品: {item_id}", QMessageBox.Warning)
                    return True
                self.save_user_data(u)
            self.show_centered_message("成功", f"已设置 {len(target_users)} 个用户的 {item_id} 为 {amount}",
                                       QMessageBox.Information)
            return True

        # ===== /clear 清空物品 =====
        if cmd_name == "clear":
            if len(parts) < 3:
                self.show_centered_message("错误", "格式: /clear <用户> <物品或*>", QMessageBox.Warning)
                return True
            user_spec = parts[1]
            item_spec = parts[2].lower()

            # 仅支持单个用户或@s，不支持@a
            target_users = []
            if user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                                break
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            for u in target_users:
                if item_spec == '*':
                    u['balance'] = 0
                    u['credit'] = 0
                    # 不清除时装和专票
                elif item_spec == 'cake':
                    u['balance'] = 0
                elif item_spec == 'credit':
                    u['credit'] = 0
                else:
                    self.show_centered_message("错误", f"未知物品: {item_spec}", QMessageBox.Warning)
                    return True
                self.save_user_data(u)
            self.show_centered_message("成功", f"已清空 {item_spec}，影响 {len(target_users)} 个用户",
                                       QMessageBox.Information)
            return True

        # ===== /revoke 物品扣除（不带 code），含统计更新 =====
        if cmd_name == "revoke" and not (len(parts) >= 3 and parts[1].lower() == "code"):
            if len(parts) < 3:
                self.show_centered_message("错误", "格式: /revoke <用户> <物品:数量&物品...>", QMessageBox.Warning)
                return True

            user_spec = parts[1]
            items_spec = ' '.join(parts[2:])

            # 解析物品列表
            items = []
            for part in items_spec.split('&'):
                part = part.strip()
                if not part:
                    continue
                if ':' in part:
                    itype, qty = part.split(':', 1)
                    try:
                        qty = int(qty)
                    except:
                        self.show_centered_message("错误", f"数量错误: {part}", QMessageBox.Warning)
                        return True
                else:
                    itype = part
                    qty = 1

                # ★ 修改点1：扩展支持 jade 和 star_rail_tickets
                if itype not in ('cake', 'credit', 'ticket', 'star_rail_tickets',
                                 'jade') and itype not in AVAILABLE_CLOTHES:
                    self.show_centered_message("错误", f"未知物品: {itype}", QMessageBox.Warning)
                    return True
                items.append({'type': itype, 'amount': qty})

            if not items:
                self.show_centered_message("错误", "未指定有效物品", QMessageBox.Warning)
                return True

            # 获取目标用户（单机支持 @s 或具体用户，@a 仅操作自己）
            target_users = []
            if user_spec == '@s':
                target_users = [self.current_user]
            elif user_spec == '@a':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            # 遍历每个目标用户
            for u in target_users:
                inv = u  # 单机用户数据即 inventory
                items_desc = []
                # ★ 修改点2：初始化所有增量统计变量
                credit_delta = 0
                cake_delta = 0
                ticket_delta = 0
                jade_delta = 0

                for item in items:
                    itype = item['type']
                    amount = item['amount']
                    if itype == 'cake':
                        inv['balance'] = inv.get('balance', 0) - amount
                        cake_delta -= amount
                        items_desc.append(f"橡木蛋糕卷 x{amount}")
                    elif itype == 'credit':
                        inv['credit'] = inv.get('credit', 0) - amount
                        if inv['credit'] < -99999999:
                            inv['credit'] = -99999999
                        credit_delta -= amount
                        items_desc.append(f"信用点 x{amount}")
                    elif itype in ('ticket', 'star_rail_tickets'):  # ★ 同时支持两种写法
                        inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) - amount
                        ticket_delta -= amount
                        items_desc.append(f"星铁专票 x{amount}")
                    elif itype == 'jade':  # ★ 新增星琼支持
                        inv['jade'] = inv.get('jade', 0) - amount
                        jade_delta -= amount
                        items_desc.append(f"星琼 x{amount}")
                    else:  # 时装
                        if amount > 0:
                            owned = inv.get('owned_clothes', ['normal'])
                            if itype in owned:
                                owned.remove(itype)
                                inv['owned_clothes'] = owned
                                clothes_name = AVAILABLE_CLOTHES.get(itype, {}).get('name', itype)
                                items_desc.append(f"时装 [{clothes_name}]")
                        # 时装扣除不影响统计

                # ★ 修改点3：更新月度统计（包含 jade 和 ticket）
                if credit_delta != 0 or cake_delta != 0 or ticket_delta != 0 or jade_delta != 0:
                    self.check_month_reset()
                    if 'monthly_credit_gained' in u:
                        u['monthly_credit_gained'] += credit_delta
                    if 'monthly_cake_gained' in u:
                        u['monthly_cake_gained'] += cake_delta
                    if 'monthly_jade_gained' in u:  # 新增
                        u['monthly_jade_gained'] += jade_delta
                    if 'monthly_ticket_gained' in u:  # 新增
                        u['monthly_ticket_gained'] += ticket_delta
                    # 可选：扩展 redeem_stats（保留原逻辑）
                    stats = u.setdefault('redeem_stats', {})
                    if credit_delta != 0:
                        stats['credit'] = stats.get('credit', 0) + credit_delta
                    if cake_delta != 0:
                        stats['cake'] = stats.get('cake', 0) + cake_delta
                    if jade_delta != 0:
                        stats['jade'] = stats.get('jade', 0) + jade_delta
                    if ticket_delta != 0:
                        stats['ticket'] = stats.get('ticket', 0) + ticket_delta

                # 保存用户数据
                if u.get('userid') == self.current_user.get('userid'):
                    self._save_current_user()
                else:
                    self.save_user_data(u)

                # 发送邮件通知
                if items_desc:
                    mail_content = f"管理员已从您的账户中扣除以下物品：\n" + "\n".join(items_desc)
                else:
                    mail_content = "管理员执行了物品扣除操作，但未影响您的任何物品。"
                send_mail(u, "物品扣除通知", mail_content, [])

                # 如果操作的是自己，刷新界面
                if u.get('userid') == self.current_user.get('userid'):
                    self.update_bag_display()
                    self.show_centered_message("成功", f"已扣除物品，请查收邮箱通知", QMessageBox.Information)
                else:
                    self.show_centered_message("成功", f"已扣除用户 {u.get('username')} 的物品，并发送了邮件通知",
                                               QMessageBox.Information)

            return True

        # ===== /revoke code 撤销兑换码 =====
        if cmd_name == "revoke" and len(parts) >= 3 and parts[1].lower() == "code":
            if len(parts) < 4:
                self.show_centered_message("错误", "格式: /revoke code <兑换码> <用户>", QMessageBox.Warning)
                return True
            code = parts[2]
            user_spec = parts[3]

            all_codes = load_all_codes()
            if code not in all_codes:
                self.show_centered_message("错误", f"兑换码 {code} 不存在", QMessageBox.Warning)
                return True
            cinfo = all_codes[code]
            if cinfo.get("revoked"):
                self.show_centered_message("提示", "该兑换码已被撤销", QMessageBox.Warning)
                return True

            # 获取目标用户
            target_users = []
            if user_spec == '@a':
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("userid"):
                                target_users.append(u)
                        except:
                            continue
            elif user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            # 标记撤销
            for u in target_users:
                userid = u["userid"]
                if userid not in cinfo.get("revoked_users", []):
                    cinfo.setdefault("revoked_users", []).append(userid)
            save_all_codes(all_codes)
            self.show_centered_message("成功", f"已撤销 {len(target_users)} 个用户对兑换码 {code} 的使用资格",
                                       QMessageBox.Information)
            return True

        # ===== 其他未知命令 =====
        self.show_centered_message("提示", f"未知命令或该命令不支持单机模式", QMessageBox.Warning)
        return True

    def clear_all_items(self, target_identifiers_str=None):
            """清空目标用户的所有物品（蛋糕卷+信用点）"""
            if not target_identifiers_str:
                self.show_centered_message("错误", "请指定目标用户（如 @a）", QMessageBox.Warning)
                return False

            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False

                # 遍历清空蛋糕卷 + 信用点
                for user in target_users:
                    user["balance"] = 0  # 清空蛋糕卷
                    user["credit"] = 0   # 清空信用点
                    self.save_user_data(user)

                self.show_centered_message("成功", f"已清空 {len(target_users)} 个用户的所有物品！", QMessageBox.Information)
            except Exception as e:
                self.show_centered_message("错误", f"清空失败：{str(e)}", QMessageBox.Warning)
                return False
            return True

    def redeem_code(self):
        dialog = RedeemCodeDialog(self)
        self._redeem_dialog = dialog
        dialog.code_submitted.connect(self.handle_redeem_code)
        dialog.open()

    def handle_redeem_code(self, code):
        """
        兑换码窗口输入处理（支持命令）
        - 如果输入以 '/' 开头，作为命令处理：
            · /help、/move、/debug 本地处理
            · 联机模式：其他命令发送到服务端
            · 单机模式：其他命令调用 process_admin_command 本地执行
        - 否则作为兑换码处理（联机发送 redeem_code 请求，单机本地验证并发放）
        """
        dialog = getattr(self, '_redeem_dialog', None)
        if dialog is None or not dialog.isVisible():
            return

        if not code:
            QMessageBox.warning(dialog, "提示", "请输入兑换码或命令")
            return

        # ---------- 命令处理 ----------
        if code.startswith('/'):
            parts = code.split()
            cmd = parts[0].lower()

            # ----- 本地通用命令（单机/联机均本地处理） -----
            if cmd == '/help':
                self.show_help()
                return

            if cmd == '/move':
                if not self.allow_move:
                    self.show_centered_message("提示", "请先在右键菜单中勾选“允许移动”", QMessageBox.Warning)
                elif self.is_moving:
                    self.show_centered_message("提示", "桌宠正在移动中", QMessageBox.Warning)
                elif self.current_animation_state != "Standby":
                    self.show_centered_message("提示", "当前状态不是Standby，无法移动", QMessageBox.Warning)
                else:
                    self.start_random_move()
                return

            # ----- 仅 debugger 可用的调试命令 -----
            if cmd == '/debug':
                if not self.is_debugger:
                    self.show_centered_message("提示", "无效指令", QMessageBox.Warning)
                    return
                if len(parts) < 2:
                    self.show_centered_message("提示", "用法: /debug on 或 /debug off")
                    return
                mode = parts[1].lower()
                if mode not in ('on', 'off'):
                    self.show_centered_message("提示", "参数必须为 on 或 off")
                    return
                if self.debug_mode == (mode == 'on'):
                    self.show_centered_message("提示", f"联机功能已经是 {'启用' if self.debug_mode else '禁用'} 状态")
                    return
                self.debug_mode = (mode == 'on')
                os.environ['FIREFLY_DEBUG_MODE'] = mode
                self.create_menu()
                self.show_centered_message("提示", f"联机功能已{'启用' if self.debug_mode else '禁用'}")
                return

            # ----- 其他命令：单机本地处理，联机发送到服务端 -----
            if self.mp_client.connected:
                # 联机模式：直接发送到服务端（服务端进行权限校验和执行）
                self.mp_client.send_command(code)
            else:
                # 单机模式：调用本地命令处理器（process_admin_command）
                # 若返回 False，说明命令未被识别或处理失败
                if not self.process_admin_command(code):
                    self.show_centered_message("提示", "未知命令或该命令不支持单机模式", QMessageBox.Warning)
            return

        # ---------- 兑换码处理（非命令） ----------
        # 联机模式：发送兑换码到服务端
        if self.mp_client.connected:
            self._before_redeem_credit = self.current_user.get('credit', 0)
            self.mp_client._send({'type': 'redeem_code', 'data': {'code': code}})
            return

        # ---------- 单机模式兑换码处理（原有逻辑） ----------
        all_codes = load_all_codes()
        redeemed = self.current_user.get("redeemed_codes", [])

        if code not in all_codes:
            QMessageBox.warning(dialog, "提示", "无效的兑换码！")
            return
        cinfo = all_codes[code]
        if cinfo.get("revoked"):
            QMessageBox.warning(dialog, "提示", "该兑换码已被管理员撤销")
            return
        if self.current_user["userid"] in cinfo.get("revoked_users", []):
            QMessageBox.warning(dialog, "提示", "您对该兑换码的使用资格已被撤销")
            return
        expire_time = cinfo.get("expire_time")
        if expire_time:
            try:
                expire_dt = datetime.fromisoformat(expire_time)
                if datetime.now() > expire_dt:
                    QMessageBox.warning(dialog, "提示", "该兑换码已过期")
                    return
            except:
                pass
        if code in redeemed and cinfo.get("max_uses", 1) == 1:
            QMessageBox.warning(dialog, "提示", "您已经使用过该兑换码！")
            return
        target_spec = cinfo.get("target_user")
        if target_spec:
            try:
                target_users = self.find_users_by_identifiers(target_spec)
                if target_users:
                    user_match = any(tu["userid"] == self.current_user["userid"] for tu in target_users)
                    if not user_match:
                        QMessageBox.warning(dialog, "提示", "未知兑换码")
                        return
            except:
                QMessageBox.warning(dialog, "提示", "兑换码配置错误")
                return
        if cinfo["max_uses"] != 0 and cinfo["used_count"] >= cinfo["max_uses"]:
            QMessageBox.warning(dialog, "提示", "该兑换码已失效（达到使用次数上限）")
            return

        # 单机奖励发放
        items = []
        for item in cinfo.get("items", []):
            itype = item["type"]
            if itype == "cake":
                items.append({"type": "cake", "amount": item["amount"]})
            elif itype == "credit":
                items.append({"type": "credit", "amount": item["amount"]})
            elif itype == "clothes":
                items.append({"type": "clothes", "id": item["id"]})
            elif itype == "star_rail_ticket":
                items.append({"type": "star_rail_ticket", "amount": item["amount"]})
            elif itype == "jade":
                items.append({"type": "jade", "amount": item["amount"]})
                # 累加统计
                self.current_user["monthly_jade_gained"] = self.current_user.get("monthly_jade_gained", 0) + item[
                    "amount"]
                self.current_user["total_jade_gained"] = self.current_user.get("total_jade_gained", 0) + item["amount"]

        self.send_mail_to_user(self.current_user, "兑换码奖励",
                               f"成功使用兑换码 {code}，获得以下物品。", items)
        if cinfo["max_uses"] != 0:
            self.current_user["redeemed_codes"] = redeemed + [code]
        cinfo["used_count"] += 1
        save_all_codes(all_codes)
        history = self.current_user.get("redeem_history", [])
        history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 使用兑换码 {code}")
        self.current_user["redeem_history"] = history
        self.check_month_reset()
        for item in items:
            if item["type"] == "credit":
                self.current_user["monthly_credit_gained"] += item["amount"]
            elif item["type"] == "cake":
                self.current_user["monthly_cake_gained"] += item["amount"]
        self._save_current_user()

        QMessageBox.information(dialog, "成功", "兑换成功！物品已发送至邮箱，请查收。")

    def show_history(self):
        if self.mp_client.connected:
            # 显示在线兑换记录
            win = HistoryWindow({'redeem_history': self.online_redeemed_codes}, False, self)
        else:
            is_admin = self.current_user.get("uid", "").lower().startswith("admin")
            win = HistoryWindow(self.current_user, is_admin, self)
        win.open()

    def open_mailbox(self):
        if self.mp_client.connected:
            self.mp_client._send({'type': 'get_mails'})
            self._pending_mail_request = 'open'
        else:
            mailbox_win = MailboxWindow(self, self)
            mailbox_win.exec_()

    # ---------- 动作方法 ----------
    def feed(self):
        self.check_month_reset()
        now_ts = time.time()
        inv = self.get_active_inventory()
        last_feed = self.current_user.get("last_feed_time")
        cool_down_sec = 10 * 60
        if last_feed is not None:
            diff = now_ts - last_feed
            if diff < cool_down_sec:
                remain = cool_down_sec - diff
                m = int(remain // 60)
                s = int(remain % 60)
                self.show_centered_message("冷却提示", f"流萤已经吃饱啦！\n投喂冷却中！还需等待 {m}分{s}秒",
                                           QMessageBox.Warning)
                return
        balance = inv.get('balance', 0)
        if balance <= 0:
            self.show_centered_message("提示", "橡木蛋糕卷不足，无法投喂！", QMessageBox.Warning)
            return
        inv['balance'] = balance - 1
        self.current_user["last_feed_time"] = now_ts
        if self.mp_client.connected and self.online_inventory is not None:
            self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
        else:
            self._save_current_user()

        # 统计
        self.current_user["total_interactions"] += 1
        self.current_user["monthly_interactions"] += 1
        self.current_user["total_affection_up"] += 1
        self.current_user["monthly_affection_up"] += 1
        self._save_current_user()

        self.last_animation_state = self.current_animation_state
        self.record_interaction(extra_seconds=10 * 60)

        if self.movie is None:
            return
        self.movie.stop()
        gif_path = None
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            gif_path = asset_path("images", "firefly", "actions", "eat", "eat_Large.gif")
        elif 'small' in files:
            gif_path = asset_path("images", "firefly", "actions", "eat", "eat_small.gif")
        elif 'normal' in files:
            gif_path = asset_path("images", "firefly", "actions", "eat", "eat.gif")
        elif 'medium' in files:
            gif_path = asset_path("images", "firefly", "actions", "eat", "eat_medium.gif")
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(5000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def heart(self):
        self.check_month_reset()
        if self.movie is None:
            return
        self.last_animation_state = self.current_animation_state
        self.record_interaction(extra_seconds=2 * 60)
        self.movie.stop()
        gif_path = None
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            gif_path = asset_path("images", "firefly", "actions", "Love", "Love_Large.gif")
        elif 'small' in files:
            gif_path = asset_path("images", "firefly", "actions", "Love", "Love_small.gif")
        elif 'normal' in files:
            gif_path = asset_path("images", "firefly", "actions", "Love", "Love.gif")
        elif 'medium' in files:
            gif_path = asset_path("images", "firefly", "actions", "Love", "Love_medium.gif")
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(1300, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

        # 统计
        self.current_user["total_interactions"] += 1
        self.current_user["monthly_interactions"] += 1
        self.current_user["total_affection_up"] += 1
        self.current_user["monthly_affection_up"] += 1
        self._save_current_user()

    def sleep(self):
        self.check_month_reset()
        if self.anim_restore_timer:
            self.anim_restore_timer.stop()
            self.anim_restore_timer = None
        if self.movie is None:
            return
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            gif_path = asset_path("images", "firefly", "actions", "sleep", "sleep_Large.gif")
        elif 'small' in files:
            gif_path = asset_path("images", "firefly", "actions", "sleep", "sleep_small.gif")
        elif 'normal' in files:
            gif_path = asset_path("images", "firefly", "actions", "sleep", "sleep.gif")
        elif 'medium' in files:
            gif_path = asset_path("images", "firefly", "actions", "sleep", "sleep_medium.gif")

        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()

        self.current_animation_state = "Sleep"
        self.sleep_start_time = time.time()
        self.current_user["total_sleep_count"] += 1
        self.current_user["monthly_sleep_count"] += 1
        self._save_current_user()

    def restore_previous_animation(self):
        if self.last_animation_state == "Discomfort":
            self.changeToDiscomfort()
        else:
            self.changeToStandby()

    def changeToStandby(self):
        if self.movie is None:
            return
        self.movie.stop()
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            self.movie = QMovie(
                asset_path("images", "firefly", "actions", "Standby", "Standby_Large.gif"))
        elif 'small' in files:
            self.movie = QMovie(
                asset_path("images", "firefly", "actions", "Standby", "Standby_small.gif"))
        elif 'normal' in files:
            self.movie = QMovie(asset_path("images", "firefly", "actions", "Standby", "Standby.gif"))
        elif 'medium' in files:
            self.movie = QMovie(
                asset_path("images", "firefly", "actions", "Standby", "Standby_medium.gif"))
        self.label.setMovie(self.movie)
        self.movie.start()
        self.current_animation_state = "Standby"
        self.persistent_state = "Standby"
        if self.current_user:
            self.current_user["last_state"] = "Standby"
            self._save_current_user()

    def changeToDiscomfort(self):
        if self.movie is None:
            return
        self.movie.stop()
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            self.movie = QMovie(
                asset_path("images", "firefly", "actions", "Discomfort", "Discomfort_Large.gif"))
        elif 'small' in files:
            self.movie = QMovie(
                asset_path("images", "firefly", "actions", "Discomfort", "Discomfort_small.gif"))
        elif 'normal' in files:
            self.movie = QMovie(
                asset_path("images", "firefly", "actions", "Discomfort", "Discomfort.gif"))
        elif 'medium' in files:
            self.movie = QMovie(
                asset_path("images", "firefly", "actions", "Discomfort", "Discomfort_medium.gif"))
        self.label.setMovie(self.movie)
        self.movie.start()
        self.current_animation_state = "Discomfort"
        self.persistent_state = "Discomfort"
        if self.current_user:
            self.current_user["last_state"] = "Discomfort"
            self._save_current_user()
        show_notification_async(
            title="流萤桌宠",
            message="萤宝不开心了，给她投喂或陪她玩玩吧！",
            icon_path=asset_path("images", "firefly", "Sadness", "abandoned.ico"),
            duration=3
        )

    def sing_and_dance(self):
        self.play_music()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_Large.gif")
        elif 'small' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_small.gif")
        elif 'normal' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing.gif")
        elif 'medium' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_medium.gif")
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(88000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def sing_and_dance2(self):
        self.play_music()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_Large.gif")
        elif 'small' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_small.gif")
        elif 'normal' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing.gif")
        elif 'medium' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_medium.gif")
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(92000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def sing_and_dance3(self):
        self.play_music()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_Large.gif")
        elif 'small' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_small.gif")
        elif 'normal' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing.gif")
        elif 'medium' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_medium.gif")
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(155000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def sing_and_dance4(self):
        self.play_music()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = list_size_markers()
        base = get_base_dir()
        if 'Large' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_Large.gif")
        elif 'small' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_small.gif")
        elif 'normal' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing.gif")
        elif 'medium' in files:
            gif_path = asset_path("images", "firefly", "actions", "sing", "sing_medium.gif")
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(340000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def check_new(self):
        webbrowser.open("https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online/releases")

    def AI(self):
        # 检查是否被服务器禁用
        if not self.ai_enabled:
            self.show_centered_message("提示", "此功能已被服务器禁用", QMessageBox.Warning)
            return

        def check_process_running(process_name):
            for process in psutil.process_iter(['name']):
                if process.info['name'] == process_name:
                    return True
            return False

        process_name = 'AI.exe'
        if not check_process_running(process_name):
            exe_path = os.path.join(get_base_dir(), "tools", "AI.exe")
            subprocess.Popen([exe_path])

    def open_AI(self):
        if not self.ai_enabled:
            self.show_centered_message("提示", "此功能已被服务器禁用", QMessageBox.Warning)
            return
        self.check_month_reset()
        if self.current_user:
            self.current_user["monthly_ai_click_count"] += 1
            self._save_current_user()

        def check_process_running(process_name):
            for process in psutil.process_iter(['name']):
                if process.info['name'] == process_name:
                    return True
            return False

        process_name = 'AI.exe'
        if not check_process_running(process_name):
            exe_path = os.path.join(get_base_dir(), "tools", "AI.exe")
            subprocess.Popen([exe_path])

    def set_cursor(self, cursor_name):
        if self.current_file_name and os.path.exists(self.current_file_name):
            if not os.path.exists(cursor_name):
                os.rename(self.current_file_name, cursor_name)
            self.current_file_name = cursor_name

    def rename_file(self, new_name):
        if self.current_file_name and os.path.exists(self.current_file_name) and not os.path.exists(new_name):
            os.rename(self.current_file_name, new_name)
            self.current_file_name = new_name

    def set_default_cursor(self):
        self.setCursor(QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = True
            self.offset = event.pos()

    def mouseMoveEvent(self, event):
        if self.draggable:
            self.move(event.globalPos() - self.offset)
            self.movie.stop()
            files = list_size_markers()
            base = get_base_dir()
            m = None
            if 'Large' in files:
                m = QMovie(asset_path("images", "firefly", "actions", "mention", "mention_Large.gif"))
            elif 'small' in files:
                m = QMovie(asset_path("images", "firefly", "actions", "mention", "mention_small.gif"))
            elif 'normal' in files:
                m = QMovie(asset_path("images", "firefly", "actions", "mention", "mention.gif"))
            elif 'medium' in files:
                m = QMovie(
                    asset_path("images", "firefly", "actions", "mention", "mention_medium.gif"))
            if m:
                self.label.setMovie(m)
                m.start()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = False
            if self.movie:
                self.label.setMovie(self.movie)
                self.movie.start()

    def _check_and_grant_achievement(self):
        """检查月度成就并发放奖励（仅联机模式）"""
        try:
            return self._check_and_grant_achievement_inner()
        except Exception:
            # 该方法在 _save_current_user 的 try 之外被调用，且可能由虚函数触发，
            # 异常会直接导致 PyQt abort，因此就地兜住
            _log_exception(sys.exc_info())

    def _check_and_grant_achievement_inner(self):
        """实际的月度成就检查逻辑"""
        # ★ 修改：单机模式直接返回，不执行月度成就
        if not self.mp_client.connected:
            return  # 单机不发放月度奖励

        if not self.current_user:
            return

        import calendar
        now = datetime.now()
        current_month = now.strftime('%Y-%m')
        days_in_month = calendar.monthrange(now.year, now.month)[1]

        # 目标值
        credit_target = 7200 * days_in_month
        sign_target = days_in_month
        jade_target = 1600
        cake_target = 72

        # 当前值
        credit_gained = self.current_user.get('monthly_credit_gained', 0)
        sign_days = self.current_user.get('monthly_sign_days', 0)
        jade_gained = self.current_user.get('monthly_jade_gained', 0)
        cake_gained = self.current_user.get('monthly_cake_gained', 0)

        # 检查是否全部达标
        if (credit_gained >= credit_target and
                sign_days >= sign_target and
                jade_gained >= jade_target and
                cake_gained >= cake_target):

            # 检查是否已发放
            if self.current_user.get('last_achievement_month') == current_month:
                return

            # 发放 10 张星铁专票
            items = [{"type": "star_rail_ticket", "amount": 10}]
            self.send_mail_to_user(
                self.current_user,
                "🎉 月度成就达成！",
                "恭喜你！本月所有养成进度均已达成 100%，获得 10 张星铁专票作为奖励！",
                items
            )
            self.current_user['last_achievement_month'] = current_month
            self._save_current_user()
            show_notification_async(
                title="流萤桌宠",
                message="🎉 恭喜你达成月度全成就！请查看邮箱领取奖励。",
                duration=3
            )

    def mouseDoubleClickEvent(self, event):
        if self._ui_locked():
            return
        if event.button() == Qt.LeftButton:
            try:
                music_path = os.path.join(get_base_dir(), "music", "我将，点燃心海！.wav")
                pygame.mixer.music.load(music_path)
                pygame.mixer.music.set_volume(0.5)
                pygame.mixer.music.play()
            except Exception as e:
                print(f"双击播放失败: {e}")

    # ---------- 生日检测与欢迎 ----------
    def is_birthday_today(self):
        if not self.current_user:
            return False
        birthday_str = self.current_user.get("birthday")
        if not birthday_str:
            return False
        try:
            bd = datetime.strptime(birthday_str, "%Y-%m-%d")
            today = datetime.now()
            return (bd.month == today.month and bd.day == today.day)
        except:
            return False

    def has_birthday_greeted_this_year(self):
        if not self.current_user:
            return True
        last_year = self.current_user.get("last_birthday_greet_year")
        return last_year == datetime.now().year

    def send_birthday_mail(self):
        """发送生日邮件（单机：5200信用点；联机：由服务端处理）"""
        # 单机模式：直接本地发送
        if not self.mp_client.connected:
            current_year = datetime.now().year
            if self.current_user.get('birthday_reward_year') == current_year:
                return  # 已发放
            # ★ 修改：单机生日奖励改为 5200 信用点
            items = [{"type": "credit", "amount": 5200}]
            self.send_mail_to_user(
                self.current_user,
                "流萤的祝福",
                f"{self.current_user.get('username', '用户')}，生日快乐！",
                items
            )
            self.current_user['birthday_reward_year'] = current_year
            self._save_current_user()
            return

        # 联机模式：请求服务端发送（服务端逻辑保持不变，发放320星琼）
        self.mp_client._send({
            'type': 'request_birthday_mail',
            'data': {
                'display_name': self.online_name or self.current_user.get('username', '用户')
            }
        })

    def hi_music(self):
        def play_welcome():
            try:
                uid = self.current_user.get("uid", "") if self.current_user else ""
                last_state = self.current_user.get("last_state", "Standby")
                base = get_base_dir()

                # 生日处理
                if self.is_birthday_today() and not self.has_birthday_greeted_this_year():
                    # 尝试播放生日音频
                    birthday_sound = None
                    if uid.lower().startswith("admin"):
                        birthday_sound = os.path.join(base, "music", "管理员生日快乐.wav")
                    elif uid.lower().startswith("trailblazer"):
                        birthday_sound = os.path.join(base, "music", "开拓者生日快乐.wav")
                    else:
                        birthday_sound = os.path.join(base, "music", "生日快乐.wav")
                    if os.path.exists(birthday_sound):
                        pygame.mixer.music.load(birthday_sound)
                        pygame.mixer.music.set_volume(0.5)
                        pygame.mixer.music.play()
                    # 发送生日邮件
                    self.send_birthday_mail()
                    return  # 播放完生日语音后不再播放常规欢迎

                # 常规欢迎流程
                if last_state == "Discomfort":
                    if uid.lower().startswith("admin"):
                        sound_file = os.path.join(base, "music", "Admin_back.wav")
                    elif uid.lower().startswith("trailblazer"):
                        sound_file = os.path.join(base, "music", "Trailblazer_back.wav")
                    else:
                        sound_file = os.path.join(base, "music", "User_back.wav")
                    if os.path.exists(sound_file):
                        pygame.mixer.music.load(sound_file)
                        pygame.mixer.music.set_volume(0.5)
                        pygame.mixer.music.play()
                        return

                if uid.lower().startswith("trailblazer"):
                    sound_file = os.path.join(base, "music", "开拓者！欢迎回来！.wav")
                elif uid.lower().startswith("admin"):
                    sound_file = os.path.join(base, "music", "你好，管理员！欢迎回来！.wav")
                else:
                    sound_file = os.path.join(base, "music", "我叫流萤，是鸢尾花家系的译者。.wav")
                if os.path.exists(sound_file):
                    pygame.mixer.music.load(sound_file)
                    pygame.mixer.music.set_volume(0.5)
                    pygame.mixer.music.play()
                else:
                    print(f"欢迎音乐文件不存在: {sound_file}")
            except Exception as e:
                print(f"播放欢迎音乐失败: {e}")

        QTimer.singleShot(500, play_welcome)

    def _get_color_icon(self, color):
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        if color == "green":
            painter.setBrush(QBrush(QColor(0, 255, 0)))
        elif color == "yellow":
            painter.setBrush(QBrush(QColor(255, 215, 0)))
        else:  # red
            painter.setBrush(QBrush(QColor(255, 0, 0)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 12, 12)
        painter.end()
        return QIcon(pixmap)

    def create_icon_text_action(self, text, icon_path, parent_menu, enabled=True, tooltip=""):
        widget = QWidget()
        widget.setAttribute(Qt.WA_StyledBackground, True)
        widget.setStyleSheet("QWidget { background: transparent; border-radius: 4px; }")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(25, 7, 4, 7)
        layout.setSpacing(4)

        # 图标（彩色原图）：用 ColorPixmapLabel 自绘 —— 普通 QLabel 在禁用态
        # （背包条目 enabled=False）会被 QStyle 灰度化，图标就"没颜色"了
        pixmap = QPixmap(icon_path)
        if not pixmap.isNull():
            pixmap = pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label = ColorPixmapLabel(pixmap)
            layout.addWidget(icon_label)

        # 文字
        text_label = QLabel(text)
        text_label.setStyleSheet("color: #000000;")
        layout.addWidget(text_label)
        layout.addStretch()

        hl_style = "QWidget { background: LightSkyBlue; border-radius: 4px; }"
        tr_style = "QWidget { background: transparent; border-radius: 4px; }"

        def on_enter(event):
            # 确定性高亮：把菜单当前动作切换为本动作——QMenu 按原生状态机清除
            # 上一项的高亮；本项高亮由控件自绘（QMenu 不为 widget 项绘制高亮）。
            # 旧实现的 setActiveAction(None)+延迟重绘与原生高亮竞争，
            # 导致相邻两项概率性同时高亮
            parent_menu.setActiveAction(action)
            widget.setStyleSheet(hl_style)
            event.accept()

        def on_leave(event):
            widget.setStyleSheet(tr_style)
            event.accept()

        widget.enterEvent = on_enter
        widget.leaveEvent = on_leave

        action = QWidgetAction(parent_menu)
        action.setDefaultWidget(widget)
        action.setEnabled(enabled)
        action.setToolTip(tooltip)

        # 菜单收起时清掉残留高亮
        parent_menu.aboutToHide.connect(lambda: widget.setStyleSheet(tr_style))
        return action

    # ---------- 资源修复：居中窗口 + 全程锁定交互 ----------
    def _ui_locked(self):
        """资源修复期间：所有窗口/菜单/托盘交互都要被挡住"""
        return bool(getattr(self, '_repairing', False))

    def _repair_mark_windows_closed(self):
        """修复期间收掉已打开的子窗口（商店/聊天/卡池页等）"""
        for _attr in ('shop_window', 'chat_window', 'gacha_view'):
            try:
                _w = getattr(self, _attr, None)
                if _w is not None:
                    _w.close()
                    setattr(self, _attr, None)
            except Exception:
                pass

    def _show_repair_dialog(self):
        """居中、与客户端配色一致的修复进度窗口"""
        dlg = QDialog(self)
        dlg.setWindowTitle("资源修复")
        dlg.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        dlg.setFixedSize(420, 240)
        dlg.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border: 2px solid #4CAF50;
                      border-radius: 10px; }
            QLabel { color: #E0E0E0; font-size: 14px; }
            QLabel#repair_title { font-size: 16px; font-weight: bold; color: #FFFFFF; }
            QLabel#repair_step { color: #BDBDBD; font-size: 13px; }
            QProgressBar { border: 1px solid #555; border-radius: 6px;
                           background: #2D2D2D; text-align: center; color: #EEE;
                           height: 18px; }
            QProgressBar::chunk { background-color: #4CAF50; border-radius: 5px; }
            QPushButton { background-color: #3A3A3A; color: #EEEEEE;
                          border: 1px solid #555555; border-radius: 8px;
                          padding: 8px 16px; font-size: 13px; }
            QPushButton:hover { background-color: #4CAF50; color: white; }
            QPushButton:disabled { color: #888888; background-color: #2A2A2A; }
        """)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(14)
        title = QLabel("🔧 正在修复资源")
        title.setObjectName("repair_title")
        title.setAlignment(Qt.AlignCenter)
        step = QLabel("准备中…")
        step.setObjectName("repair_step")
        step.setAlignment(Qt.AlignCenter)
        step.setWordWrap(True)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        tip = QLabel("修复期间请不要操作，完成后会自动恢复")
        tip.setObjectName("repair_step")
        tip.setAlignment(Qt.AlignCenter)
        btn = QPushButton("修复中…")
        btn.setEnabled(False)
        lay.addWidget(title)
        lay.addWidget(step)
        lay.addWidget(bar)
        lay.addWidget(tip)
        lay.addWidget(btn, 0, Qt.AlignCenter)
        # 修复窗口按"整屏居中"（不跟随桌宠窗口位置）
        try:
            _scr = QApplication.primaryScreen().availableGeometry()
            dlg.move(_scr.x() + (_scr.width() - dlg.width()) // 2,
                     _scr.y() + (_scr.height() - dlg.height()) // 2)
        except Exception:
            self._center_dialog_on_parent_or_screen(dlg)
        dlg.repair_bar = bar
        dlg.repair_step = step
        dlg.repair_btn = btn
        dlg.repair_title = title
        dlg.repair_lock = True
        try:
            dlg.setWindowModality(Qt.ApplicationModal)
        except Exception:
            pass
        dlg.show()
        return dlg

    def repair_all_resources(self):
        """🔧 资源修复：强制重新下载/覆盖**所有**新内容（置顶菜单项）。

        覆盖：卡池页面与 mp4 视频资源（清签名强制重新铺开基底）、卡池封面与物品展示图、
        自定义物品/时装资源、商店图片、所有需要资源包的菜单资源。
        修复期间弹出居中窗口并**锁定一切交互**（右键/双击/托盘/其它窗口），完成后解锁。
        """
        if self._ui_locked():
            return
        self._repairing = True
        try:
            self._repair_mark_windows_closed()
        except Exception:
            pass
        # 托盘菜单：修复期间置为不可用（连托盘右键也点不动）
        try:
            if getattr(self, 'tray_icon', None):
                _m = RoundedMenu()
                _a = _m.addAction("🔧 资源修复中，请稍候…")
                _a.setEnabled(False)
                self.tray_icon.setContextMenu(_m)
        except Exception:
            pass
        dlg = self._show_repair_dialog()

        def _menu_items(items):
            out = []
            for it in (items or []):
                if not isinstance(it, dict):
                    continue
                out.append(it)
                out.extend(_menu_items(it.get('children')))
            return out

        def step_gacha_page():
            """卡池页面 + mp4 视频 + 基底图片：清签名后整包重铺"""
            n = 0
            for it in _menu_items(self.server_custom_menu):
                if it.get('type') != 'gacha' or not it.get('id'):
                    continue
                mid = it['id']
                _p = os.path.join(self.server_resource_dir, mid, '.gacha_base_sig')
                try:
                    if os.path.exists(_p):
                        os.remove(_p)
                except Exception:
                    pass
                self._ensure_gacha_base(mid)
                self._gacha_log("资源修复：已重新铺开卡池页面与视频 " + str(mid))
                n += 1
            return n

        def step_images():
            """卡池封面 + 物品展示图：清掉签名/版本标记后强制重下"""
            n = 0
            for it in _menu_items(self.server_custom_menu):
                if it.get('type') != 'gacha' or not it.get('id'):
                    continue
                mid = it['id']
                img_dir = os.path.join(self.server_resource_dir, mid, 'imgs')
                try:
                    if os.path.isdir(img_dir):
                        for fn in os.listdir(img_dir):
                            if fn.endswith('.sig') or fn.endswith('.ver'):
                                try:
                                    os.remove(os.path.join(img_dir, fn))
                                except Exception:
                                    pass
                except Exception:
                    pass
                self._sync_item_images(mid)
                n += 1
            return n

        def step_custom():
            self.sync_custom_items()
            return 1

        def step_shop():
            self._sync_shop_images_local()
            return 1

        def step_packs():
            if any(it.get('resource_required') for it in _menu_items(self.server_custom_menu)):
                self._force_redownload_all_packs()
                return 1
            return 0

        steps = [
            ("正在覆盖卡池页面与视频资源…", step_gacha_page),
            ("正在重下卡池封面与物品图片…", step_images),
            ("正在重下自定义物品/时装资源…", step_custom),
            ("正在重下商店图片…", step_shop),
            ("正在重下资源包…", step_packs),
        ]

        total = len(steps)
        state = {'i': 0, 'done': 0, 'failed': 0}

        def run_next():
            if state['i'] >= total:
                try:
                    dlg.repair_title.setText("✅ 资源修复完成")
                    _msg = "已重新下载/覆盖 %d 项" % state['done']
                    if state['failed']:
                        _msg += "，%d 项失败（详见控制台日志）" % state['failed']
                    dlg.repair_step.setText(_msg)
                    dlg.repair_bar.setValue(100)
                    dlg.repair_btn.setText("关闭")
                    dlg.repair_btn.setEnabled(True)
                    try:
                        dlg.repair_btn.clicked.disconnect()
                    except Exception:
                        pass
                    dlg.repair_btn.clicked.connect(lambda: (dlg.close(), dlg.deleteLater()))
                    dlg.repair_lock = False
                except Exception:
                    pass
                try:
                    self._refresh_open_gacha_views()
                except Exception:
                    pass
                self._repairing = False
                try:
                    if getattr(self, 'tray_icon', None):
                        self.tray_icon.setContextMenu(self.create_menu())
                except Exception:
                    pass
                self._gacha_log("资源修复完成 done=%d failed=%d"
                                % (state['done'], state['failed']))
                print("[资源修复] 完成 done=%d failed=%d" % (state['done'], state['failed']))
                return
            label, fn = steps[state['i']]
            try:
                dlg.repair_step.setText(label)
                dlg.repair_bar.setValue(int(state['i'] * 100 / total))
            except Exception:
                pass
            try:
                state['done'] += int(fn() or 0)
            except Exception as e:
                state['failed'] += 1
                print("[资源修复] %s 失败: %s" % (label, e))
            state['i'] += 1
            try:
                dlg.repair_bar.setValue(int(state['i'] * 100 / total))
            except Exception:
                pass
            QTimer.singleShot(60, run_next)   # 让窗口有机会刷新

        QTimer.singleShot(120, run_next)

    def _force_redownload_all_packs(self):
        """强制重下所有 resource_required 的资源包（忽略本地是否已存在）"""
        def collect(items):
            out = []
            for it in (items or []):
                if not isinstance(it, dict):
                    continue
                if it.get('resource_required'):
                    out.append(it)
                out.extend(collect(it.get('children')))
            return out

        for it in collect(self.server_custom_menu):
            try:
                # download_resource 会直接重新下载并覆盖解压，天然就是"强制"
                self.download_resource(it, silent=True)
            except Exception as e:
                print(f"[资源修复] 资源包 {it.get('id')} 下载失败: {e}")

    def _reset_all_resources(self):
        """重新下载所有需要资源的资源包（覆盖原有）"""

        def collect_resource_items(items):
            result = []
            for item in items:
                if item.get('resource_required', False):
                    result.append(item)
                if 'children' in item:
                    result.extend(collect_resource_items(item['children']))
            return result

        to_download = collect_resource_items(self.server_custom_menu)
        if not to_download:
            QMessageBox.information(self, "提示", "没有需要下载的资源包。")
            return

        # 自定义深色居中确认对话框
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("确认重置")
        msg_box.setText(f"将重新下载 {len(to_download)} 个资源包（覆盖已有），确定继续吗？")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)

        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                color: #E0E0E0;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
                background-color: #1E1E1E;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton:disabled {
                background-color: #666;
            }
        """)
        # 居中
        parent_geo = self.geometry()
        x = parent_geo.x() + (parent_geo.width() - msg_box.width()) // 2
        y = parent_geo.y() + (parent_geo.height() - msg_box.height()) // 2
        msg_box.move(x, y)

        reply = msg_box.exec_()
        if reply != QMessageBox.Yes:
            return

        for item in to_download:
            self.download_resource(item, silent=True)

    def contextMenuEvent(self, event):
        # 资源修复期间：不弹任何菜单
        if self._ui_locked():
            try:
                event.accept()
            except Exception:
                pass
            return
        if self.is_moving:
            return  # 移动中不弹出菜单
        if self.connecting:  # 正在连接，不弹出菜单
            return
        if self.rename_required:
            QToolTip.showText(event.globalPos(), "请先设置昵称", self)
            return
        current_cursor = self.cursor()
        QApplication.setOverrideCursor(current_cursor)
        # ✅ 根据联机状态获取背包数据
        if self.mp_client.connected and self.online_inventory:
            inv = self.online_inventory
        else:
            inv = self.current_user

        balance = inv.get('balance', 0)
        credit = inv.get('credit', 0)
        tickets = inv.get('star_rail_tickets', 0)
        owned = inv.get('owned_clothes', ['normal'])

        try:
            menu = RoundedMenu(self)
            menu.setToolTipsVisible(True)
            if self.is_debugger:
                action0 = QAction(f'Firefly_Win64_v{CLIENT_VERSION}_G [debug]', self)
            else:
                action0 = QAction(f'Firefly_Win64_v{CLIENT_VERSION}_G', self)
            action0.setDisabled(True)
            action1 = QAction('🍖 投喂', self)  # 添加 emoji
            action2 = QAction('💖 比心', self)  # 添加 emoji
            action3 = QAction('😴 睡觉', self)  # 添加 emoji
            action_new = QAction('🔄 检查更新', self)  # 添加 emoji
            action100 = QAction('💬 在线对话', self)
            if not self.ai_enabled:
                action100.setEnabled(False)
                action100.setToolTip("⚠此功能已被服务器禁止！")

            menu.addAction(action0)
            menu.addAction(action1)
            menu.addAction(action2)
            menu.addAction(action3)

            if self.current_animation_state == "Sleep":
                action4 = QAction('✨ 唤醒', self)  # 添加 emoji
                action4.triggered.connect(self.wake_up)
                menu.addAction(action4)

            action_mailbox = QAction('📬 邮箱', self)
            menu.addAction(action_mailbox)
            shop_action = QAction('🛒 商店', self)  # 添加 emoji
            shop_action.triggered.connect(self.open_shop)
            menu.addAction(shop_action)

            # 背包子菜单
            bag = RoundedMenu(menu)
            bag.setTitle("🎒 背包")
            bag.setToolTipsVisible(True)
            items_added = False
            # 背包条目统一用「真实图片 + 文本」；取不到图片才回退「emoji + 文本」。
            # 用 create_icon_text_action 自绘：图标 16×16 彩色、和原生图标列对齐
            # （直接 setIcon 时 Qt5 会把图标贴最左侧、还常被压扁 —— 用户反馈的错位/没颜色）
            def _bag_action(text_with_emoji, text_plain, item_type, item_id=None, info=None):
                _ip = self.item_icon_path(item_type, item_id, info)
                if _ip:
                    try:
                        return self.create_icon_text_action(text_plain, _ip, bag,
                                                            enabled=False)
                    except Exception as _e:
                        print(f"[背包] 图片图标失败({item_id}): {_e}")
                act = QAction(text_with_emoji, self)
                act.setDisabled(True)
                return act

            if balance > 0:
                bag.addAction(_bag_action(f'🍰 橡木蛋糕卷：{balance}个',
                                          f'橡木蛋糕卷：{balance}个', 'cake'))
                items_added = True
            if credit > 0:
                bag.addAction(_bag_action(f'💰 信用点：{credit}',
                                          f'信用点：{credit}', 'credit'))
                items_added = True
            if tickets > 0:
                bag.addAction(_bag_action(f'🎫 星铁专票：{tickets}张',
                                          f'星铁专票：{tickets}张', 'star_rail_ticket'))
                items_added = True
            jade = inv.get('jade', 0)
            if jade > 0:
                bag.addAction(_bag_action(f'💎 星琼：{jade}', f'星琼：{jade}', 'jade'))
                items_added = True
            custom_items = inv.get('custom_items', {})
            # 单机模式不显示"服务器新增物品"（用户要求）：这些物品的定义与图片都来自服务器，
            # 离线时显示出来只会是一串 id，容易误解
            if custom_items and not self.mp_client.connected:
                custom_items = {}
            if custom_items:
                if items_added:
                    bag.addSeparator()
                for cid, cnt in custom_items.items():
                    info = next((it for it in self.known_custom_items() if it.get('id') == cid), None)
                    # 单机模式没有服务端下发 → 用本地缓存解析名称（以前直接显示物品 id）
                    name = (info or {}).get('name') or self.custom_item_name(cid) or cid
                    icon = info.get('icon', '📦') if info else '📦'
                    bag.addAction(_bag_action(f'{icon} {name} × {cnt}', f'{name} × {cnt}',
                                              (info or {}).get('type') or 'custom', cid, info))
                    items_added = True
            if not items_added:
                empty_action = QAction("暂无物品", self)
                empty_action.setDisabled(True)
                bag.addAction(empty_action)
            menu.addMenu(bag)

            # 时装子菜单
            clothes_menu = RoundedMenu(menu)
            clothes_menu.setTitle("👗 时装")
            clothes_menu.setToolTipsVisible(True)
            for clothes_id, info in AVAILABLE_CLOTHES.items():
                action = QAction(info["name"], self)
                if clothes_id in owned:
                    action.triggered.connect(lambda checked, cid=clothes_id: self.apply_clothes(cid))
                else:
                    action.setEnabled(False)
                    action.setToolTip(info.get("description", "未拥有"))
                clothes_menu.addAction(action)
            menu.addMenu(clothes_menu)

            # 音量子菜单
            volume_menu = RoundedMenu("🔊 音量")
            volume_action = create_volume_menu(self)
            volume_menu.addAction(volume_action)
            menu.addMenu(volume_menu)

            # 签到
            if self.is_signed_today():
                sign_action = menu.addAction("✅ 今日已签到")
                sign_action.setDisabled(True)
            else:
                sign_action = menu.addAction("📅 每日签到")
                sign_action.triggered.connect(self.sign_in)
            menu.addAction(sign_action)

            # 游戏社区（单机/联机都有）：把原「小三月日记」收进来做二级菜单
            community = RoundedMenu(menu)
            community.setTitle("🏘️ 游戏社区")
            community.setToolTipsVisible(True)

            # 小三月的秘密基地——图标使用内置 MARCH_7TH.png（用户指定），
            # 悬浮高亮采用确定性状态切换（见 create_icon_text_action），无双高亮
            OPEN_TIME = datetime(2026, 8, 6, 12, 0, 0)
            now = datetime.now()
            icon_path = os.path.join(get_base_dir(), "assets", "images", "MARCH_7TH.png")
            tooltip = "为所有崩铁玩家打造了一个属于我们的新世界！——小三月日记"

            if self._website_status is False:
                is_open = False
                display_text = "小三月日记(维护中)"
                tooltip_text = self._website_title if self._website_title else "公测延期，敬请期待"
            elif self._website_status is True:
                is_open = True
                display_text = "小三月日记"
                tooltip_text = tooltip
            else:
                if now < OPEN_TIME:
                    is_open = False
                    display_text = "小三月日记"
                    tooltip_text = ""
                else:
                    is_open = False
                    display_text = "小三月日记(检测中...)"
                    tooltip_text = "正在检测网站状态..."

            # 图标+文字使用 QWidgetAction 自绘：Qt5 原生图标列固定贴在最左侧
            # （无 setIconSize、QIcon 会把非方形位图压扁），无法右移对齐 emoji，
            # 因此沿用图字控件保证图标位置；悬浮高亮改为确定性状态切换（见下）
            secret_action = self.create_icon_text_action(
                display_text, icon_path, community,
                enabled=is_open,
                tooltip=tooltip_text
            )
            secret_action.triggered.connect(lambda: webbrowser.open("https://blog.mar7th.cn/"))
            community.addAction(secret_action)
            menu.addMenu(community)

            # AI唱歌子菜单
            sing = RoundedMenu(menu)
            sing.setTitle("🎤 AI唱歌&二创")
            sing.addAction("🎵 AI合唱-不眠之夜", self.sing_and_dance)
            sing.addAction("🎶 AI独唱-打上花火", self.sing_and_dance2)
            sing.addAction("🎙️ AI独唱-5:20AM", self.sing_and_dance3)
            sing.addAction("✨ 二创-Dream of Firefly", self.sing_and_dance4)
            sing.addAction("⏹️ 停止音乐", self.stop_music)
            menu.addMenu(sing)

            # 更改桌宠大小 + 允许移动
            mouse_GUI = RoundedMenu(menu)
            mouse_GUI.setTitle("🐭 更改桌宠大小")
            mouse_GUI.addAction("🔍 更改：大号", self.change_GUI_to_Large)
            mouse_GUI.addAction("🖥️ 更改：默认", self.change_GUI_to_normal)
            mouse_GUI.addAction("📏 更改：中号", self.change_GUI_to_medium)
            mouse_GUI.addAction("🔬 更改：小号", self.change_GUI_to_small)
            move_action = QAction("🚶‍ 允许移动", self)
            move_action.setCheckable(True)
            move_action.setChecked(self.allow_move)
            move_action.triggered.connect(self.toggle_move)
            mouse_GUI.addAction(move_action)
            menu.addMenu(mouse_GUI)

            menu.addSeparator()
            if self.mp_client.connected:
                report_action = QAction('📊 当月收获报告', self)
                report_action.triggered.connect(self.generate_monthly_report)
                menu.addAction(report_action)

            # 修改指针子菜单
            mouse = RoundedMenu(menu)
            mouse.setTitle("🖱️ 修改指针")
            mouse.addAction("🪲 流萤指针", lambda: self.set_cursor_style("Firefly"))
            mouse.addAction("🤖 萨姆指针", lambda: self.set_cursor_style("Sam"))
            mouse.addAction("🚫 停用指针", lambda: self.set_cursor_style("None"))
            menu.addMenu(mouse)

            # ========== 原"更多功能"改为工具箱子菜单 ==========
            toolbox_menu = RoundedMenu(self)
            toolbox_menu.setTitle("🧰 工具箱")
            toolbox_menu.setToolTipsVisible(True)

            # 子选项1：更多工具（原功能）
            more_tools_action = QAction("🛠️ 更多工具", self)
            more_tools_action.triggered.connect(self.open_settings_tool)
            toolbox_menu.addAction(more_tools_action)

            # 子选项2：清空日志
            clear_log_action = QAction("🗑️ 清理日志", self)
            clear_log_action.triggered.connect(self.clear_logs)
            toolbox_menu.addAction(clear_log_action)

            menu.addMenu(toolbox_menu)

            # 检查更新（单独保留）
            menu.addAction(action_new)

            # 在线对话
            menu.addAction(action100)

            # 兑换码、修改用户名、兑换明细
            action_redeem = QAction('🎟️ 使用兑换码', self)
            if not self.mp_client.connected:
                change_name_action = menu.addAction("✏️ 修改用户名")
                change_name_action.triggered.connect(self.change_username_single)
            action_history = QAction('📋 兑换明细', self)
            menu.addAction(action_redeem)
            menu.addAction(action_history)

            # 多人联机子菜单
            # 多人联机子菜单
            if self.is_debugger and not self.debug_mode:
                # 调试模式且未开启联机 → 禁用
                mp_menu = RoundedMenu(menu)
                mp_menu.setTitle("🌐 多人联机 (调试模式已禁用)")
                disabled_action = QAction("联机功能已禁用", self)
                disabled_action.setEnabled(False)
                mp_menu.addAction(disabled_action)
                menu.addMenu(mp_menu)
            else:
                # 正常联机菜单（保持不变）
                mp_menu = RoundedMenu(menu)
                if self.mp_client.connected:
                    latency = self.mp_client._latest_latency_ms
                    if latency < 0:
                        latency = 0
                    if latency <= 40:
                        icon = self._get_color_icon("green")
                    elif latency <= 119:
                        icon = self._get_color_icon("yellow")
                    else:
                        icon = self._get_color_icon("red")
                    mp_menu.setIcon(icon)
                    mp_menu.setTitle(f"🌐 多人联机 (延迟{latency}ms)")
                    name_action = mp_menu.addAction(self.server_display_name)
                    name_action.setEnabled(False)
                    chat_action = mp_menu.addAction("💬 聊天室")
                    chat_action.triggered.connect(self.open_chat_window)
                    chat_action.setEnabled(self.chat_enabled)
                    if not self.chat_enabled:
                        chat_action.setToolTip("⚠此功能已被服务器禁止！")
                    rename_action = mp_menu.addAction("✏️ 修改昵称")
                    rename_action.triggered.connect(self.change_online_name)
                    player_list_action = mp_menu.addAction("📋 成员列表")
                    player_list_action.triggered.connect(self.show_player_list)
                    # 注销账号（冷却期内为取消注销）放在退出游戏之前，"退出游戏"保持最底部
                    if getattr(self, 'destroy_pending_until', None) and self.current_user:
                        remain = self._destroy_remaining_text()
                        destroy_action = mp_menu.addAction(f"❌ 取消注销(剩余时间{remain})")
                        destroy_action.setData('destroy_cancel')
                        destroy_action.triggered.connect(self.cancel_account_destroy)
                        self._destroy_action = destroy_action
                    else:
                        self._destroy_action = None
                        destroy_action = mp_menu.addAction("🗑️ 注销账号")
                        destroy_action.triggered.connect(self.start_account_destroy)
                    leave_action = mp_menu.addAction("🚪 退出游戏")
                    leave_action.triggered.connect(self.disconnect_server)
                else:
                    mp_menu.setTitle("🌐 多人联机")
                    join_action = mp_menu.addAction("🎮 加入游戏")
                    join_action.triggered.connect(self.show_join_dialog)
                    join_action2 = mp_menu.addAction("🖥️ 获取服务端")
                    join_action2.triggered.connect(self.get_server)
                    destroy_action = mp_menu.addAction("🗑️ 注销账号")
                    destroy_action.triggered.connect(self.start_account_destroy)
                menu.addMenu(mp_menu)

            # 服务器内容子菜单（动态）
            if self.server_custom_menu:
                server_menu = RoundedMenu(self)
                server_menu.setTitle("第三方的服务器功能")
                server_menu.setToolTipsVisible(True)
                # 🔧 资源修复：置顶。不依赖是否有资源包，点击即重新下载/覆盖所有新内容
                repair_action = QAction("🔧 资源修复", self)
                repair_action.setToolTip("重新下载/覆盖所有新内容：卡池页面、卡池封面与物品图、"
                                         "自定义物品/时装资源、商店图片、资源包")
                repair_action.triggered.connect(self.repair_all_resources)
                server_menu.addAction(repair_action)
                server_menu.addSeparator()
                info_action = QAction("以下功能均来自服务器，与游戏作者无关", self)
                info_action.setEnabled(False)
                server_menu.addAction(info_action)
                server_menu.addSeparator()

                def has_resource_required(items):
                    for item in items:
                        if item.get('resource_required', False):
                            return True
                        if 'children' in item and has_resource_required(item['children']):
                            return True
                    return False

                if has_resource_required(self.server_custom_menu):
                    reset_action = QAction("🔧 资源重置", self)
                    reset_action.setToolTip("重新下载所有需要资源的资源包（覆盖原有）")
                    reset_action.triggered.connect(self._reset_all_resources)
                    server_menu.addAction(reset_action)
                    server_menu.addSeparator()

                def add_menu_items(parent_menu, items):
                    for item in items:
                        expiry = item.get('expiry')
                        if expiry:
                            try:
                                dt = datetime.fromisoformat(expiry)
                                if datetime.now(dt.tzinfo) >= dt:
                                    continue
                            except:
                                pass

                        label = item.get('label', '未知')
                        res_required = item.get('resource_required', False)
                        res_exists = self._resource_exists(item.get('id', ''))
                        if res_required and not res_exists:
                            label += " (未下载)"
                        if item.get('type') == 'gacha':
                            remaining = self._get_remaining_text(expiry)
                            if remaining:
                                label += remaining

                        children = item.get('children', [])
                        if children:
                            sub_menu = RoundedMenu(parent_menu)
                            _ic = item.get('icon', '')
                            _ic = str(_ic) if _ic else ''
                            if _ic and len(_ic) <= 4 and not _ic.startswith(('http', '/')):
                                sub_menu.setTitle(f"{_ic} {label}")
                            else:
                                sub_menu.setTitle(label)
                            if res_required and not res_exists:
                                sub_menu.setEnabled(False)
                                sub_menu.setToolTip("资源未下载，无法使用此功能")
                            add_menu_items(sub_menu, children)
                            parent_menu.addMenu(sub_menu)
                        else:
                            action = self._create_server_menu_item(item)
                            # 注意：不要在这里 setText(label) —— 那会把图标前缀覆盖掉
                            if res_required and not res_exists:
                                action.setEnabled(False)
                                action.setToolTip(action.toolTip() + " 你需要先下载服务器资源包才能使用此功能!")
                            action.triggered.connect(lambda checked, it=item: self._on_server_menu_clicked(it))
                            parent_menu.addAction(action)

                add_menu_items(server_menu, self.server_custom_menu)
                menu.addMenu(server_menu)

            # 用户信息
            user_info = menu.addAction(f"👤当前登录：{self.get_display_uid()}")
            user_info.setDisabled(True)
            menu.addAction(user_info)

            # 退出桌宠
            exit_all_action = QAction('🚪 退出桌宠', self)
            exit_all_action.triggered.connect(self.show_exit_dialog)
            menu.addAction(exit_all_action)

            # 连接信号（保持不变）
            action1.triggered.connect(self.feed)
            action2.triggered.connect(self.heart)
            action3.triggered.connect(self.sleep)
            action_new.triggered.connect(self.check_new)
            action100.triggered.connect(self.open_AI)
            action_redeem.triggered.connect(self.redeem_code)
            action_history.triggered.connect(self.show_history)
            action_mailbox.triggered.connect(self.open_mailbox)

            pixmap_path = os.path.join(get_base_dir(), "mouse", "Firefly", "p1.gif")
            pixmap = QPixmap(pixmap_path) if os.path.exists(pixmap_path) else QPixmap(16, 16)
            cursor = QCursor(pixmap)
            menu.setCursor(cursor)
            menu.exec_(event.globalPos())
        finally:
            QApplication.restoreOverrideCursor()

    def clear_logs(self):
        """
        清空客户端日志文件（firefly_crash.log 和 firefly_client.log）
        """
        # 创建自定义 QMessageBox 并应用深色样式
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("确认清理日志")
        msg_box.setText("确定要清理所有日志文件吗？\n（这将清理 crash 日志和客户端日志）")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)

        # 应用深色样式（与 show_centered_message 保持一致）
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #888888;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton[text="No"] {
                background-color: #666666;
            }
            QPushButton[text="No"]:hover {
                background-color: #888888;
            }
        """)

        # 居中显示
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg_box.move(screen_center - msg_box.rect().center())

        reply = msg_box.exec_()
        if reply == QMessageBox.No:
            return

        log_files = ["firefly_crash.log", "firefly_client.log"]
        cleared = []
        for f in log_files:
            if os.path.exists(f):
                try:
                    open(f, 'w').close()
                    cleared.append(f)
                except Exception as e:
                    print(f"清理 {f} 失败: {e}")

        if cleared:
            self.show_centered_message("成功", f"已清理日志文件：{', '.join(cleared)}", QMessageBox.Information)
        else:
            self.show_centered_message("提示", "没有找到可清理的日志文件", QMessageBox.Information)

    def get_server(self):
        webbrowser.open("https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online/releases")

    def toggle_move(self, checked):
        self.allow_move = checked
        if self.current_user:
            self.current_user["allow_move"] = checked
            self._save_current_user()
        if checked:
            delay = random.randint(5 * 60, 15 * 60) * 1000
            self.move_timer.start(delay)
        else:
            self.move_timer.stop()
            if self.is_moving:
                self.stop_move()

    def start_random_move(self):
        if not self.allow_move or self.is_moving:
            return
        screen = QApplication.primaryScreen().availableGeometry()

        # 水平移动距离范围：屏幕宽度的 1/6 ~ 2.0/6 （最大范围减少0.5/6）
        min_ratio = 1.0 / 6.0
        max_ratio = 2.0 / 6.0
        max_dx = screen.width() * random.uniform(min_ratio, max_ratio)
        min_horizontal = max(30, screen.width() * 0.05)  # 最小水平位移
        if max_dx < min_horizontal:
            max_dx = min_horizontal * 2

        # 强制水平分量不为零
        direction = random.choice([-1, 1])
        dx = direction * random.uniform(min_horizontal, max_dx)

        # 垂直位移范围，可为正、负或零
        min_ratio_v = 1.0 / 6.0
        max_ratio_v = 2.0 / 6.0
        max_dy = screen.height() * random.uniform(min_ratio_v, max_ratio_v)
        dy = random.uniform(-max_dy, max_dy)

        target_x = self.x() + dx
        target_y = self.y() + dy

        # 边界约束
        target_x = max(0, min(screen.width() - self.width(), target_x))
        target_y = max(0, min(screen.height() - self.height(), target_y))

        # 如果水平位移太小（边界导致），放弃此次移动
        if abs(target_x - self.x()) < min_horizontal:
            self.move_timer.start(random.randint(5 * 60, 15 * 60) * 1000)
            return

        self.move_start_x, self.move_start_y = self.x(), self.y()
        self.move_target_x, self.move_target_y = target_x, target_y

        distance = math.hypot(target_x - self.x(), target_y - self.y())
        if distance < 10:
            self.move_timer.start(random.randint(5 * 60, 15 * 60) * 1000)
            return

        self.move_duration = distance / self.move_speed
        self.move_start_time = time.time()
        self.is_moving = True

        # 暂停情感检查计时器
        if self.emo_check_timer.isActive():
            self.emo_check_timer.stop()

        # 播放行走动画
        direction_str = "right" if target_x >= self.x() else "left"
        self._play_walk_animation(direction_str)

        self.move_animation_timer.start(30)

    def _play_walk_animation(self, direction):
        size_suffix = ""
        markers = list_size_markers()
        if "Large" in markers:
            size_suffix = "_Large"
        elif "small" in markers:
            size_suffix = "_small"
        elif "medium" in markers:
            size_suffix = "_medium"
        base = get_base_dir()
        gif_path = asset_path("images", "firefly", "actions", direction,
                                f"{direction}{size_suffix}.gif")
        if not os.path.exists(gif_path):
            gif_path = asset_path("images", "firefly", "actions", direction, f"{direction}.gif")
        new_movie = QMovie(gif_path)
        self.label.setMovie(new_movie)
        self.movie = new_movie
        self.movie.start()

    def stop_move(self):
        """立即停止随机移动并恢复待机动画（原代码缺失该方法导致崩溃）"""
        self.move_animation_timer.stop()
        self.is_moving = False
        self.changeToStandby()
        if not self.emo_check_timer.isActive():
            self.emo_check_timer.start(1000)

    def update_move_position(self):
        elapsed = time.time() - self.move_start_time
        if elapsed >= self.move_duration:
            self.move(int(self.move_target_x), int(self.move_target_y))
            self.is_moving = False
            self.move_animation_timer.stop()
            self.changeToStandby()
            if not self.emo_check_timer.isActive():
                self.emo_check_timer.start(1000)
            if self.allow_move:
                delay = random.randint(5 * 60, 15 * 60) * 1000
                self.move_timer.start(delay)
        else:
            progress = elapsed / self.move_duration
            x = self.move_start_x + (self.move_target_x - self.move_start_x) * progress
            y = self.move_start_y + (self.move_target_y - self.move_start_y) * progress
            self.move(int(x), int(y))

    def wake_up(self):
        if self.anim_restore_timer and self.anim_restore_timer.isActive():
            self.anim_restore_timer.stop()
        self.anim_restore_timer = None

        self.sleep_start_time = None

        last_state = self.current_user.get("last_state", "Standby") if self.current_user else "Standby"

        if last_state == "Discomfort":
            QTimer.singleShot(50, self._do_wake_up_discomfort)
        else:
            QTimer.singleShot(50, self._do_wake_up_standby)

        if hasattr(self, 'wake_action_tray') and self.wake_action_tray:
            QTimer.singleShot(150, lambda: self.wake_action_tray.setVisible(False))

    def _do_wake_up_standby(self):
        self.changeToStandby()
        self.record_interaction()

    def _do_wake_up_discomfort(self):
        self.changeToDiscomfort()

    def create_menu(self):
        # 退出登录/注销流程中 current_user 可能已清空：此时不再重建菜单，避免空引用崩溃
        if not self.current_user:
            return
        if self.connecting or self.is_moving:
            return
        if self.connecting:  # 正在连接，不创建菜单（托盘菜单会因此不可用）
            return
        menu = RoundedMenu()
        menu.setToolTipsVisible(True)
        if self.is_debugger:
            fuck_Manthe = menu.addAction(f"Firefly_Win64_v{CLIENT_VERSION}_G [debug]")
        else:
            fuck_Manthe = menu.addAction(f"Firefly_Win64_v{CLIENT_VERSION}_G")
        fuck_Manthe.setDisabled(True)
        feed_action = menu.addAction("🍖 投喂")
        love_action = menu.addAction("💖 比心")
        mailbox_action = menu.addAction("📬 邮箱")
        mailbox_action.triggered.connect(self.open_mailbox)
        volume_menu = RoundedMenu("🔊 音量")
        volume_action = create_volume_menu(self)
        volume_menu.addAction(volume_action)
        menu.addMenu(volume_menu)

        if self.is_signed_today():
            sign_action = menu.addAction("✅ 今日已签到")
            sign_action.setDisabled(True)
        else:
            sign_action = menu.addAction("📅 每日签到")
            sign_action.triggered.connect(self.sign_in)

        menu.addAction("🔧 更多功能", self.open_settings_tool)
        menu.addAction("🚀 脱离卡死", self.teleport_to_center)
        menu.addAction("🔄 检查更新", self.check_new)
        ai_action = QAction("💬 在线对话", self)
        ai_action.triggered.connect(self.AI)
        if not self.ai_enabled:
            ai_action.setEnabled(False)
            ai_action.setToolTip("⚠此功能已被服务器禁止！")
        menu.addAction(ai_action)
        redeem_action = menu.addAction("🎟️使用兑换码")
        redeem_action.triggered.connect(self.redeem_code)
        history_action = menu.addAction("📋 兑换明细")
        history_action.triggered.connect(self.show_history)
        user_info = menu.addAction(f"👤当前登录：{self.get_display_uid()}")
        user_info.setDisabled(True)
        exit_all_action = menu.addAction("🚪 退出桌宠")
        exit_all_action.triggered.connect(self.show_exit_dialog)
        feed_action.triggered.connect(self.feed)
        love_action.triggered.connect(self.heart)
        self.tray_icon.setContextMenu(menu)
        self.set_default_cursor()
        self.tray_icon.show()

    def teleport_to_center(self):
        """确认后移动桌宠窗口到屏幕中央，刷新当前动画，不改变任何状态"""
        # 自定义确认对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("脱离卡死")
        dialog.setFixedSize(400, 150)
        dialog.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #CCCCCC;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 20px;
                font-size: 14px;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton#cancel_btn {
                background-color: #666666;
            }
            QPushButton#cancel_btn:hover {
                background-color: #888888;
            }
        """)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(30, 30, 30, 20)
        layout.setSpacing(20)

        label = QLabel("注意！此功能仅限桌宠超出屏幕范围时使用，你确定要使用吗？")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("cancel_btn")
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        # 对话框居中
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())

        if dialog.exec_() == QDialog.Accepted:
            # 1. 移动窗口到屏幕中央
            screen = QApplication.primaryScreen().geometry()
            win_rect = self.geometry()
            if win_rect.width() == 0 or win_rect.height() == 0:
                win_rect.setWidth(self.width())
                win_rect.setHeight(self.height())
            x = screen.x() + (screen.width() - win_rect.width()) // 2
            y = screen.y() + (screen.height() - win_rect.height()) // 2
            self.move(x, y)

            # 保存位置
            if self.current_user:
                self.current_user["last_x"] = x
                self.current_user["last_y"] = y
                self._save_current_user()

            # 2. 刷新当前动画（解决透明问题）
            if self.movie:
                self.movie.stop()
                size_suffix = ""
                markers = list_size_markers()
                if "Large" in markers:
                    size_suffix = "_Large"
                elif "small" in markers:
                    size_suffix = "_small"
                elif "medium" in markers:
                    size_suffix = "_medium"
                state = self.current_animation_state
                base = get_base_dir()
                if state == "Sleep":
                    gif_path = asset_path("images", "firefly", "actions", "sleep",
                                            f"sleep{size_suffix}.gif")
                    if not os.path.exists(gif_path):
                        gif_path = asset_path("images", "firefly", "actions", "sleep", "sleep.gif")
                elif state == "Discomfort":
                    gif_path = asset_path("images", "firefly", "actions", "Discomfort",
                                            f"Discomfort{size_suffix}.gif")
                    if not os.path.exists(gif_path):
                        gif_path = asset_path("images", "firefly", "actions", "Discomfort",
                                                "Discomfort.gif")
                else:
                    gif_path = asset_path("images", "firefly", "actions", "Standby",
                                            f"Standby{size_suffix}.gif")
                    if not os.path.exists(gif_path):
                        gif_path = asset_path("images", "firefly", "actions", "Standby",
                                                "Standby.gif")
                if os.path.exists(gif_path):
                    new_movie = QMovie(gif_path)
                    self.label.setMovie(new_movie)
                    self.movie = new_movie
                    self.movie.start()
                else:
                    if state == "Sleep":
                        self.sleep()
                    elif state == "Discomfort":
                        self.changeToDiscomfort()
                    else:
                        self.changeToStandby()

    def logout(self):
        self.current_user["last_state"] = self.persistent_state
        self._save_current_user()
        self.current_user = None
        self.hide()
        self.tray_icon.setVisible(False)
        self.show_login_again()

    def show_login_again(self):
        login_win = LoginWindow()
        login_win.login_success.connect(self.on_login_success)
        login_win.open()

    def on_login_success(self, user_data):
        app = QApplication.instance()
        if hasattr(app, 'firefly') and app.firefly:
            app.firefly.deleteLater()
            app.firefly = None
            QApplication.processEvents()
        app.firefly = Firefly(user_data=user_data)
        app.firefly.init_tray_icon()
        app.firefly.create_menu()
        app.firefly.show()
        app.firefly.restore_last_position()

    def closeEvent(self, event):
        if self.current_user:
            self.current_user["last_state"] = self.persistent_state
            pos = self.pos()
            self.current_user["last_x"] = pos.x()
            self.current_user["last_y"] = pos.y()
            self._save_current_user()
        event.accept()

    def kill_self_and_children(self):
        current_pid = os.getpid()
        current_process = psutil.Process(current_pid)
        try:
            for child in current_process.children(recursive=True):
                try:
                    child.kill()
                except:
                    pass
            current_process.kill()
        except:
            sys.exit()

    def __del__(self):
        if hasattr(self, 'hotkey_filter'):
            try:
                self.hotkey_filter.unregister()
                QApplication.instance().removeNativeEventFilter(self.hotkey_filter)
            except:
                pass

    def out_win(self):
        """
        安全退出桌宠程序，清理所有资源并退出进程。
        此方法会保存当前状态、关闭所有子窗口、停止音乐、注销热键，最后调用 sys.exit(0)。
        """

        # 2. 停止音乐播放
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except Exception as e:
            print(f"[退出] 停止音乐失败: {e}")

        # 3. 保存当前用户状态（动画、位置）到磁盘
        if self.current_user:
            self.current_user["last_state"] = self.persistent_state
            pos = self.pos()
            self.current_user["last_x"] = pos.x()
            self.current_user["last_y"] = pos.y()
            self._save_current_user()

        # 4. 关闭所有可能打开的子窗口
        # 4.1 聊天窗口
        if hasattr(self, 'chat_window') and self.chat_window:
            try:
                self.chat_window.close()
                self.chat_window = None
            except Exception as e:
                print(f"[退出] 关闭聊天窗口失败: {e}")

        # 4.2 兑换码对话框
        if hasattr(self, '_redeem_dialog') and self._redeem_dialog:
            try:
                self._redeem_dialog.reject()
                self._redeem_dialog = None
            except:
                pass

        # 4.3 抽卡页面（QWebEngineView）
        # 在 out_win 中，关闭抽卡窗口部分
        if hasattr(self, 'gacha_view') and self.gacha_view:
            try:
                self.gacha_view.close()  # 这会触发我们定义的 on_close，但因为我们 hide 并 ignore，实际不会关闭，需要强制删除
                self.gacha_view.deleteLater()
            except:
                pass
            self.gacha_view = None
            self._current_gacha_bridge = None

        # 4.4 在线列表悬浮窗
        if hasattr(self, 'online_overlay') and self.online_overlay:
            try:
                self.online_overlay.hide()
                self.online_overlay = None
            except:
                pass

        # 4.5 强制改名对话框
        if hasattr(self, '_rename_dialog') and self._rename_dialog:
            try:
                self._rename_dialog.reject()
                self._rename_dialog = None
            except:
                pass

        # 4.6 其他可能存在的模态对话框（商店、历史记录等）
        # 由于它们是独立的 QDialog，退出时通常自动回收，但主动关闭更安全
        # 这里可以根据需要遍历所有顶层窗口关闭，但为简洁，省略

        # 5. 隐藏主窗口和托盘图标
        self.hide()
        if self.tray_icon:
            self.tray_icon.setVisible(False)
            try:
                self.tray_icon = None
            except:
                pass

        # 6. 结束本进程派生、仍运行在 _MEI 临时目录内的子进程
        #    （QtWebEngine 渲染进程、AI.exe 等）。
        #    修复：此前在退出前启动 tools/kill_process.exe——它本身就位于
        #    _MEI 内，主进程退出时 PyInstaller 清理临时目录必然失败，弹
        #    "Failed to remove temporary directory: ...\\_MEIxxxx"；
        #    且它会按进程名误杀其他桌宠实例（多开测试好友对话时）。
        #    现改为进程内按 _MEI 路径精确清理（见 cleanup_mei_children）
        # 7. 彻底退出应用程序（清理 _MEI 子进程后立即退出）
        _hard_exit(0)


# ====================== 自动登录检查 ======================
def auto_login_check():
    if not os.path.exists(USER_CONFIG):
        return None
    try:
        with open(USER_CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        if config.get("remember") and config.get("userid") and config.get("password"):
            userid = config["userid"]
            pwd = config["password"]
            user_path = os.path.join(USER_DATA_DIR, f"{userid}.json")
            if os.path.exists(user_path):
                try:
                    with open(user_path, encoding="utf-8") as f:
                        user_data = json.load(f)
                except:
                    return None
                if not isinstance(user_data, dict):
                    return None
                if user_data.get("password") == pwd:
                    user_data = ensure_user_fields(user_data, user_path)
                    banned_until = user_data.get("banned_until")
                    if banned_until:
                        try:
                            until_date = datetime.fromisoformat(banned_until)
                            if datetime.now() < until_date:
                                if os.path.exists(USER_CONFIG):
                                    os.remove(USER_CONFIG)
                                return None
                            else:
                                user_data["banned"] = False
                                user_data["banned_until"] = None
                                with open(user_path, "w", encoding="utf-8") as f:
                                    json.dump(user_data, f, ensure_ascii=False, indent=2)
                        except:
                            pass
                    if user_data.get("banned"):
                        if os.path.exists(USER_CONFIG):
                            os.remove(USER_CONFIG)
                        return None
                    return user_data
    except:
        pass
    return None

# ====================== 主程序 ======================
if __name__ == '__main__':
    try:
        import signal
        firefly = None
        # 取证：把 Python 调用栈 dump 到崩溃日志——即使发生 qFatal/abort 等
        # 原生崩溃（无 Python 异常），也能看到崩在哪一行
        try:
            import faulthandler as _faulthandler
            _crash_fh = open(os.path.join(get_writable_dir(), "firefly_crash.log"),
                             "a", encoding="utf-8")
            _faulthandler.enable(_crash_fh)
        except Exception:
            pass
        # 防复发：后台清理残留的 _MEI 解压目录（累积会占满系统盘，
        # 导致之后启动时解压 Qt5WebEngineCore.dll 失败）。
        # 延迟 6 秒再执行：避免与窗口/动画初始化争抢 CPU 造成启动卡顿
        def _delayed_mei_cleanup():
            try:
                time.sleep(6)
            except Exception:
                pass
            try:
                cleanup_stale_mei_dirs()
            except Exception:
                pass
        try:
            threading.Thread(target=_delayed_mei_cleanup, daemon=True).start()
        except Exception:
            pass
        app = MyApplication(sys.argv)
        check_essential_resources()

        def sigint_handler(signum, frame):
            print("\n正在退出程序...")
            if hasattr(app, 'firefly') and app.firefly:
                app.firefly.out_win()
            else:
                QApplication.quit()
        signal.signal(signal.SIGINT, sigint_handler)

        transparent_pixmap = QPixmap(1, 1)
        transparent_pixmap.fill(Qt.transparent)
        app.setWindowIcon(QIcon(transparent_pixmap))
        app.setQuitOnLastWindowClosed(False)

        font_path = os.path.join(get_base_dir(), "assets", "font", "HarmonyOS_Sans_SC_Bold.ttf")
        if os.path.exists(font_path):
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id != -1:
                font_families = QFontDatabase.applicationFontFamilies(font_id)
                if font_families:
                    harmony_font = QFont(font_families[0])
                    harmony_font.setPointSize(10)
                    app.setFont(harmony_font)

        try:
            pygame.mixer.init()
        except Exception as e:
            print(f"音频初始化失败(将以静音模式运行): {e}")

        current_user = auto_login_check()

        if current_user:
            # ---------- 新增 debugger 检测 ----------
            if current_user.get('userid') == 'debugger' and not os.environ.get('FIREFLY_DEBUGGER'):
                os.environ['QTWEBENGINE_REMOTE_DEBUGGING'] = '9222'
                os.environ['FIREFLY_DEBUGGER'] = '1'
                # 重启程序
                if getattr(sys, 'frozen', False):
                    program = sys.executable
                    args = []
                else:
                    program = sys.executable
                    args = [os.path.abspath(sys.argv[0])]
                if sys.platform == 'win32':
                    subprocess.Popen([program] + args, env=_spawn_env(),
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
                else:
                    subprocess.Popen([program] + args, env=_spawn_env(), start_new_session=True)
                _hard_exit(0)

            # ★★★ 自动登录初始化，捕获异常后调用全局钩子 ★★★
            try:
                if hasattr(app, 'firefly') and app.firefly:
                    app.firefly.deleteLater()
                    app.firefly = None
                    QApplication.processEvents()  # 确保销毁完成

                app.firefly = Firefly(user_data=current_user)
                app.firefly.init_tray_icon()
                app.firefly.create_menu()
                app.firefly.show()
                app.firefly.restore_last_position()
            except Exception:
                # 直接调用您的全局崩溃钩子，它会显示自定义窗口并退出
                global_exception_hook(*sys.exc_info())

        else:
            login_win = LoginWindow()

            def on_login_success(user_data):
                # 与自动登录分支一致：任何异常都走统一崩溃钩子（写日志 + 明确提示），
                # 避免"登录后既没有窗口也没有进程、还查不到原因"
                try:
                    login_win.accept()
                    if hasattr(app, 'firefly') and app.firefly:
                        app.firefly.deleteLater()
                        app.firefly = None
                    app.firefly = Firefly(user_data=user_data)
                    app.firefly.init_tray_icon()
                    app.firefly.create_menu()
                    app.firefly.show()
                    app.firefly.restore_last_position()
                    # 保底：桌宠是 Qt.Tool 窗口，若不关掉该开关，登录窗口关闭可能让
                    # 主事件循环立刻退出（表现为"登录后没有窗口"）
                    app.setQuitOnLastWindowClosed(False)
                    logging.info("[登录] 桌宠窗口已创建并显示")
                except Exception:
                    logging.error(f"[登录] 创建桌宠失败: {sys.exc_info()[1]}")
                    global_exception_hook(*sys.exc_info())

            login_win.login_success.connect(on_login_success)

            # 模态显示，等待用户操作
            if login_win.exec_() != QDialog.Accepted:
                # 用户取消或直接关闭窗口
                app.quit()
                sys.exit(0)

        logging.info("[启动] 进入主事件循环")
        sys.exit(app.exec_())

    except Exception:
        # 最外层捕获所有未被内部捕获的异常，调用统一崩溃窗口
        global_exception_hook(*sys.exc_info())
