import sys
import json
import requests
import os
import threading
import time
from io import StringIO
from PyQt5.QtGui import QIcon, QTextCursor, QPixmap, QFont, QFontDatabase
from PyQt5.QtWidgets import (QApplication, QWidget, QTextBrowser,
                             QTextEdit, QPushButton, QVBoxLayout,
                             QHBoxLayout, QLabel, QMessageBox)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

# ===================== 全局配置 =====================
global_log_buffer = StringIO()
original_print = print
CHAT_HISTORY_FILE = "chat_history.json"
MAX_RETRIES = 3
RETRY_DELAY = 2
EMPTY_MEMORY_TIP = "我是流萤，一起来聊聊吧！"
# 颜色配置
COLOR_GREEN = "#4CAF50"
COLOR_GREEN_HOVER = "#45a049"
COLOR_RED = "#f44336"
COLOR_RED_HOVER = "#d32f2f"
COLOR_GRAY = "#666666"
COLOR_GRAY_HOVER = "#555555"
COLOR_AI_BG = "#00CED1"
COLOR_TEXT = "#FFFFFF"
LABEL_FONT_SIZE = "12px"

# 自定义字体配置
FONT_FILE_PATH = "../assets/font/HarmonyOS_Sans_SC_Bold.ttf"  # 鸿蒙中文字体路径
FONT_FAMILY = "HarmonyOS Sans SC Medium"  # 字体家族名称（加载后使用）

# 消息模板
# ========== 核心修改：调整模板结构，标签单独成行 + 统一字体 ==========
USER_MSG_TEMPLATE = """
<div style="margin: 8px 0; font-family: {font_family};>
    <div style="text-align: right; color: {text_color}; font-size: {label_size} ; margin-bottom: 2px; font-family: {font_family};">我:</div>
    <div style="text-align: right;">
        <span style="background-color: {bg_color}; color: {text_color}; padding: 8px 12px; border-radius: 10px; max-width: 70%; display: inline-block; text-align: left; font-family: {font_family}; font-size: 14px;">{content}</span>
    </div>
</div>
"""

# AI消息模板（保持原有逻辑 + 统一字体）
AI_MSG_TEMPLATE = """
<div style="text-align: left; margin: 8px 0; font-family: {font_family};">
    <span style="color: {text_color}; font-size: {label_size}; margin-bottom: 2px; display: block; font-family: {font_family};">流萤: </span>
    <span style="background-color: {bg_color}; color: {text_color}; padding: 8px 12px; border-radius: 10px; max-width: 70%; display: inline-block; font-family: {font_family}; font-size: 14px;">{content}</span>
</div>
"""

# 空提示模板（核心修改：图片路径、尺寸、居中 + 统一字体）
EMPTY_TIP_TEMPLATE = """
<div style="text-align: center; margin-top: 50px; font-family: {font_family};">
    <span style="color: #888888; font-size: 16px; font-family: {font_family};">{content}</span>
    <!-- 仅空聊天区显示的图片 -->
    <div style="margin-top: 20px; text-align: center;"> <!-- 确保图片容器居中 -->
        <!-- 替换为指定图片路径 -->
        <!-- 原尺寸：width: 24px; height: 24px; 改为更小的16px（可自行调整） -->
        <img src="../assets/images/icon/happy.png" style="width: 16px; height: 16px;"/>
    </div>
</div>
"""


# ===================== 自定义控件 =====================
class ChatHistoryBrowser(QTextBrowser):
    """自定义聊天记录浏览器，仅允许复制AI回答内容"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)

    # 重写右键菜单：仅允许复制AI内容（禁用默认右键菜单）
    def contextMenuEvent(self, event):
        event.ignore()  # 禁用右键菜单，彻底禁止右键复制

    # 重写快捷键事件：拦截复制快捷键，仅允许复制AI内容
    def keyPressEvent(self, event):
        # 拦截Ctrl+C、Ctrl+Insert等复制快捷键
        if (event.key() == Qt.Key_C and event.modifiers() == Qt.ControlModifier) or \
                (event.key() == Qt.Key_Insert and event.modifiers() == Qt.ShiftModifier):
            # 获取选中的文本
            selected_text = self.textCursor().selectedText()
            if selected_text:
                # 检查选中的文本是否是AI回答（包含"流萤: "标识）
                html = self.toHtml()
                cursor = self.textCursor()
                start = cursor.selectionStart()
                end = cursor.selectionEnd()
                # 简单判断：AI内容包含"流萤: "标签
                if "流萤: " in html[start:end]:
                    super().keyPressEvent(event)  # 允许复制AI内容
                else:
                    event.ignore()  # 禁止复制非AI内容
            else:
                event.ignore()
            return
        # 其他快捷键正常处理
        super().keyPressEvent(event)


class InputTextEdit(QTextEdit):
    """自定义输入框，禁止拖拽粘贴和快捷键粘贴"""

    def __init__(self, parent=None):
        super().__init__(parent)

    # 禁止拖拽进入
    def dragEnterEvent(self, event):
        event.ignore()

    # 禁止拖拽放下（粘贴）
    def dropEvent(self, event):
        event.ignore()

    # 拦截粘贴快捷键（Ctrl+V/Shift+Insert）
    def keyPressEvent(self, event):
        # 拦截粘贴快捷键
        if (event.key() == Qt.Key_V and event.modifiers() == Qt.ControlModifier) or \
                (event.key() == Qt.Key_Insert and event.modifiers() == Qt.ShiftModifier):
            event.ignore()
            return
        # 处理Shift+Enter换行（保持原有逻辑）
        if event.key() == Qt.Key_Return and event.modifiers() == Qt.ShiftModifier:
            super().keyPressEvent(event)
            return
        # 其他按键正常处理
        super().keyPressEvent(event)


def custom_print(*args, **kwargs):
    original_print(*args, **kwargs)
    global_log_buffer.write(' '.join(map(str, args)) + '\n')
    global_log_buffer.flush()


print = custom_print


# ===================== 加载自定义字体 =====================
def load_custom_font():
    """加载鸿蒙中文字体文件"""
    if not os.path.exists(FONT_FILE_PATH):
        print(f"警告：字体文件不存在 - {FONT_FILE_PATH}")
        return False
    # 加载字体文件
    font_id = QFontDatabase.addApplicationFont(FONT_FILE_PATH)
    if font_id == -1:
        print(f"错误：加载字体失败 - {FONT_FILE_PATH}")
        return False
    # 获取字体家族名称
    font_families = QFontDatabase.applicationFontFamilies(font_id)
    if font_families:
        global FONT_FAMILY
        FONT_FAMILY = font_families[0]  # 更新为实际加载的字体名称
        print(f"✅ 成功加载字体：{FONT_FAMILY}")
        return True
    else:
        print(f"错误：获取字体家族名称失败 - {FONT_FILE_PATH}")
        return False


# ===================== 网络检测 =====================
def check_network_connection():
    try:
        requests.get("https://www.baidu.com", timeout=5)
        requests.get("https://api.siliconflow.cn", timeout=5)
        return True
    except Exception as e:
        print(f"网络检测失败: {e}")
        return False


# ===================== AI线程 =====================
class SiliconFlowWorker(QThread):
    response_chunk = pyqtSignal(str)
    response_complete = pyqtSignal(bool, str)
    error_occurred = pyqtSignal(str)

    def __init__(self, api_key, conversation_history):
        super().__init__()
        self.api_key = api_key
        self.conversation_history = conversation_history
        self.running = True
        self.full_response = ""

    def run(self):
        if not check_network_connection():
            self.error_occurred.emit(
                "网络连接失败！\n1. 检查是否联网\n2. 关闭代理/VPN\n3. 清除DNS缓存"
            )
            return

        for retry in range(MAX_RETRIES):
            if not self.running:
                break
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }

                messages = [{"role": "system",
                             "content": "你叫流萤，是个害羞的女孩子，我最好的朋友，最喜欢吃的食物是橡木蛋糕卷，来自崩坏：星穹铁道游戏，你常常说：生命因何而沉睡？因为我们终将从梦中醒来! 你会记住所有之前的对话内容，基于完整的上下文回复。"}]
                for role, content in self.conversation_history:
                    messages.append({"role": "user" if role == "user" else "assistant", "content": content.strip()})

                payload = {
                    "model": "deepseek-ai/DeepSeek-R1",
                    "messages": messages,
                    "stream": True,
                    "temperature": 0.7,
                    "max_tokens": 2048
                }

                with requests.post(
                        "https://api.siliconflow.cn/v1/chat/completions",
                        headers=headers,
                        json=payload,
                        stream=True,
                        timeout=60
                ) as response:
                    if response.status_code != 200:
                        raise Exception(f"API错误: {response.status_code} - {response.text}")

                    for chunk in response.iter_lines():
                        if not self.running:
                            break
                        if chunk:
                            decoded_chunk = chunk.decode('utf-8')
                            try:
                                if decoded_chunk.startswith("data:") and decoded_chunk != "data: [DONE]":
                                    json_data = json.loads(decoded_chunk[5:].strip())
                                    content = json_data["choices"][0]["delta"].get("content", "")
                                    if content:
                                        self.response_chunk.emit(content)
                                        self.full_response += content
                            except:
                                continue

                self.response_complete.emit(True, self.full_response)
                return
            except Exception as e:
                if retry < MAX_RETRIES - 1:
                    self.error_occurred.emit(
                        f"请求失败（第{retry + 1}/{MAX_RETRIES}次）: {str(e)}，{RETRY_DELAY}秒后重试...")
                    time.sleep(RETRY_DELAY)
                else:
                    self.error_occurred.emit(
                        f"请求最终失败！\n错误原因: {str(e)}\n\n解决方案：\n1. 检查网络\n2. 清除DNS缓存\n3. 关闭代理/VPN"
                    )

    def stop(self):
        self.running = False


# ===================== 主窗口 =====================
class ChatWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.conversation_history = self.load_chat_history()
        self.api_key = "sk-xichaynbteuddjxbcsdclomzyupcpexhoqveoygbvfwuovzv"
        self.current_reply = ""
        self.ai_worker = None
        # 占位符配置
        self.placeholder_text = "点击输入问题交流"
        self.is_placeholder = True

        self.init_ui()
        # 初始化占位符
        self.input_box.setText(self.placeholder_text)
        self.input_box.setStyleSheet(f"""
            QTextEdit {{
                background-color: #1E1E1E; 
                border: 2px solid #3A3A3A; 
                border-radius: 10px; 
                padding: 10px; 
                font-size: 14px;
                color: #888888;
                font-family: {FONT_FAMILY};  /* 输入框字体 */
            }}
        """)
        self.check_auth()
        self.check_network_on_start()
        self.update_chat_history()

    def clear_memory(self):
        if self.ai_worker and self.ai_worker.isRunning():
            msg_box = QMessageBox()
            msg_box.setWindowTitle("操作提示")
            msg_box.setText("请等待回复完成后再清除记忆～")
            msg_box.setIcon(QMessageBox.Information)
            ok_btn = msg_box.addButton("OK", QMessageBox.AcceptRole)
            ok_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COLOR_GREEN};
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 8px 16px;
                    font-size: 14px;
                    font-family: {FONT_FAMILY};  /* 按钮字体 */
                }}
                QPushButton:hover {{background-color: {COLOR_GREEN_HOVER};}}
            """)
            msg_box.exec_()
            return

        confirm_dialog = QMessageBox(self)
        confirm_dialog.setWindowTitle("补要啊~")
        confirm_dialog.setText("\n你真的要清除掉流萤的记忆吗？\n或许...你不应该这么做！\n注意:此操作不可逆!")

        # ========== 核心修改：替换默认问号为自定义PNG ==========
        confirm_dialog.setIcon(QMessageBox.NoIcon)  # 关闭默认图标
        # 加载自定义图片（替换为你的图片路径）
        custom_icon_path = "../assets/images/firefly/Sadness/no.png"  # 请修改为实际路径
        pixmap = QPixmap(custom_icon_path)
        if not pixmap.isNull():  # 检查图片是否加载成功
            # 调整图片大小（可选，保持比例并缩放到80x80）
            pixmap = pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            confirm_dialog.setIconPixmap(pixmap)  # 设置自定义图片
        else:
            # 图片加载失败时降级显示默认问号
            confirm_dialog.setIcon(QMessageBox.Question)
            print(f"警告：自定义图标加载失败，路径：{custom_icon_path}")
        # ======================================================

        yes_btn = confirm_dialog.addButton("是", QMessageBox.YesRole)
        no_btn = confirm_dialog.addButton("否", QMessageBox.NoRole)
        confirm_dialog.setDefaultButton(no_btn)

        yes_btn.setStyleSheet(f"""
            QPushButton {{background-color: {COLOR_RED}; color: white; border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px; font-family: {FONT_FAMILY};}}
            QPushButton:hover {{background-color: {COLOR_RED_HOVER};}}
        """)
        no_btn.setStyleSheet(f"""
            QPushButton {{background-color: {COLOR_GREEN}; color: white; border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px; font-family: {FONT_FAMILY};}}
            QPushButton:hover {{background-color: {COLOR_GREEN_HOVER};}}
        """)

        confirm_dialog.exec_()
        if confirm_dialog.clickedButton() == yes_btn:
            self.conversation_history = []
            if os.path.exists(CHAT_HISTORY_FILE):
                try:
                    os.remove(CHAT_HISTORY_FILE)
                    print("✅ 已删除本地记忆文件")
                except Exception as e:
                    print(f"⚠️ 删除失败: {e}")
            self.update_chat_history()
            success_dialog = QMessageBox(self)
            success_dialog.setWindowTitle("我怎么...都不记得了?")
            success_dialog.setText("流萤的记忆已全部清除，现在可以开始新的对话啦～\n或许这是另一种新生...")
            ok_btn = success_dialog.addButton("OK", QMessageBox.AcceptRole)
            ok_btn.setStyleSheet(f"""
                QPushButton {{background-color: {COLOR_GREEN}; color: white; border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px; font-family: {FONT_FAMILY};}}
                QPushButton:hover {{background-color: {COLOR_GREEN_HOVER};}}
            """)
            success_dialog.exec_()
            self.status_label.setText("就绪（记忆已清除）")

    def check_network_on_start(self):
        if not check_network_connection():
            QMessageBox.warning(self, "网络提示", "检测到网络异常，可能无法连接AI服务！\n请检查网络后重试。")

    def load_chat_history(self):
        try:
            if os.path.exists(CHAT_HISTORY_FILE):
                with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if isinstance(history, list) and all(
                        isinstance(item, list) and len(item) == 2 and item[0] in ["user", "ai"] and isinstance(item[1],
                                                                                                               str) for
                        item in history):
                    print(f"✅ 加载{len(history)}条历史")
                    return history
            return []
        except Exception as e:
            print(f"⚠️ 加载历史失败: {e}")
            return []

    def save_chat_history(self):
        try:
            with open(CHAT_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.conversation_history, f, ensure_ascii=False, indent=2)
            print(f"✅ 保存{len(self.conversation_history)}条历史")
        except Exception as e:
            print(f"⚠️ 保存失败: {e}")

    def update_chat_history(self):
        self.chat_history.clear()
        if not self.conversation_history:
            # 填充空提示模板（传入字体）
            empty_html = EMPTY_TIP_TEMPLATE.format(content=EMPTY_MEMORY_TIP, font_family=FONT_FAMILY)
            self.chat_history.insertHtml(empty_html)
        else:
            html_content = ""
            for role, content in self.conversation_history:
                if role == "user":
                    user_html = USER_MSG_TEMPLATE.format(
                        bg_color=COLOR_GREEN, text_color=COLOR_TEXT, label_size=LABEL_FONT_SIZE,
                        content=content.replace("\n", "<br>"), font_family=FONT_FAMILY
                    )
                    html_content += user_html
                else:
                    ai_html = AI_MSG_TEMPLATE.format(
                        bg_color=COLOR_AI_BG, text_color=COLOR_TEXT, label_size=LABEL_FONT_SIZE,
                        content=content.replace("\n", "<br>"), font_family=FONT_FAMILY
                    )
                    html_content += ai_html
            self.chat_history.insertHtml(html_content)
        self.chat_history.moveCursor(QTextCursor.End)
        self.chat_history.setReadOnly(True)

    def check_auth(self):
        if not self.api_key or self.api_key == "your-api-key-here":
            QMessageBox.critical(self, "配置错误", "请先配置有效的硅基流动API密钥！")
            sys.exit(1)

    def init_ui(self):
        self.setWindowIcon(QIcon("../assets/images/firefly/icon/icon.ico"))
        self.setWindowTitle('流萤-AI在线对话')
        # 核心修改1：锁死窗口大小为1280×820
        self.setFixedSize(1280, 820)  # 禁止调整窗口大小
        # 可选：将窗口居中显示
        screen_geo = QApplication.desktop().availableGeometry()
        self.move((screen_geo.width() - 1280) // 2, (screen_geo.height() - 820) // 2)

        # 核心控件：使用自定义控件
        self.chat_history = ChatHistoryBrowser()  # 替换为自定义聊天记录浏览器
        self.input_box = InputTextEdit()  # 替换为自定义输入框
        self.input_box.setMaximumHeight(100)
        self.send_btn = QPushButton("发送")
        self.clear_btn = QPushButton("清除记忆")
        self.status_label = QLabel("就绪（已加载历史对话）")
        # 版本号标签
        self.version_label = QLabel("Firefly_AI_Chat_V4.0.1r(Online)")
        self.version_label.setStyleSheet(f"color: #888888; font-size: 12px; font-family: {FONT_FAMILY};")
        self.version_label.setAlignment(Qt.AlignRight)

        # 布局
        btn_vbox = QVBoxLayout()
        btn_vbox.addWidget(self.send_btn)
        btn_vbox.addWidget(self.clear_btn)
        btn_vbox.setSpacing(5)

        input_hbox = QHBoxLayout()
        input_hbox.addWidget(self.input_box)
        input_hbox.addLayout(btn_vbox)

        bottom_hbox = QHBoxLayout()
        bottom_hbox.addWidget(self.status_label)
        bottom_hbox.addStretch()
        bottom_hbox.addWidget(self.version_label)

        main_vbox = QVBoxLayout()
        main_vbox.addWidget(self.chat_history)
        main_vbox.addLayout(input_hbox)
        main_vbox.addLayout(bottom_hbox)
        self.setLayout(main_vbox)

        # 信号绑定
        self.send_btn.clicked.connect(self.send_message)
        self.clear_btn.clicked.connect(self.clear_memory)
        self.input_box.installEventFilter(self)

        # 样式表（核心修改：替换字体为鸿蒙字体）
        self.setStyleSheet(f"""
            QWidget {{background-color: #2D2D2D; color: {COLOR_TEXT}; font-family: {FONT_FAMILY};}}
            QTextBrowser {{
                background-color: #1E1E1E; 
                border: 2px solid #3A3A3A; 
                border-radius: 10px; 
                padding: 10px; 
                font-size: 14px;
                line-height: 1.5;
                font-family: {FONT_FAMILY};
            }}
            QPushButton {{
                color: white; 
                border: none; 
                border-radius: 8px; 
                padding: 12px 24px; 
                font-size: 14px; 
                min-width: 80px;
                font-family: {FONT_FAMILY};
            }}
            QPushButton#send_btn {{background-color: {COLOR_GREEN};}}
            QPushButton#send_btn:hover {{background-color: {COLOR_GREEN_HOVER};}}
            QPushButton#send_btn:disabled {{background-color: {COLOR_GRAY}; color: #CCCCCC; cursor: not-allowed;}}
            QPushButton#clear_btn {{background-color: {COLOR_RED};}}
            QPushButton#clear_btn:hover {{background-color: {COLOR_RED_HOVER};}}
            QLabel {{font-size: 14px; padding: 8px; font-family: {FONT_FAMILY};}}
        """)
        self.send_btn.setObjectName("send_btn")
        self.clear_btn.setObjectName("clear_btn")

    def eventFilter(self, obj, event):
        if obj is self.input_box:
            # 焦点进入
            if event.type() == event.FocusIn:
                if self.is_placeholder:
                    self.input_box.clear()
                    self.input_box.setStyleSheet(f"""
                        QTextEdit {{
                            background-color: #1E1E1E; 
                            border: 2px solid #3A3A3A; 
                            border-radius: 10px; 
                            padding: 10px; 
                            font-size: 14px;
                            color: #FFFFFF;
                            font-family: {FONT_FAMILY};
                        }}
                    """)
                    self.is_placeholder = False
            # 焦点离开
            elif event.type() == event.FocusOut:
                if not self.input_box.toPlainText().strip():
                    self.input_box.setText(self.placeholder_text)
                    self.input_box.setStyleSheet(f"""
                        QTextEdit {{
                            background-color: #1E1E1E; 
                            border: 2px solid #3A3A3A; 
                            border-radius: 10px; 
                            padding: 10px; 
                            font-size: 14px;
                            color: #888888;
                            font-family: {FONT_FAMILY};
                        }}
                    """)
                    self.is_placeholder = True
            # 回车发送
            elif event.type() == event.KeyPress:
                if event.key() == Qt.Key_Return and event.modifiers() == Qt.ShiftModifier:
                    return False
                elif event.key() == Qt.Key_Return:
                    if self.send_btn.isEnabled() and not self.is_placeholder:
                        self.send_message()
                        return True
        return super().eventFilter(obj, event)

    def send_message(self):
        message = self.input_box.toPlainText().strip()
        if not message or self.is_placeholder or message == self.placeholder_text:
            return
        if self.ai_worker and self.ai_worker.isRunning():
            return

        self.conversation_history.append(("user", message))
        self.save_chat_history()
        self.update_chat_history()

        # 清空并恢复占位符
        self.input_box.clear()
        self.input_box.setText(self.placeholder_text)
        self.input_box.setStyleSheet(f"""
            QTextEdit {{
                background-color: #1E1E1E; 
                border: 2px solid #3A3A3A; 
                border-radius: 10px; 
                padding: 10px; 
                font-size: 14px;
                color: #888888;
                font-family: {FONT_FAMILY};
            }}
        """)
        self.is_placeholder = True

        self.status_label.setText("流萤正在思考...")
        self.send_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)

        self.ai_worker = SiliconFlowWorker(self.api_key, self.conversation_history)
        self.ai_worker.response_chunk.connect(self.on_response_chunk)
        self.ai_worker.response_complete.connect(self.on_response_complete)
        self.ai_worker.error_occurred.connect(self.on_error_occurred)
        self.ai_worker.start()

    def on_response_chunk(self, chunk):
        self.current_reply += chunk
        temp_history = self.conversation_history.copy()
        temp_history.append(("ai", self.current_reply))

        self.chat_history.clear()
        if not temp_history:
            empty_html = EMPTY_TIP_TEMPLATE.format(content=EMPTY_MEMORY_TIP, font_family=FONT_FAMILY)
            self.chat_history.insertHtml(empty_html)
        else:
            html_content = ""
            for role, content in temp_history:
                if role == "user":
                    user_html = USER_MSG_TEMPLATE.format(
                        bg_color=COLOR_GREEN, text_color=COLOR_TEXT, label_size=LABEL_FONT_SIZE,
                        content=content.replace("\n", "<br>"), font_family=FONT_FAMILY
                    )
                    html_content += user_html
                else:
                    ai_html = AI_MSG_TEMPLATE.format(
                        bg_color=COLOR_AI_BG, text_color=COLOR_TEXT, label_size=LABEL_FONT_SIZE,
                        content=content.replace("\n", "<br>"), font_family=FONT_FAMILY
                    )
                    html_content += ai_html
            self.chat_history.insertHtml(html_content)
        self.chat_history.moveCursor(QTextCursor.End)

    def on_response_complete(self, success, full_response):
        if success and full_response:
            self.conversation_history.append(("ai", full_response))
            self.save_chat_history()
            self.update_chat_history()

        self.current_reply = ""
        self.status_label.setText("就绪（已记住本次对话）")
        self.send_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)

    def on_error_occurred(self, error_msg):
        error_html = AI_MSG_TEMPLATE.format(
            bg_color="#ff5555", text_color=COLOR_TEXT, label_size=LABEL_FONT_SIZE,
            content=f"❌ {error_msg}".replace("\n", "<br>"), font_family=FONT_FAMILY
        )
        self.chat_history.insertHtml(error_html)
        self.chat_history.moveCursor(QTextCursor.End)

        self.current_reply = ""
        self.status_label.setText("就绪（请求失败）")
        self.send_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)

    def closeEvent(self, event):
        self.save_chat_history()
        if self.ai_worker and self.ai_worker.isRunning():
            self.ai_worker.stop()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    # 第一步：加载自定义鸿蒙字体
    load_custom_font()

    # 第二步：设置应用级全局字体（兜底）
    app_font = QFont(FONT_FAMILY, 14)
    app.setFont(app_font)

    window = ChatWindow()
    window.show()
    sys.exit(app.exec_())