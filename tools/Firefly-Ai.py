import sys
import json
import requests
import os
import threading
import time
from io import StringIO
from PyQt5.QtGui import QIcon, QFont, QFontDatabase, QPixmap
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSharedMemory, QTimer, QBuffer, QByteArray
from PyQt5.QtWidgets import (QApplication, QWidget, QScrollArea,
                             QTextEdit, QPushButton, QVBoxLayout,
                             QHBoxLayout, QLabel, QMessageBox, QSizePolicy)

# ===================== 资源路径处理 =====================
def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
        abs_path = os.path.normpath(os.path.join(base_dir, relative_path))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        abs_path = os.path.normpath(os.path.join(base_dir, relative_path))
    return abs_path

# ===================== 全局配置 =====================
global_log_buffer = StringIO()
original_print = print
CHAT_HISTORY_FILE = "chat_history.json"
MAX_RETRIES = 3
RETRY_DELAY = 2
EMPTY_MEMORY_TIP = "我是流萤，一起来聊聊吧！"
COLOR_GREEN = "#4CAF50"
COLOR_GREEN_HOVER = "#45a049"
COLOR_RED = "#f44336"
COLOR_RED_HOVER = "#d32f2f"
COLOR_GRAY = "#666666"
COLOR_GRAY_HOVER = "#555555"
COLOR_AI_BG = "#00CED1"
COLOR_TEXT = "#FFFFFF"
LABEL_FONT_SIZE = "12px"

FONT_FILE_PATH = resource_path("../assets/font/HarmonyOS_Sans_SC_Bold.ttf")
FONT_FAMILY = "HarmonyOS Sans SC Medium"

def pixmap_to_data_uri(pixmap, image_format="PNG"):
    if pixmap.isNull():
        return ""
    ba = QByteArray()
    buffer = QBuffer(ba)
    buffer.open(QBuffer.WriteOnly)
    pixmap.save(buffer, image_format)
    base64_data = ba.toBase64().data().decode()
    return f"data:image/{image_format.lower()};base64,{base64_data}"

HAPPY_IMG_DATA_URI = ""

EMPTY_TIP_TEMPLATE = """
<div style="text-align: center; margin-top: 50px; font-family: {font_family};">
    <div style="color: #888888; font-size: 16px; font-family: {font_family}; margin-bottom: 20px;">
        {content}
    </div>
    <div>
        <img src="{img_src}" style="width: 16px; height: 16px;"/>
    </div>
</div>
"""

def custom_print(*args, **kwargs):
    original_print(*args, **kwargs)
    global_log_buffer.write(' '.join(map(str, args)) + '\n')
    global_log_buffer.flush()

print = custom_print

# ===================== 加载自定义字体 =====================
def load_custom_font():
    global FONT_FAMILY
    if not os.path.exists(FONT_FILE_PATH):
        print(f"警告：字体文件不存在 - {FONT_FILE_PATH}")
        FONT_FAMILY = "Microsoft YaHei"
        return False
    font_id = QFontDatabase.addApplicationFont(FONT_FILE_PATH)
    if font_id == -1:
        print(f"错误：加载字体失败 - {FONT_FILE_PATH}")
        FONT_FAMILY = "Microsoft YaHei"
        return False
    font_families = QFontDatabase.applicationFontFamilies(font_id)
    if font_families:
        FONT_FAMILY = font_families[0]
        print(f"✅ 成功加载字体：{FONT_FAMILY}")
        return True
    else:
        print(f"错误：获取字体家族名称失败 - {FONT_FILE_PATH}")
        FONT_FAMILY = "Microsoft YaHei"
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
        self.placeholder_text = "点击输入问题交流"
        self.is_placeholder = True

        self.init_ui()
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
        self.check_auth()
        self.check_network_on_start()
        self.update_chat_history()

    # ---------- 滚动控制 ----------
    def scroll_to_top(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(0)

    def scroll_to_bottom(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())

    # ---------- 消息气泡 ----------
    def add_message(self, role, content, is_error=False):
        container = QWidget()
        container.setStyleSheet("background: transparent; margin: 0; padding: 0;")
        container.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)

        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        name_label = QLabel("我:" if role == 'user' else "流萤:" if not is_error else "错误")
        name_label.setStyleSheet(f"""
            QLabel {{
                color: {COLOR_TEXT};
                font-size: {LABEL_FONT_SIZE};
                font-family: {FONT_FAMILY};
                margin: 0px;
                padding: 0px;
            }}
        """)
        name_label.setAlignment(Qt.AlignRight if role == 'user' else Qt.AlignLeft)

        bubble = QLabel()
        bubble.setWordWrap(True)
        bubble.setMaximumWidth(int(self.scroll_area.width() * 0.7))
        bubble.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        bubble.setStyleSheet(f"""
            QLabel {{
                background-color: {COLOR_GREEN if role == 'user' else COLOR_AI_BG if not is_error else '#ff5555'};
                color: {COLOR_TEXT};
                padding: 10px 14px;
                border-radius: 14px;
                font-size: 14px;
                font-family: {FONT_FAMILY};
                margin: 0px;
                border: none;
            }}
        """)
        bubble.setText(content)

        if role == 'user':
            layout.addStretch()
            layout.addWidget(bubble, alignment=Qt.AlignRight)
            layout.addWidget(name_label, alignment=Qt.AlignRight)
        else:
            layout.addWidget(name_label, alignment=Qt.AlignLeft)
            layout.addWidget(bubble, alignment=Qt.AlignLeft)
            layout.addStretch()

        container.adjustSize()
        container.setFixedHeight(container.sizeHint().height())

        self.msg_layout.addWidget(container)

    # ---------- 更新聊天历史 ----------
    def update_chat_history(self):
        # 清空
        while self.msg_layout.count():
            item = self.msg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.conversation_history:
            empty_html = EMPTY_TIP_TEMPLATE.format(
                content=EMPTY_MEMORY_TIP,
                font_family=FONT_FAMILY,
                img_src=HAPPY_IMG_DATA_URI
            )
            empty_label = QLabel()
            empty_label.setText(empty_html)
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("background: transparent; margin: 0; padding: 0;")
            empty_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.msg_layout.addWidget(empty_label)
        else:
            for role, content in self.conversation_history:
                self.add_message(role, content)

        self.msg_container.updateGeometry()
        self.msg_container.adjustSize()

        total_height = 0
        for i in range(self.msg_layout.count()):
            item = self.msg_layout.itemAt(i)
            if item.widget():
                total_height += item.widget().height()
                if i < self.msg_layout.count() - 1:
                    total_height += self.msg_layout.spacing()
        if total_height > 0:
            self.msg_container.setFixedHeight(total_height)
        else:
            self.msg_container.setFixedHeight(self.scroll_area.height())

        self.scroll_to_top()
        QTimer.singleShot(50, self.scroll_to_top)

    # ---------- 加载和保存历史 ----------
    def load_chat_history(self):
        try:
            if os.path.exists(CHAT_HISTORY_FILE):
                with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if isinstance(history, list) and all(
                        isinstance(item, list) and len(item) == 2 and item[0] in ["user", "ai"] and isinstance(item[1], str)
                        for item in history):
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

    # ---------- 清除记忆 ----------
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
                    font-family: {FONT_FAMILY};
                }}
                QPushButton:hover {{background-color: {COLOR_GREEN_HOVER};}}
            """)
            msg_box.exec_()
            return

        confirm_dialog = QMessageBox(self)
        confirm_dialog.setWindowTitle("补要啊~")
        confirm_dialog.setText("\n你真的要清除掉流萤的记忆吗？\n或许...你不应该这么做！\n注意:此操作不可逆!")

        confirm_dialog.setIcon(QMessageBox.NoIcon)
        custom_icon_path = resource_path("../assets/images/firefly/Sadness/no.png")
        pixmap = QPixmap(custom_icon_path)
        if not pixmap.isNull():
            pixmap = pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            confirm_dialog.setIconPixmap(pixmap)
        else:
            confirm_dialog.setIcon(QMessageBox.Question)
            print(f"警告：自定义图标加载失败，路径：{custom_icon_path}")

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
            self.status_label.setText("就绪（记忆已清除）")

    # ---------- 网络检查 ----------
    def check_network_on_start(self):
        if not check_network_connection():
            QMessageBox.warning(self, "网络提示", "检测到网络异常，可能无法连接AI服务！\n请检查网络后重试。")

    # ---------- API密钥检查 ----------
    def check_auth(self):
        if not self.api_key or self.api_key == "your-api-key-here":
            QMessageBox.critical(self, "配置错误", "请先配置有效的硅基流动API密钥！")
            sys.exit(1)

    # ---------- UI初始化 ----------
    def init_ui(self):
        icon_path = resource_path("../assets/images/firefly/icon/icon.ico")
        self.setWindowIcon(QIcon(icon_path))
        self.setWindowTitle('流萤-AI在线对话')
        self.setFixedSize(1280, 820)
        screen_geo = QApplication.desktop().availableGeometry()
        self.move((screen_geo.width() - 1280) // 2, (screen_geo.height() - 820) // 2)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignTop)
        # ★ 修改背景色为和输入框一样的深色 #1E1E1E
        self.scroll_area.setStyleSheet("""
            QScrollArea { background: #1E1E1E; border: 2px solid #3A3A3A; border-radius: 10px; }
        """)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.msg_container = QWidget()
        self.msg_container.setStyleSheet("background: transparent; margin: 0; padding: 0;")
        self.msg_container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.msg_layout = QVBoxLayout(self.msg_container)
        self.msg_layout.setContentsMargins(0, 0, 0, 0)
        self.msg_layout.setSpacing(6)

        self.scroll_area.setWidget(self.msg_container)
        main_layout.addWidget(self.scroll_area)

        input_hbox = QHBoxLayout()
        self.input_box = QTextEdit()
        self.input_box.setMaximumHeight(100)
        self.input_box.setStyleSheet("""
            QTextEdit {
                background-color: #1E1E1E;
                border: 2px solid #3A3A3A;
                border-radius: 10px;
                padding: 10px;
                font-size: 14px;
                color: #888888;
            }
        """)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("send_btn")
        self.send_btn.setStyleSheet(f"""
            QPushButton#send_btn {{
                background-color: {COLOR_GREEN};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px 24px;
                font-size: 14px;
                min-width: 80px;
                font-family: {FONT_FAMILY};
            }}
            QPushButton#send_btn:hover {{background-color: {COLOR_GREEN_HOVER};}}
            QPushButton#send_btn:disabled {{
                background-color: {COLOR_GRAY};
                color: #CCCCCC;
                cursor: not-allowed;
            }}
        """)
        self.clear_btn = QPushButton("清除记忆")
        self.clear_btn.setObjectName("clear_btn")
        self.clear_btn.setStyleSheet(f"""
            QPushButton#clear_btn {{
                background-color: {COLOR_RED};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px 24px;
                font-size: 14px;
                min-width: 80px;
                font-family: {FONT_FAMILY};
            }}
            QPushButton#clear_btn:hover {{background-color: {COLOR_RED_HOVER};}}
        """)

        btn_vbox = QVBoxLayout()
        btn_vbox.addWidget(self.send_btn)
        btn_vbox.addWidget(self.clear_btn)
        btn_vbox.setSpacing(5)

        input_hbox.addWidget(self.input_box)
        input_hbox.addLayout(btn_vbox)
        main_layout.addLayout(input_hbox)

        bottom_hbox = QHBoxLayout()
        self.status_label = QLabel("就绪（已加载历史对话）")
        self.status_label.setStyleSheet(f"color: #888888; font-size: 14px; font-family: {FONT_FAMILY};")
        self.version_label = QLabel("Firefly_AI_Chat_V4.0.1r(Online)")
        self.version_label.setStyleSheet(f"color: #888888; font-size: 12px; font-family: {FONT_FAMILY};")
        self.version_label.setAlignment(Qt.AlignRight)

        bottom_hbox.addWidget(self.status_label)
        bottom_hbox.addStretch()
        bottom_hbox.addWidget(self.version_label)
        main_layout.addLayout(bottom_hbox)

        self.send_btn.clicked.connect(self.send_message)
        self.clear_btn.clicked.connect(self.clear_memory)
        self.input_box.installEventFilter(self)

        self.setStyleSheet(f"""
            QWidget {{
                background-color: #2D2D2D;
                color: {COLOR_TEXT};
                font-family: {FONT_FAMILY};
            }}
        """)

    # ---------- 事件过滤 ----------
    def eventFilter(self, obj, event):
        if obj is self.input_box:
            if event.type() == event.FocusIn:
                if self.is_placeholder:
                    self.input_box.clear()
                    self.input_box.setStyleSheet("""
                        QTextEdit {
                            background-color: #1E1E1E;
                            border: 2px solid #3A3A3A;
                            border-radius: 10px;
                            padding: 10px;
                            font-size: 14px;
                            color: #FFFFFF;
                            font-family: """ + FONT_FAMILY + """;
                        }
                    """)
                    self.is_placeholder = False
            elif event.type() == event.FocusOut:
                if not self.input_box.toPlainText().strip():
                    self.input_box.setText(self.placeholder_text)
                    self.input_box.setStyleSheet("""
                        QTextEdit {
                            background-color: #1E1E1E;
                            border: 2px solid #3A3A3A;
                            border-radius: 10px;
                            padding: 10px;
                            font-size: 14px;
                            color: #888888;
                            font-family: """ + FONT_FAMILY + """;
                        }
                    """)
                    self.is_placeholder = True
            elif event.type() == event.KeyPress:
                if event.key() == Qt.Key_Return and event.modifiers() == Qt.ShiftModifier:
                    return False
                elif event.key() == Qt.Key_Return:
                    if self.send_btn.isEnabled() and not self.is_placeholder:
                        self.send_message()
                        return True
        return super().eventFilter(obj, event)

    # ---------- 发送消息 ----------
    def send_message(self):
        message = self.input_box.toPlainText().strip()
        if not message or self.is_placeholder or message == self.placeholder_text:
            return
        if self.ai_worker and self.ai_worker.isRunning():
            return

        self.conversation_history.append(("user", message))
        self.save_chat_history()
        self.update_chat_history()
        QTimer.singleShot(0, self.scroll_to_bottom)

        self.input_box.clear()
        self.input_box.setText(self.placeholder_text)
        self.input_box.setStyleSheet("""
            QTextEdit {
                background-color: #1E1E1E;
                border: 2px solid #3A3A3A;
                border-radius: 10px;
                padding: 10px;
                font-size: 14px;
                color: #888888;
                font-family: """ + FONT_FAMILY + """;
            }
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

    # ---------- 流式响应 ----------
    def on_response_chunk(self, chunk):
        self.current_reply += chunk
        temp_history = self.conversation_history.copy()
        temp_history.append(("ai", self.current_reply))
        while self.msg_layout.count():
            item = self.msg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for role, content in temp_history:
            self.add_message(role, content)
        total_height = 0
        for i in range(self.msg_layout.count()):
            item = self.msg_layout.itemAt(i)
            if item.widget():
                total_height += item.widget().height()
                if i < self.msg_layout.count() - 1:
                    total_height += self.msg_layout.spacing()
        if total_height > 0:
            self.msg_container.setFixedHeight(total_height)
        self.scroll_to_bottom()

    def on_response_complete(self, success, full_response):
        if success and full_response:
            self.conversation_history.append(("ai", full_response.strip()))
            self.save_chat_history()
            self.update_chat_history()
            QTimer.singleShot(0, self.scroll_to_bottom)
        self.current_reply = ""
        self.status_label.setText("就绪（已记住本次对话）")
        self.send_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)

    # ---------- 错误处理（弹窗不退出） ----------
    def on_error_occurred(self, error_msg):
        self.add_message("ai", f"❌ {error_msg}", is_error=True)
        total_height = 0
        for i in range(self.msg_layout.count()):
            item = self.msg_layout.itemAt(i)
            if item.widget():
                total_height += item.widget().height()
                if i < self.msg_layout.count() - 1:
                    total_height += self.msg_layout.spacing()
        if total_height > 0:
            self.msg_container.setFixedHeight(total_height)
        self.scroll_to_bottom()

        if "403" in error_msg or "Access denied" in error_msg or "identity verification" in error_msg:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("连接错误")
            msg_box.setText("流萤AI暂不可用！\n请检查与服务器的连通性！")
            msg_box.setIcon(QMessageBox.Critical)
            ok_btn = msg_box.addButton("确定", QMessageBox.AcceptRole)
            ok_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {COLOR_GREEN};
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 8px 16px;
                    font-size: 14px;
                    font-family: {FONT_FAMILY};
                }}
                QPushButton:hover {{
                    background-color: {COLOR_GREEN_HOVER};
                }}
            """)
            msg_box.exec_()

        self.current_reply = ""
        self.status_label.setText("就绪（请求失败）")
        self.send_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)

    # ---------- 关闭事件 ----------
    def closeEvent(self, event):
        self.save_chat_history()
        if self.ai_worker and self.ai_worker.isRunning():
            self.ai_worker.stop()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)

    shared_mem = QSharedMemory("FireflyAI_SingleInstance")
    if shared_mem.attach():
        msg = QMessageBox()
        msg.setWindowTitle("提示")
        msg.setText("程序已经在运行中，请勿重复打开！")
        msg.setIcon(QMessageBox.Information)
        msg.setStyleSheet("""
            QMessageBox { background-color: #2D2D2D; color: #FFFFFF; }
            QLabel { color: #FFFFFF; }
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 8px; padding: 8px 16px; }
            QPushButton:hover { background-color: #45a049; }
        """)
        msg.exec_()
        sys.exit(0)
    else:
        shared_mem.create(1)

    app.setStyleSheet("""
        QWidget {
            background-color: #2D2D2D;
            color: #FFFFFF;
            font-family: "Microsoft YaHei", "HarmonyOS Sans SC Medium", sans-serif;
        }
        QMessageBox {
            background-color: #2D2D2D;
            color: #FFFFFF;
        }
        QMessageBox QLabel {
            color: #FFFFFF;
        }
        QMessageBox QPushButton {
            background-color: #4CAF50;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 8px 16px;
            min-width: 80px;
        }
        QMessageBox QPushButton:hover {
            background-color: #45a049;
        }
        QDialog {
            background-color: #2D2D2D;
            color: #FFFFFF;
        }
        QScrollBar:vertical {
            background: #1E1E1E;
            width: 12px;
            border-radius: 6px;
        }
        QScrollBar::handle:vertical {
            background: #555555;
            border-radius: 6px;
            min-height: 20px;
        }
        QScrollBar::handle:vertical:hover {
            background: #777777;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
    """)

    load_custom_font()
    app_font = QFont(FONT_FAMILY, 14)
    app.setFont(app_font)

    happy_img_path = resource_path("../assets/images/icon/happy.png")
    happy_pixmap = QPixmap(happy_img_path)
    if not happy_pixmap.isNull():
        HAPPY_IMG_DATA_URI = pixmap_to_data_uri(happy_pixmap)
        print("✅ 成功加载 happy.png 并转换为 Data URI")
    else:
        HAPPY_IMG_DATA_URI = ""
        print(f"警告：happy.png 加载失败，路径：{happy_img_path}")

    window = ChatWindow()
    window.show()
    sys.exit(app.exec_())