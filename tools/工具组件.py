import os
import sys
import psutil
import pygame
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, \
    QTabWidget, QRadioButton, QListWidget
from PyQt5.QtGui import QPainter, QColor, QPainterPath, QBrush, QFont, QIcon, QPixmap
from PyQt5.QtCore import Qt, QRectF

pluginsPath = 'plugins'
if os.path.exists(pluginsPath):
    QApplication.addLibraryPath(pluginsPath)


class More(QMainWindow, QTabWidget, QPixmap):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.radius = 15
        self.setWindowTitle("更多工具")
        self.setWindowIcon(QIcon('./icon/setting.ico'))
        main_layout = QVBoxLayout()

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.create_control_buttons())
        button_layout.setContentsMargins(0, 0, 0, 0)

        extra_text_label = QLabel("流萤桌宠工具")
        extra_text_label.setFont(QFont("Arial", 12, QFont.Bold))
        extra_text_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(extra_text_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.list('双击播放'), '音频赏听')
        self.tabs.addTab(self.check(), '小工具')

        mouse_pointer_tab = self.create_radio_button_tab('安装鼠标指针主题', ['流萤指针', '萨姆指针'])
        self.tabs.addTab(mouse_pointer_tab, '鼠标指针')
        self.tabs.addTab(self.create_tab_content('V4.0免费正式版', '桌宠版本'), '关于程序')
        self.tabs.addTab(self.create_tab_content4('https://github.com/ChaozhongLiu/DyberPet          时装二次创作:白咻                 本程序由Gubwin二次创作，开源开放。', '原作者项目地址'), '更多')

        # 修改为浅灰色背景 (#D3D3D3)
        self.tabs.setStyleSheet("""
            QTabWidget {
                background-color: #A9A9A9;  /* 浅灰色背景 */
            }
            QTabBar {
                background-color: #A9A9A9;  /* 浅灰色背景 */
            }
            QTabBar::tab {
                background-color: LightSkyBlue;
                font-weight: bold;
            }
            QTabBar::tab:selected {
                background-color: Deepskyblue;
            }
        """)

        container = QWidget()
        container.setLayout(main_layout)
        main_layout.addLayout(button_layout)
        main_layout.addWidget(self.tabs)

        self.setCentralWidget(container)
        self.resize(800, 500)

    def create_radio_button_tab(self, title, options):
        tab_content = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(QLabel(title))

        for option in options:
            radio_button = QRadioButton(option)
            radio_button.toggled.connect(self.install_mouse)
            layout.addWidget(radio_button)

        tab_content.setLayout(layout)
        tab_content.setContentsMargins(0, 0, 0, 350)
        return tab_content

    def install_mouse(self):
        selected_button = self.sender()

        def open_folder(relative_path):
            absolute_path = os.path.abspath(relative_path)
            if os.path.isdir(absolute_path):
                os.startfile(absolute_path)
            else:
                print(f"路径 {absolute_path} 不是一个有效的文件夹。")

        if selected_button.isChecked():
            if selected_button.text() == '流萤指针':
                folder_path = "./mouse/Firefly"
                open_folder(folder_path)
            elif selected_button.text() == '萨姆指针':
                relative_path = "./mouse/Sam"
                open_folder(relative_path)

    def list(self, text):
        tab_content = QWidget()
        layout = QVBoxLayout()
        list_widget = QListWidget()
        hbox = QHBoxLayout()
        list_widget.setFixedHeight(600)

        music_folder = 'music'

        for file in os.listdir(music_folder):
            if file.endswith(('.mps', '.wav', '.ogg', '')):
                list_widget.addItem(file)

        layout.addWidget(list_widget)

        def play_music(item):
            music_folder = 'music'
            music_folder_path = os.path.abspath(music_folder)
            pygame.mixer.init()
            music_file = item.text()
            next_path = os.path.join(music_folder_path, music_file)

            if music_file != "##双击播放##":
                pygame.mixer.music.load(next_path)
                pygame.mixer.music.play()

        list_widget.itemDoubleClicked.connect(play_music)

        return list_widget
        layout.setContentsMargins(0, 0, 0, 0)

    def check(self):
        tab_content = QWidget()

        def check_battery():
            battery = psutil.sensors_battery()
            plugged = battery.power_plugged
            percent = battery.percent

            if plugged:
                pygame.mixer.init()
                pygame.mixer.music.load("./music/电源适配器已经连接了！现在开始，就是性能模式了哦。.wav")
                pygame.mixer.music.set_volume(0.5)
                pygame.mixer.music.play()
            elif 100 > percent > 20:
                pygame.mixer.init()
                pygame.mixer.music.load("./music/现在的电量保持的不错呢，可以放心玩耍啦！.wav")
                pygame.mixer.music.set_volume(0.5)
                pygame.mixer.music.play()
            elif percent == 100:
                pygame.mixer.init()
                pygame.mixer.music.load("./music/电量已经满了呢，要拔掉电源适配器吗？.wav")
                pygame.mixer.music.set_volume(0.5)
                pygame.mixer.music.play()
            elif percent <= 20:
                pygame.mixer.init()
                pygame.mixer.music.load("./music/啊，当前的电量过低了，如果关机了，可就见不到我咯。.wav")
                pygame.mixer.music.set_volume(0.5)
                pygame.mixer.music.play()

        tab2_layout = QVBoxLayout()
        self.label = QLabel('检测电池状态')
        self.label.setFont(QFont("Arial", 10, QFont.Bold))
        tab2_layout.addWidget(self.label)

        imageLabel = QLabel()
        pixmap = QPixmap("./assets/images/icon/battery.png")
        imageLabel.setPixmap(pixmap.scaled(200, 200, Qt.KeepAspectRatio))
        tab2_layout.addWidget(imageLabel)

        self.button = QPushButton('点击检测')
        self.button.setFont(QFont("Arial", 10, QFont.Bold))
        self.button.clicked.connect(lambda: check_battery())
        tab2_layout.addWidget(self.button)

        tab_content.setLayout(tab2_layout)
        tab2_layout.setContentsMargins(0, 0, 600, 200)
        return tab_content

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = True
            self.offset = event.pos()

    def mouseMoveEvent(self, event):
        if self.draggable:
            self.move(event.globalPos() - self.offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = False

    def create_tab_content(self, text, tab_name):
        tab_content = QWidget()
        imageLabel = QLabel()
        layout = QVBoxLayout()
        pixmap = QPixmap("./assets/images/firefly/icon/icon.png")
        imageLabel.setPixmap(pixmap.scaled(200, 200, Qt.KeepAspectRatio))
        layout.addWidget(imageLabel)

        label = QLabel(f"{tab_name}: {text}")
        label.setFont(QFont("Arial", 10, QFont.Bold))
        layout.addWidget(label)

        tab_content.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 200)
        return tab_content

    def create_tab_content4(self, text, tab_name):
        tab_content = QWidget()
        layout = QVBoxLayout()
        imageLabel = QLabel()
        pixmap = QPixmap("./assets/images/firefly/Lovely/peeping2.png")
        imageLabel.setPixmap(pixmap.scaled(200, 200, Qt.KeepAspectRatio))
        layout.addWidget(imageLabel)

        label = QLabel(f"{tab_name}: {text}")
        label.setFont(QFont("Arial", 10, QFont.Bold))
        layout.addWidget(label)

        tab_content.setLayout(layout)

        imageLabel = QLabel()
        pixmap = QPixmap("./assets/images/work.png")
        imageLabel.setPixmap(pixmap.scaled(450, 400, Qt.KeepAspectRatio))
        layout.addWidget(imageLabel)
        return tab_content

    def create_control_buttons(self):
        buttons = QWidget()
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(800, 0, 10, 0)

        min_button = QPushButton("—")
        min_button.setFont(QFont("Arial", 10, QFont.Bold))
        min_button.clicked.connect(self.showMinimized)
        min_button.setStyleSheet("""
            border-radius: 15px;
            background-color: LightSkyBlue;
            border: 1px solid LightSkyBlue;
            min-width: 20px;
            min-height: 20px;
            """)
        button_layout.addWidget(min_button)
        min_button.setObjectName("min_button")

        close_button = QPushButton("X")
        close_button.setFont(QFont("Arial", 10, QFont.Bold))
        close_button.clicked.connect(self.close)
        close_button.setStyleSheet("""
            border-radius: 15px;
            background-color: LightSkyBlue;
            border: 1px solid LightSkyBlue;
            min-width: 20px;
            min-height: 20px;
            """)
        button_layout.addWidget(close_button)

        button_layout.setSpacing(10)
        buttons.setLayout(button_layout)
        return buttons

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        brush = QBrush(QColor(135, 206, 250))
        painter.setBrush(brush)
        painter.setPen(Qt.NoPen)

        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, self.radius, self.radius)
        painter.drawPath(path)

        painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
        super().paintEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = More()
    window.show()
    sys.exit(app.exec_())