import sys
import os
import pickle
import json
import glob
import webbrowser
import urllib.request
import urllib.error
from typing import Optional
import logging
import tempfile
import subprocess
import shutil
import concurrent.futures
import multiprocessing
from functools import partial, lru_cache
from datetime import datetime
from threading import Thread, Lock
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QDateEdit, QDialog, QMessageBox,
    QFileDialog, QComboBox, QCheckBox, QGroupBox, QFrame, QScrollArea, QTextEdit,
    QSlider, QStackedWidget, QProgressDialog, QProgressBar, QGraphicsDropShadowEffect, QSizePolicy, QListWidget, QListWidgetItem
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QDate, QTimer, QEvent, QUrl
from PyQt6.QtGui import QPainter, QBrush, QLinearGradient, QColor, QPen
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from urllib.parse import urlparse

# Оптимизация для многопоточных операций
MAX_WORKERS = multiprocessing.cpu_count()
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)

GITHUB_REPO = 'Jokissmens/Helper'

# Application version — обновляйте при релизах
APP_VERSION = '0.4.0'

def _normalize_tag(tag: str) -> str:
    if not tag:
        return ''
    return tag.strip().lstrip('vV')

def compare_versions(a: str, b: str) -> int:
    """Compare two versions like '0.4.1' (basic semver compare).

    Returns -1 if a < b, 0 if equal, 1 if a > b
    """
    def to_tuple(s: str):
        try:
            s = _normalize_tag(s)
            parts = [int(x) if x.isdigit() else 0 for x in s.split('.')]
            while len(parts) < 3:
                parts.append(0)
            return tuple(parts[:3])
        except Exception:
            return (0, 0, 0)

    ta = to_tuple(a)
    tb = to_tuple(b)
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0

def fetch_latest_github_release(repo: str, timeout: float = 5.0) -> Optional[dict]:
    """Get latest release info from GitHub API (simple wrapper)."""
    if not repo or '/' not in repo:
        return None
    api = f'https://api.github.com/repos/{repo}/releases/latest'
    headers = {
        'User-Agent': 'Helper-Updater/1.0',
        'Accept': 'application/vnd.github.v3+json'
    }
    req = urllib.request.Request(api, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            data = json.loads(raw.decode('utf-8'))
            return {
                'tag_name': data.get('tag_name'),
                'html_url': data.get('html_url'),
                'name': data.get('name'),
                'body': data.get('body'),
                'assets': data.get('assets', [])
            }
    except urllib.error.HTTPError as e:
        # Если нет релизов (404) — попробуем как fallback взять последний тег из /tags
        logging.debug(f'GitHub API HTTPError (releases): {e.code} {e.reason}')
        if e.code == 404:
            try:
                tags_api = f'https://api.github.com/repos/{repo}/tags'
                reqt = urllib.request.Request(tags_api, headers=headers)
                with urllib.request.urlopen(reqt, timeout=timeout) as tr:
                    rawt = tr.read()
                    tags = json.loads(rawt.decode('utf-8'))
                    if isinstance(tags, list) and tags:
                        top = tags[0]
                        tname = top.get('name') or top.get('ref')
                        # construct a minimal release-like dict
                        return {
                            'tag_name': tname,
                            'html_url': f'https://github.com/{repo}/releases' ,
                            'name': tname,
                            'body': None,
                            'assets': []
                        }
            except Exception:
                logging.debug('Fallback to tags failed')
        return None
    except Exception as e:
        logging.debug(f'Ошибка при обращении к GitHub API: {e}')
        return None

def fetch_github_file(repo: str, path: str, ref: str = None, timeout: float = 6.0) -> Optional[dict]:
    """Fetch file content from GitHub repository via contents API.

    Returns dict with keys: 'content' (decoded str), 'sha', 'download_url' or None on error.
    """
    if not repo or '/' not in repo:
        return None

    api = f'https://api.github.com/repos/{repo}/contents/{path}'
    if ref:
        api += f'?ref={ref}'
    headers = {
        'User-Agent': 'Helper-Updater/1.0',
        'Accept': 'application/vnd.github.v3+json'
    }
    req = urllib.request.Request(api, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            data = json.loads(raw.decode('utf-8'))
            content_b64 = data.get('content')
            if not content_b64:
                return None
            import base64
            try:
                decoded = base64.b64decode(content_b64).decode('utf-8', errors='replace')
            except Exception:
                decoded = base64.b64decode(content_b64 + '==').decode('utf-8', errors='replace')

            return {
                'content': decoded,
                'sha': data.get('sha'),
                'download_url': data.get('download_url'),
                'path': data.get('path')
            }
    except urllib.error.HTTPError as e:
        logging.debug(f'GitHub file HTTPError: {e.code} {e.reason}')
        return None
    except Exception as e:
        logging.debug(f'Ошибка при обращении к GitHub contents API: {e}')
        return None
# Оптимизация доступа к файлам
file_lock = Lock()

# Кэширование для часто используемых операций
@lru_cache(maxsize=128)
def get_file_size(file_path):
    """Кэшированное получение размера файла."""
    try:
        return os.path.getsize(file_path)
    except (OSError, IOError):
        return 0
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from urllib.parse import urlparse

# Оптимизация настроек окружения и Qt
os.environ.update({
    'QT_LOGGING_RULES': '*.debug=false;qt.qpa.*=false',
    'QT_QPA_PLATFORM': 'windows:darkmode=0',
    'PYTHONOPTIMIZE': '2',  # Включаем оптимизации Python
    'PYTHONASYNCIODEBUG': '0'  # Отключаем отладку asyncio
})

# Конфигурация логирования с ротацией и оптимизацией производительности
def setup_optimized_logging():
    from logging.handlers import RotatingFileHandler
    
    # Оптимизированные настройки логирования
    log_file = 'Logi.log'
    max_bytes = 5 * 1024 * 1024  # 5 MB
    backup_count = 3
    
    try:
        # Создаем директорию для логов если нужно
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        # Настраиваем форматирование
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Файловый обработчик с ротацией
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        
        # Консольный обработчик
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        # Настройка корневого логгера
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        
        # Очищаем существующие обработчики
        root_logger.handlers.clear()
        
        # Добавляем новые обработчики
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
        # Отключаем пропагацию для некоторых логгеров
        for logger_name in ['PIL', 'googleapiclient.discovery', 'oauth2client.client']:
            logging.getLogger(logger_name).propagate = False
            
    except Exception as e:
        # Fallback к базовому логированию
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        logging.error(f"Ошибка настройки расширенного логирования: {e}")

# Инициализация оптимизированного логирования
setup_optimized_logging()

class MainWindow(QMainWindow):
    """Главное окно приложения с оптимизированным управлением ресурсами."""
    
    # Константы для оптимизации
    CONFIG_FILE = 'config.json'
    CREDENTIALS_FILE = 'token.pickle'
    THEME_FILE = 'theme.txt'
    HISTORY_FILE = 'upload_history.json'
    AHK_DATA_FILE = 'ahk_data.json'
    
    def _setup_logging(self):
        """Инициализация логирования для экземпляра MainWindow (fallback).

        Этот метод просто вызывает глобальную функцию настройки логирования
        и гарантирует, что экземпляр имеет атрибут logger.
        """
        try:
            setup_optimized_logging()
            self.logger = logging.getLogger(__name__)
        except Exception as e:
            logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
            logging.error(f"Fallback логирования: {e}")
            self.logger = logging.getLogger(__name__)
    
    def _setup_logging(self):
        """Совместимый метод-обёртка для инициализации логирования в этом классе.

        Делегируем настройку модульной функции setup_optimized_logging() и
        обеспечиваем безопасный fallback.
        """
        try:
            setup_optimized_logging()
            self.logger = logging.getLogger(__name__)
        except Exception as e:
            logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
            logging.error(f"Не удалось инициализировать логирование: {e}")
            self.logger = logging.getLogger(__name__)
    
    def _setup_logging(self):
        """Совместимый метод-обёртка для инициализации логирования.

        Ранее код ожидает метод экземпляра; здесь делегируем в модульную
        функцию `setup_optimized_logging`, обеспечивая fallback при ошибке.
        """
        try:
            setup_optimized_logging()
            # Эксплицитный логгер экземпляра (удобно для дальнейших вызовов)
            self.logger = logging.getLogger(__name__)
        except Exception as e:
            # На случай ошибок — минимальная настройка логирования
            logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
            logging.error(f"Не удалось инициализировать оптимизированное логирование: {e}")
            self.logger = logging.getLogger(__name__)
    
    def _setup_logging(self):
        """Настройка оптимизированного логирования с ротацией файлов."""
        log_file = 'Logi.log'
        max_bytes = 1024 * 1024  # 1 MB
        backup_count = 3
    
    def _setup_logging(self):
        """Настройка оптимизированного логирования с ротацией файлов."""
        log_file = 'Logi.log'
        max_bytes = 1024 * 1024  # 1 MB
        backup_count = 3
        
        try:
            from logging.handlers import RotatingFileHandler
            
            # Создаем директорию для логов если её нет
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)
                
            # Настраиваем форматирование
            formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            
            # Файловый обработчик с ротацией
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding='utf-8'
            )
            file_handler.setFormatter(formatter)
            
            # Консольный обработчик
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            
            # Настраиваем корневой логгер
            root_logger = logging.getLogger()
            root_logger.setLevel(logging.INFO)
            root_logger.addHandler(file_handler)
            root_logger.addHandler(console_handler)
            
        except Exception as e:
            # Fallback к базовому логированию при ошибке
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s - %(levelname)s - %(message)s',
                handlers=[
                    logging.FileHandler(log_file, encoding='utf-8'),
                    logging.StreamHandler()
                ]
            )
            logging.error(f"Ошибка настройки расширенного логирования: {e}")

SCOPES = ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly']

# softer default gradient and accent color
ACCENT = "#A259FF"  # замените на "#3BE8B0" при желании бирюзового акцента
SOFT_GRAD_START = QColor("#283CFF")
SOFT_GRAD_END = QColor("#1A1A2E")

THEMES = {
    "Классическая": {"s": SOFT_GRAD_START, "e": SOFT_GRAD_END, "b": "rgba(30,30,45,0.85)", "i": "rgba(255,255,255,0.12)", "r": "rgba(255,255,255,0.25)"},
    "Темная": {"s": QColor(45,45,55), "e": QColor(60,60,70), "b": "rgba(35,35,45,0.9)", "i": "rgba(255,255,255,0.08)", "r": "rgba(255,255,255,0.2)"},
    "Океан": {"s": QColor(0,119,182), "e": QColor(0,180,216), "b": "rgba(25,45,65,0.85)", "i": "rgba(255,255,255,0.12)", "r": "rgba(255,255,255,0.25)"},
    "Закат": {"s": QColor(255,94,77), "e": QColor(255,154,158), "b": "rgba(65,35,35,0.85)", "i": "rgba(255,255,255,0.12)", "r": "rgba(255,255,255,0.25)"},
    "Лес": {"s": QColor(34,139,34), "e": QColor(46,204,113), "b": "rgba(25,45,25,0.85)", "i": "rgba(255,255,255,0.12)", "r": "rgba(255,255,255,0.25)"},
    "Фиолетовый сон": {"s": QColor(142,45,226), "e": QColor(74,0,224), "b": "rgba(45,25,65,0.85)", "i": "rgba(255,255,255,0.12)", "r": "rgba(255,255,255,0.25)"},
    "Зима": {"s": QColor("#7FD3FF"), "e": QColor("#1A2B4C"), "b": "rgba(10,20,40,0.9)", "i": "rgba(220,240,255,0.12)", "r": "rgba(200,230,255,0.22)"}
}

class UploadThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)
    
    # Оптимизированные константы для загрузки
    CHUNK_SIZE = 5 * 1024 * 1024  # 5MB chunks для лучшей производительности
    MAX_RETRIES = 3               # Максимальное количество попыток при ошибках
    RETRY_DELAY = 2               # Задержка между попытками в секундах
    
    def __init__(self, creds, path, title, desc, allow_missing_ffmpeg=False, privacy_status='private'):
        super().__init__()
        self.creds = creds
        self.path = path
        self.title = title
        self.desc = desc
        # если True — пропускаем проверки ffmpeg и даём возможность загружать без обрезки
        self.allow_missing_ffmpeg = bool(allow_missing_ffmpeg)
        self._is_cancelled = False
        self._upload_progress = 0
        self._last_progress_update = 0
        # privacy status will be one of: 'private', 'unlisted', 'public'
        self.privacy_status = privacy_status if privacy_status in ('private','unlisted','public') else 'private'
    
    def cancel(self):
        self._is_cancelled = True
    
    def _validate_video_file(self, path):
        """Проверка валидности видео файла."""
        if not os.path.exists(path):
            raise FileNotFoundError("Видео файл не найден")
            
        file_size = get_file_size(path)  # Используем кэшированную функцию
        if file_size == 0:
            raise ValueError("Видео файл пуст")
            
        # Проверка формата файла и его читаемости через ffmpeg
        if shutil.which('ffmpeg') is None:
            # если настройка разрешает загрузку без ffmpeg — пропускаем проверку
            if getattr(self, 'allow_missing_ffmpeg', False):
                logging.info('FFmpeg не найден, но загрузка разрешена настройкой (allow_missing_ffmpeg=True)')
                return
            # Ясное сообщение — ffmpeg недоступен в PATH
            raise ValueError("FFmpeg не найден в системе. Установите FFmpeg и добавьте его в PATH (например: C:\\ffmpeg\\bin)")

        try:
            result = subprocess.run(
                ['ffmpeg', '-v', 'error', '-i', path, '-f', 'null', '-'],
                capture_output=True,
                text=True
            )
            if result.stderr:
                raise ValueError(f"Видео файл повреждён: {result.stderr}")
        except FileNotFoundError:
            # На случай, если бинарник удалили между проверкой и запуском
            raise ValueError("FFmpeg бинарник не найден. Убедитесь, что ffmpeg доступен в PATH")
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Ошибка проверки видео: {e.stderr if e.stderr else str(e)}")
    
    def _prepare_upload_body(self):
        """Подготовка метаданных для загрузки."""
        return {
            'snippet': {
                'title': self.title,
                'description': self.desc,
                'categoryId': '22',
                'tags': ['complaint', 'report'],
                'defaultLanguage': 'ru',
                'defaultAudioLanguage': 'ru'
            },
            'status': {
                'privacyStatus': self.privacy_status,
                'selfDeclaredMadeForKids': False,
                'embeddable': True,
                'license': 'youtube'
            },
            'recordingDetails': {
                'recordingDate': datetime.now().isoformat() + "Z"
            }
        }
    
    def run(self):
        try:
            if self._is_cancelled:
                return
                
            # Валидация файла перед загрузкой
            try:
                self.progress.emit("Проверка видео файла...")
                self._validate_video_file(self.path)
            except Exception as e:
                self.finished.emit(False, f"Ошибка проверки видео: {str(e)}")
                return
                
            # Подключение к API
            self.progress.emit("Подключение к YouTube API...")
            for attempt in range(self.MAX_RETRIES):
                try:
                    yt = build('youtube', 'v3', credentials=self.creds)
                    break
                except Exception as e:
                    if attempt == self.MAX_RETRIES - 1:
                        raise
                    logging.warning(f"Попытка подключения {attempt + 1} не удалась: {e}")
                    import time
                    time.sleep(self.RETRY_DELAY)
            
            if self._is_cancelled:
                return
                
            # Подготовка загрузки
            self.progress.emit("Подготовка видео...")
            file_size = get_file_size(self.path)  # Используем кэшированную функцию
            
            if self._is_cancelled:
                return
                
            self.progress.emit("Загрузка на YouTube...")

            # Подготовка метаданных
            body = self._prepare_upload_body()

            # Определяем MIME-тип на основе расширения файла
            file_ext = os.path.splitext(self.path)[1].lower()
            mime_types = {
                '.mp4': 'video/mp4',
                '.avi': 'video/x-msvideo',
                '.mov': 'video/quicktime',
                '.mkv': 'video/x-matroska',
                '.flv': 'video/x-flv',
                '.wmv': 'video/x-ms-wmv'
            }
            mime_type = mime_types.get(file_ext, 'video/mp4')

            media = MediaFileUpload(
                self.path,
                chunksize=self.CHUNK_SIZE,
                resumable=True,
                mimetype=mime_type
            )

            req = yt.videos().insert(
                part='snippet,status,recordingDetails',
                body=body,
                media_body=media
            )

            response = None
            last_progress_time = 0
            last_progress_value = 0
            retry_count = 0

            # Загрузка с обработкой ошибок и возобновлением
            import time
            while response is None:
                if self._is_cancelled:
                    return

                try:
                    status, response = req.next_chunk()
                    retry_count = 0

                    if status:
                        uploaded = getattr(status, 'resumable_progress', None)
                        if uploaded is None:
                            # иногда status содержит progress в другом поле
                            uploaded = getattr(status, 'progress', 0)
                        percent = (uploaded / file_size) * 100 if file_size else 0

                        cur_time = time.time()
                        if (cur_time - last_progress_time >= 0.5) or (abs(percent - last_progress_value) >= 1):
                            uploaded_mb = uploaded / (1024 * 1024)
                            total_mb = file_size / (1024 * 1024) if file_size else 0
                            self.progress.emit(f"Загружено: {int(percent)}% ({uploaded_mb:.1f}/{total_mb:.1f} MB)")
                            last_progress_time = cur_time
                            last_progress_value = percent

                except Exception as e:
                    retry_count += 1
                    logging.warning(f"Ошибка при загрузке чанка (попытка {retry_count}): {e}")
                    if retry_count > self.MAX_RETRIES:
                        raise
                    time.sleep(self.RETRY_DELAY)
                    continue

            video_id = response.get('id') if isinstance(response, dict) else None
            if not video_id:
                raise Exception('Не удалось получить id загруженного видео')

            url = f"https://www.youtube.com/watch?v={video_id}"
            logging.info(f"Видео успешно загружено: {url}")
            self.finished.emit(True, url)
            
        except Exception as e:
            logging.exception("Ошибка при загрузке видео")
            error_msg = str(e)
            if "quota" in error_msg.lower():
                error_msg = "Превышен дневной лимит загрузок YouTube. Попробуйте позже."
            elif "credentials" in error_msg.lower():
                error_msg = "Ошибка авторизации. Попробуйте авторизоваться заново."
            self.finished.emit(False, f"Ошибка: {error_msg}")

class GradientWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # softer default gradient
        self.gs, self.ge = SOFT_GRAD_START, SOFT_GRAD_END
    
    def set_gradient(self, s, e):
        self.gs, self.ge = s, e
        self.update()
    
    def paintEvent(self, event):
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), 0)
        g.setColorAt(0, self.gs)
        g.setColorAt(1, self.ge)
        p.fillRect(self.rect(), QBrush(g))


class ReleaseCheckThread(QThread):
    """Фоновая проверка релиза на GitHub. Возвращает результат через сигнал.

    Emitted dict structure: {'ok': bool, 'release': dict|None, 'error': str|None}
    """
    done = pyqtSignal(dict)

    def __init__(self, repo: str, parent=None, timeout: float = 6.0):
        super().__init__(parent)
        self.repo = repo
        self.timeout = timeout

    def run(self):
        try:
            res = fetch_latest_github_release(self.repo, timeout=self.timeout)
            if res is None:
                self.done.emit({'ok': False, 'release': None, 'error': 'Не удалось получить данные (сеть/repo).'})
            else:
                self.done.emit({'ok': True, 'release': res, 'error': None})
        except Exception as e:
            self.done.emit({'ok': False, 'release': None, 'error': str(e)})


class FileCheckThread(QThread):
    """Фоновая проверка конкретного файла в репозитории GitHub."""
    done = pyqtSignal(dict)

    def __init__(self, repo: str, path: str, parent=None, ref: str | None = None, timeout: float = 6.0):
        super().__init__(parent)
        self.repo = repo
        self.path = path
        self.ref = ref
        self.timeout = timeout

    def run(self):
        try:
            res = fetch_github_file(self.repo, self.path, ref=self.ref, timeout=self.timeout)
            if res is None:
                self.done.emit({'ok': False, 'file': None, 'error': 'Не удалось получить содержимое файла.'})
            else:
                self.done.emit({'ok': True, 'file': res, 'error': None})
        except Exception as e:
            self.done.emit({'ok': False, 'file': None, 'error': str(e)})


class DownloadReplaceThread(QThread):
    """Скачивает файл из репозитория и заменяет локальную копию атомарно, делая backup."""
    done = pyqtSignal(dict)

    def __init__(self, repo: str, path: str, local_path: str, parent=None, timeout: float = 10.0):
        super().__init__(parent)
        self.repo = repo
        self.path = path
        self.local_path = local_path
        self.timeout = timeout

    def run(self):
        try:
            res = fetch_github_file(self.repo, self.path, timeout=self.timeout)
            if not res or not res.get('content'):
                self.done.emit({'ok': False, 'backup': None, 'error': 'Не удалось скачать файл.'})
                return

            content = res['content']

            # Read existing if present
            backup_path = None
            try:
                if os.path.exists(self.local_path):
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    backup_path = f"{self.local_path}.bak.{ts}"
                    # copy2 preserves metadata
                    shutil.copy2(self.local_path, backup_path)
            except Exception:
                # If backup fails, continue but log
                logging.exception('Не удалось создать бэкап локального файла')

            # Write new file to a temp file then replace atomically
            try:
                fd, tmp = tempfile.mkstemp(suffix='.tmp', prefix='helper_', dir=os.path.dirname(self.local_path) or None, text=True)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(content)
                # Ensure safe replace using file_lock
                with file_lock:
                    os.replace(tmp, self.local_path)

                self.done.emit({'ok': True, 'backup': backup_path, 'error': None})
                return
            except Exception as e:
                logging.exception('Ошибка записи/замены файла')
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
                self.done.emit({'ok': False, 'backup': backup_path, 'error': str(e)})
                return

        except Exception as e:
            logging.exception('Ошибка в DownloadReplaceThread')
            self.done.emit({'ok': False, 'backup': None, 'error': str(e)})

# animated decorative overlays
class SnowEffectWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.flakes = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.step)
        self.timer.start(40)
        self._init_flakes()

    def _init_flakes(self, n=40):
        import random
        self.flakes = []
        w, h = max(1, self.width()), max(1, self.height())
        for _ in range(n):
            x = random.uniform(0, w)
            y = random.uniform(0, h)
            r = random.uniform(2, 6)
            s = random.uniform(0.5, 2.0)
            self.flakes.append({'x': x, 'y': y, 'r': r, 's': s})

    def resizeEvent(self, e):
        self._init_flakes()

    def step(self):
        import random
        w, h = max(1, self.width()), max(1, self.height())
        for f in self.flakes:
            f['y'] += f['s']
            f['x'] += (f['s'] * 0.2) * (1 if random.random() > 0.5 else -1)
            if f['y'] > h + 10:
                f['y'] = -10
                f['x'] = random.uniform(0, w)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 200))
        for f in self.flakes:
            p.drawEllipse(int(f['x']), int(f['y']), int(f['r']), int(f['r']))


def format_time(ms):
    """Форматирует миллисекунды в чч:мм:сс."""
    s = int(ms / 1000)
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

class VideoTrimDialog(QDialog):
    """Мини-редактор: выбор начала/конца, предпросмотр и экспорт обрезанной версии.
    
    Оптимизированная версия с кэшированием предпросмотра и эффективной обработкой видео.
    """
    # Константы для оптимизации производительности
    PREVIEW_CACHE_SIZE = 10  # Количество кадров для кэширования
    PROGRESS_UPDATE_INTERVAL = 100  # Миллисекунды между обновлениями прогресса
    def __init__(self, parent, input_path):
        super().__init__(parent)
        self.input_path = input_path
        self.result_path = None
        self.setWindowTitle("Редактировать видео")
        self.resize(800, 600)
        self.setStyleSheet("""
            QDialog {
                background: #2D2D2D;
                color: white;
            }
            QLabel {
                color: white;
            }
            QPushButton {
                color: white;
                min-width: 100px;
            }
            QSlider::groove:horizontal {
                height: 8px;
                background: rgba(255,255,255,0.1);
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #A259FF;
                width: 16px;
                margin: -4px 0;
                border-radius: 8px;
            }
            QSlider::range:horizontal {
                height: 8px;
                background: rgba(162,89,255,0.3);
                border-radius: 4px;
            }
        """)

        # Группировка виджетов
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(10,10,10,10)
        vbox.setSpacing(10)

        # Заголовок
        title = QLabel("Редактировать видео")
        title.setStyleSheet("font-size: 18px; font-weight: bold; padding: 8px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vbox.addWidget(title)
        
        # Video preview
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(360)
        vbox.addWidget(self.video_widget, 1)

        # Плеер
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)

        # Контейнер для таймлайна и звука
        timeline = QWidget()
        tl = QVBoxLayout(timeline)
        tl.setContentsMargins(15,5,15,5)
        tl.setSpacing(5)

        # Слайдер для перемотки
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        tl.addWidget(self.position_slider)

        # Контейнер для времени и кнопок управления
        time_controls = QHBoxLayout()
        
        # Текущее время / общая длительность
        self.time_label = QLabel("00:00:00 / 00:00:00")
        self.time_label.setStyleSheet("color: white; font-size: 13px;")
        time_controls.addWidget(self.time_label)
        
        # Регулятор громкости
        volume_container = QHBoxLayout()
        volume_icon = QLabel("🔊")
        volume_icon.setStyleSheet("color: white; font-size: 16px;")
        volume_container.addWidget(volume_icon)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setValue(100)
        self.volume_slider.setMaximumWidth(100)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        volume_container.addWidget(self.volume_slider)
        time_controls.addLayout(volume_container)
        
        # Контейнер для кнопок управления
        buttons_container = QHBoxLayout()
        buttons_container.setSpacing(8)
        
        # Кнопка воспроизведения
        play_btn = QPushButton("⏵")
        play_btn.setFixedSize(40, 40)
        play_btn.clicked.connect(self.toggle_playback)
        play_btn.setStyleSheet("""
            QPushButton { 
                border-radius: 20px; 
                background: rgba(255,255,255,0.1);
                font-size: 18px;
            }
            QPushButton:hover {
                background: rgba(255,255,255,0.15);
            }
        """)
        buttons_container.addWidget(play_btn)
        
        # Кнопка полноэкранного режима
        fullscreen_btn = QPushButton("⛶")
        fullscreen_btn.setFixedSize(40, 40)
        fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        fullscreen_btn.setStyleSheet("""
            QPushButton { 
                border-radius: 20px; 
                background: rgba(255,255,255,0.1);
                font-size: 18px;
            }
            QPushButton:hover {
                background: rgba(255,255,255,0.15);
            }
        """)
        buttons_container.addWidget(fullscreen_btn)
        
        time_controls.addLayout(buttons_container)
        time_controls.addWidget(play_btn)
        tl.addLayout(time_controls)

        # Слайдеры для точек начала/конца
        trim_box = QGroupBox("Выбор фрагмента")
        trim_box.setStyleSheet("""
            QGroupBox { 
                color: white;
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 8px;
                padding: 15px;
                margin-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        tb = QVBoxLayout(trim_box)
        
        # Ползунки начала и конца
        self.trim_start_slider = QSlider(Qt.Orientation.Horizontal)
        self.trim_end_slider = QSlider(Qt.Orientation.Horizontal)
        
        # Начало
        start_layout = QHBoxLayout()
        start_layout.addWidget(QLabel("Начало:"))
        self.start_label = QLabel("00:00:00")
        self.start_label.setStyleSheet("min-width: 70px;")
        start_layout.addWidget(self.start_label)
        start_layout.addWidget(self.trim_start_slider)
        tb.addLayout(start_layout)
        
        # Конец
        end_layout = QHBoxLayout()
        end_layout.addWidget(QLabel("Конец:"))
        self.end_label = QLabel("00:00:00")
        self.end_label.setStyleSheet("min-width: 70px;")
        end_layout.addWidget(self.end_label)
        end_layout.addWidget(self.trim_end_slider)
        tb.addLayout(end_layout)
        
        # События изменения ползунков
        self.trim_start_slider.valueChanged.connect(self._on_trim_start_changed)
        self.trim_end_slider.valueChanged.connect(self._on_trim_end_changed)
        
        vbox.addWidget(timeline)
        vbox.addWidget(trim_box)

        # Кнопки действий
        actions = QWidget()
        al = QHBoxLayout(actions)

        self.preview_btn = QPushButton("👁 Просмотреть")
        self.preview_btn.clicked.connect(lambda: self.play_clip(True))
        self.preview_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(76,175,80,0.3);
                border: 2px solid rgba(76,175,80,0.5);
                border-radius: 8px;
                padding: 8px 15px;
            }
            QPushButton:hover {
                background-color: rgba(76,175,80,0.4);
            }
        """)
        al.addWidget(self.preview_btn)

        self.trim_btn = QPushButton("✓ Утвердить")
        self.trim_btn.clicked.connect(self.trim_and_accept)
        self.trim_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(33,150,243,0.3);
                border: 2px solid rgba(33,150,243,0.5);
                border-radius: 8px;
                padding: 8px 15px;
            }
            QPushButton:hover {
                background-color: rgba(33,150,243,0.4);
            }
        """)
        al.addWidget(self.trim_btn)

        self.cancel_btn = QPushButton("✕ Отмена")
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,80,80,0.3);
                border: 2px solid rgba(255,80,80,0.5);
                border-radius: 8px;
                padding: 8px 15px;
            }
            QPushButton:hover {
                background-color: rgba(255,80,80,0.4);
            }
        """)
        al.addWidget(self.cancel_btn)
        vbox.addWidget(actions)

        # internal
        self.duration_ms = 0
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.position_slider.sliderMoved.connect(self._on_slider_moved)

        # load source
        try:
            self.player.setSource(QUrl.fromLocalFile(self.input_path))
        except Exception:
            # fallback to older API
            try:
                self.player.setSource(self.input_path)
            except Exception:
                pass

        # stop at chosen end
        self._play_stop_ms = None
        self._is_playing = False

    def _on_duration_changed(self, d):
        # d in ms
        self.duration_ms = d
        seconds = int(d / 1000) if d else 0
        self.position_slider.setRange(0, max(0, int(d)))
        
        # Устанавливаем диапазоны для ползунков
        self.trim_start_slider.setRange(0, max(0, int(d)))
        self.trim_end_slider.setRange(0, max(0, int(d)))
        
        # По умолчанию конец = длительность
        self.trim_end_slider.setValue(int(d))
        
        # Обновляем метки времени
        self.time_label.setText(f"00:00:00 / {format_time(d)}")
        self.end_label.setText(format_time(d))

    def _on_slider_moved(self, position):
        if self.player.duration() > 0:
            self.player.setPosition(position)

    def _on_position_changed(self, pos):
        # Update time label
        if self.player.duration() > 0:
            self.time_label.setText(f"{format_time(pos)} / {format_time(self.duration_ms)}")
            
        # Move slider
        self.position_slider.setValue(int(pos))
        
        # Stop at chosen end point
        if self._play_stop_ms and pos >= self._play_stop_ms and self._is_playing:
            self.player.pause()
            self._play_stop_ms = None
            self._is_playing = False

    def toggle_playback(self):
        if not self._is_playing:
            self.player.play()
            self._is_playing = True
        else:
            self.player.pause()
            self._is_playing = False
            
    def toggle_fullscreen(self):
        if self.video_widget.isFullScreen():
            self.video_widget.setFullScreen(False)
            self.show()  # Показываем основное окно редактора
        else:
            self.video_widget.setFullScreen(True)
            self.hide()  # Скрываем основное окно редактора
            
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.video_widget.isFullScreen():
            self.toggle_fullscreen()
        super().keyPressEvent(event)

    def _on_volume_changed(self, value):
        self.audio_output.setVolume(value / 100.0)
    
    def _on_trim_start_changed(self, value):
        if value >= self.trim_end_slider.value():
            self.trim_start_slider.setValue(self.trim_end_slider.value() - 1000)
        self.start_label.setText(format_time(value))
    
    def _on_trim_end_changed(self, value):
        if value <= self.trim_start_slider.value():
            self.trim_end_slider.setValue(self.trim_start_slider.value() + 1000)
        self.end_label.setText(format_time(value))
    
    def play_clip(self, preview_fragment=False):
        start = self.trim_start_slider.value()
        end = self.trim_end_slider.value()
        
        if end <= start:
            QMessageBox.warning(self, "Ошибка", "Время конца должно быть больше времени начала.")
            return
        try:
            self.player.setPosition(start)
            self._play_stop_ms = end if preview_fragment else None
            self._is_playing = True
            self.player.play()            
        except Exception as e:
            logging.exception("Ошибка воспроизведения")
            QMessageBox.warning(self, "Ошибка", f"Не удалось воспроизвести: {e}")

    def trim_and_accept(self):
        """Обрезка видео с помощью FFmpeg с оптимизированной обработкой."""
        # Проверяем наличие FFmpeg
        if not self._check_ffmpeg():
            return

        start = self.trim_start_slider.value() / 1000  # Convert to seconds
        end = self.trim_end_slider.value() / 1000

        if end <= start:
            QMessageBox.warning(self, "Ошибка", "Время конца должно быть больше времени начала.")
            return

        # Создаем временный файл для обрезанного видео
        fd, temp_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)

        # Создаем диалог прогресса
        progress = QProgressDialog("Обработка видео...", "Отмена", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(True)
        progress.setValue(0)

        def process_video():
            try:
                # Сначала пробуем быстрое копирование потока
                success = self._try_fast_trim(start, end, temp_path, progress)
                
                # Если быстрое копирование не удалось, используем перекодирование
                if not success and not progress.wasCanceled():
                    success = self._try_encode_trim(start, end, temp_path, progress)
                
                if success and not progress.wasCanceled():
                    self.result_path = temp_path
                    progress.setValue(100)
                    self.accept()
                else:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                        
            except Exception as e:
                logging.exception('Ошибка обработки видео')
                QMessageBox.warning(self, 'Ошибка', f'Не удалось обрезать видео: {str(e)}')
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            finally:
                progress.close()

        # Запускаем обработку в отдельном потоке
        Thread(target=process_video, daemon=True).start()

    def _check_ffmpeg(self):
        """Проверка наличия FFmpeg в системе."""
        # Проверяем бинарник сначала через shutil.which, это надёжнее и не вызывает исключение WinError 2
        if shutil.which('ffmpeg') is None:
            QMessageBox.warning(
                self,
                "FFmpeg не найден",
                "FFmpeg не найден в PATH. Для обрезки видео требуется установить FFmpeg и добавить путь к папке bin в системную переменную PATH.\n\n" \
                "Инструкция:\n1) Скачайте FFmpeg: https://ffmpeg.org/download.html\n2) Разархивируйте в, например, C:\\ffmpeg\n3) Добавьте C:\\ffmpeg\\bin в PATH\n4) Перезапустите программу"
            )
            return False

        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
            return True
        except FileNotFoundError:
            QMessageBox.warning(
                self,
                "FFmpeg не найден",
                "FFmpeg бинарник недоступен (удалён или блокируется). Проверьте PATH и антивирус." 
            )
            return False
        except Exception:
            QMessageBox.warning(
                self,
                "FFmpeg не найден",
                "Для обрезки видео требуется установить FFmpeg:\n\n"
                "1. Скачайте FFmpeg с официального сайта:\n"
                "   https://ffmpeg.org/download.html\n\n"
                "2. Распакуйте архив в удобное место\n"
                "   (например, C:\\ffmpeg)\n\n"
                "3. Добавьте путь к папке bin в PATH:\n"
                "   - Откройте Параметры системы\n"
                "   - Переменные среды\n"
                "   - Выберите Path\n"
                "   - Добавьте путь к папке bin\n"
                "   (например, C:\\ffmpeg\\bin)\n\n"
                "4. Перезапустите программу"
            )
            return False

    def _try_fast_trim(self, start, end, output_path, progress):
        """Попытка быстрой обрезки без перекодирования."""
        # Защитная проверка наличия ffmpeg перед вызовом
        if shutil.which('ffmpeg') is None:
            logging.error("FFmpeg не найден: быстрая обрезка невозможна")
            return False

        try:
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(start),
                '-i', self.input_path,
                '-t', str(end - start),
                '-c', 'copy',
                output_path
            ]
            
            try:
                process = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                universal_newlines=True
                )
            except FileNotFoundError:
                logging.error("FFmpeg не найден при попытке запуска subprocess.Popen")
                return False
            
            # Обработка вывода FFmpeg для обновления прогресса
            duration = end - start
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                
                if progress.wasCanceled():
                    process.terminate()
                    return False
                    
                if 'time=' in line:
                    try:
                        time_str = line.split('time=')[1].split()[0]
                        current_time = sum(float(x) * 60 ** i for i, x in enumerate(reversed(time_str.split(':'))))
                        progress.setValue(int(min(current_time / duration * 100, 99)))
                    except:
                        pass
            
            return process.wait() == 0
            
        except Exception as e:
            logging.error(f"Ошибка быстрой обрезки: {e}")
            return False

    def _try_encode_trim(self, start, end, output_path, progress):
        """
        Оптимизированная обрезка видео с двухпроходным кодированием.
        Обеспечивает лучшее качество при меньшем размере файла.
        """
        # Защитная проверка наличия ffmpeg
        if shutil.which('ffmpeg') is None:
            logging.error("FFmpeg не найден: кодирование невозможно")
            return False

        try:
            # Создаем временную директорию для логов двухпроходного кодирования
            with tempfile.TemporaryDirectory() as temp_dir:
                passlog_file = os.path.join(temp_dir, 'ffmpeg2pass.log')
                
                # Базовые параметры кодирования для обоих проходов
                base_params = [
                    '-ss', str(start),
                    '-i', self.input_path,
                    '-t', str(end - start),
                    '-c:v', 'libx264',
                    '-preset', 'faster',     # Оптимальный баланс скорость/качество
                    '-profile:v', 'high',    # Профиль высокого качества
                    '-level', '4.1',         # Максимальная совместимость
                    '-pix_fmt', 'yuv420p',   # Стандартный формат пикселей
                    '-movflags', '+faststart', # Оптимизация для веб
                    '-maxrate', '5000k',     # Ограничение битрейта
                    '-bufsize', '10000k',    # Размер буфера
                    '-g', '50',              # GOP size
                    '-keyint_min', '25',     # Минимальный интервал ключевых кадров
                    '-sc_threshold', '40',    # Порог смены сцены
                    '-c:a', 'aac',           # Аудиокодек
                    '-b:a', '128k',          # Битрейт аудио
                    '-ar', '44100',          # Частота дискретизации
                    '-y'                     # Перезапись файла
                ]
                
                # Первый проход - анализ
                first_pass = [
                    'ffmpeg',
                    *base_params,
                    '-pass', '1',
                    '-an',                   # Без аудио в первом проходе
                    '-f', 'null',
                    'NUL'                    # Windows NUL device
                ]
                
                # Второй проход - финальное кодирование
                second_pass = [
                    'ffmpeg',
                    *base_params,
                    '-pass', '2',
                    '-crf', '23'             # Постоянное качество
                ]
                
                # Выполняем первый проход
                try:
                    process = subprocess.Popen(
                        first_pass,
                        stderr=subprocess.PIPE,
                        universal_newlines=True
                    )
                except FileNotFoundError:
                    logging.error("FFmpeg не найден при запуске первого прохода")
                    return False
                
                # Мониторим прогресс первого прохода
                duration = end - start
                progress_value = 0
                while True:
                    if progress.wasCanceled():
                        process.terminate()
                        return False
                        
                    line = process.stderr.readline()
                    if not line:
                        break
                        
                    if 'time=' in line:
                        try:
                            time_str = line.split('time=')[1].split()[0]
                            current_time = sum(float(x) * 60 ** i for i, x in enumerate(reversed(time_str.split(':'))))
                            progress_value = int(min(current_time / duration * 50, 49))  # Первый проход до 50%
                            progress.setValue(progress_value)
                        except:
                            pass
                
                if process.wait() != 0:
                    logging.error("Ошибка в первом проходе кодирования")
                    return False
                
                # Выполняем второй проход
                try:
                    process = subprocess.Popen(
                        [*second_pass, output_path],
                        stderr=subprocess.PIPE,
                        universal_newlines=True
                    )
                except FileNotFoundError:
                    logging.error("FFmpeg не найден при запуске второго прохода")
                    return False
                
                # Мониторим прогресс второго прохода
                while True:
                    if progress.wasCanceled():
                        process.terminate()
                        return False
                        
                    line = process.stderr.readline()
                    if not line:
                        break
                        
                    if 'time=' in line:
                        try:
                            time_str = line.split('time=')[1].split()[0]
                            current_time = sum(float(x) * 60 ** i for i, x in enumerate(reversed(time_str.split(':'))))
                            progress_value = 50 + int(min(current_time / duration * 50, 49))  # Второй проход от 50% до 100%
                            progress.setValue(progress_value)
                        except:
                            pass
                
                if process.wait() != 0:
                    logging.error("Ошибка во втором проходе кодирования")
                    return False
                
                # Проверяем результат
                if os.path.exists(output_path):
                    file_size = os.path.getsize(output_path)
                    input_size = os.path.getsize(self.input_path)
                    compression_ratio = (1 - file_size/input_size) * 100
                    
                    logging.info(
                        f"Видео успешно обработано:\n"
                        f"- Размер файла: {file_size / (1024*1024):.2f} MB\n"
                        f"- Сжатие: {compression_ratio:.1f}%\n"
                        f"- Длительность: {end - start:.1f} сек"
                    )
                    return True
                    
                return False
                
        except Exception as e:
            logging.error(f"Ошибка обрезки с кодированием: {e}")
            return False
        finally:
            # Очистка временных файлов FFmpeg
            patterns = ['*.log', '*.mbtree', '*.temp.*', '*.tmp']
            clean_dirs = [os.getcwd(), tempfile.gettempdir()]
            
            for directory in clean_dirs:
                for pattern in patterns:
                    try:
                        for file in glob.glob(os.path.join(directory, pattern)):
                            try:
                                os.remove(file)
                            except OSError as e:
                                logging.debug(f"Не удалось удалить временный файл {file}: {e}")
                    except Exception as e:
                        logging.debug(f"Ошибка при поиске временных файлов: {e}")
                        continue

class MainWindow(QMainWindow):
    """Главное окно приложения с оптимизированным управлением ресурсами."""
    
    # Константы для оптимизации
    CONFIG_FILE = 'config.json'
    CREDENTIALS_FILE = 'token.pickle'
    THEME_FILE = 'theme.txt'
    AHK_DATA_FILE = 'ahk_data.json'
    
    def __init__(self):
        super().__init__()
        # Инициализация базовых атрибутов
        self.creds = None
        self.video_path = None
        self.upload_thread = None
        self.drag_pos = None
        self.video_url = None
        self._temp_trim_files = []
        
        # Настройки по умолчанию
        self.channel = "не определен"
        self.theme = "Классическая"
        # default privacy setting for uploads: 'private', 'unlisted', 'public'
        self.default_privacy = 'private'
        self.ahk_data = {}
        # настройка: разрешать загрузку без FFmpeg (если True — не блокируем загрузку при отсутствии ffmpeg)
        self.allow_upload_without_ffmpeg = False
        # настройка: полностью отключить редактор
        self.disable_editor_completely = False
        # GitHub repo для проверки обновлений (можно переопределить в настройках)
        self.github_repo = GITHUB_REPO
        # Авто-проверка при старте (если False — автопроверка отключена, можно включить в настройках)
        self.auto_check_on_start = True
        
        # Настройка логирования (вызов модульной функции напрямую для надежности)
        try:
            setup_optimized_logging()
            self.logger = logging.getLogger(__name__)
        except Exception as e:
            logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
            logging.error(f"Ошибка настройки логирования в __init__: {e}")
            self.logger = logging.getLogger(__name__)
        
        # Кэш для оптимизации производительности
        self._widget_cache = {}
        
        self.setWindowTitle("Helper - YouTube Uploader")
        self.setFixedSize(900, 650)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        
        self.grad_bg = GradientWidget()
        self.setCentralWidget(self.grad_bg)
        
        layout = QVBoxLayout(self.grad_bg)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        
        layout.addWidget(self.mk_title())
        
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(20,15,20,15)
        
        self.pages = QStackedWidget()
        self.pages.setStyleSheet("background: transparent;")
        cl.addWidget(self.pages)
        
        self.pages.addWidget(self.mk_main())
        self.pages.addWidget(self.mk_upload_new())  # Используем новый метод
        self.pages.addWidget(self.mk_settings())
        self.pages.addWidget(self.mk_ahk())
        
        layout.addWidget(content)
        layout.addWidget(self.mk_nav())
        
        self.apply_theme()
        self.load_all()

        # Авто-проверка обновлений при старте приложения — если конфигурация содержит корректный репозиторий
        try:
            # запускаем в отдельном потоке, не блокируя UI, только если разрешено в настройках
            if getattr(self, 'auto_check_on_start', True):
                QTimer.singleShot(350, lambda: self._start_auto_update_check())
        except Exception:
            logging.exception('Не удалось запустить авто-проверку обновлений при старте')
    
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() < 60:
            self.drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
    
    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self.drag_pos:
            self.move(e.globalPosition().toPoint() - self.drag_pos)
            e.accept()
    
    def mouseReleaseEvent(self, e):
        self.drag_pos = None
    
    def closeEvent(self, e):
        """Корректное закрытие приложения с очисткой ресурсов."""
        try:
            # Останавливаем все фоновые процессы
            if self.upload_thread and self.upload_thread.isRunning():
                self.upload_thread.cancel()  # Отменяем загрузку
                self.upload_thread.quit()
                self.upload_thread.wait()
            
            # Очищаем временные файлы
            self._cleanup_temp_files()
            
            # Сохраняем настройки
            self._save_settings()
            
            # Очищаем кэш виджетов
            self._widget_cache.clear()
            
        except Exception as ex:
            logging.error(f"Ошибка при закрытии приложения: {ex}")
            
        finally:
            e.accept()
    
    def _cleanup_temp_files(self):
        """Очистка временных файлов с оптимизированной обработкой."""
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            for tf in self._temp_trim_files:
                if not os.path.exists(tf):
                    continue
                    
                def delete_file(path):
                    try:
                        with file_lock:  # Используем блокировку для безопасного удаления
                            if os.path.exists(path):
                                os.unlink(path)
                                return True
                    except Exception as e:
                        logging.debug(f"Ошибка удаления {path}: {e}")
                        return False
                
                futures.append(executor.submit(delete_file, tf))
        
        # Ожидаем завершения всех операций
        results = [f.result() for f in futures]
        deleted = sum(1 for r in results if r)
        
        if deleted:
            logging.info(f"Очищено {deleted} временных файлов")
        
        self._temp_trim_files.clear()
    
    @lru_cache(maxsize=32)
    def _read_settings_file(self, filepath):
        """Кэшированное чтение файлов настроек."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return None
    
    def _save_settings(self):
        """Сохранение настроек приложения с оптимизацией записи."""
        try:
            # Используем временные файлы для безопасной записи
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_theme, \
                 tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as temp_ahk, \
                 tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as temp_cfg:
                
                # Записываем данные во временные файлы
                temp_theme.write(self.theme)
                json.dump(self.ahk_data, temp_ahk, ensure_ascii=False, indent=2)
                
                # Закрываем файлы для безопасного перемещения
                temp_theme.close()
                temp_ahk.close()
                temp_cfg.close()
                
                # Атомарно перемещаем файлы
                with file_lock:
                    os.replace(temp_theme.name, self.THEME_FILE)
                    os.replace(temp_ahk.name, self.AHK_DATA_FILE)
                    # Сохраняем конфигурационный файл (добавляем оба флага)
                    cfg = {
                        'allow_upload_without_ffmpeg': bool(getattr(self, 'allow_upload_without_ffmpeg', False)),
                        'disable_editor_completely': bool(getattr(self, 'disable_editor_completely', False)),
                        'default_privacy': str(getattr(self, 'default_privacy', 'private')),
                        'github_repo': str(getattr(self, 'github_repo', GITHUB_REPO)),
                        'auto_check_on_start': bool(getattr(self, 'auto_check_on_start', True))
                    }
                    with open(temp_cfg.name, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    os.replace(temp_cfg.name, self.CONFIG_FILE)
            
            # Очищаем кэш настроек
            self._read_settings_file.cache_clear()
            
            logging.info("Настройки успешно сохранены")
            
        except Exception as e:
            logging.error(f"Ошибка при сохранении настроек: {e}")
            # Пытаемся очистить временные файлы в случае ошибки
            for temp_file in [temp_theme.name, temp_ahk.name, getattr(locals().get('temp_cfg'), 'name', None)]:
                try:
                    os.unlink(temp_file)
                except:
                    pass
    
    def mk_title(self):
        bar = QWidget()
        bar.setFixedHeight(60)
        bar.setStyleSheet("background: transparent;")
        l = QHBoxLayout(bar)
        l.setContentsMargins(20,10,20,10)
        
        t = QLabel("Helper")
        t.setStyleSheet("font-size: 28px; font-weight: bold; color: white;")
        l.addWidget(t)
        l.addStretch()
        
        for txt, func, clr in [("−", self.showMinimized, "rgba(100,150,255,0.5)"), 
                                ("✕", self.close, "rgba(255,80,80,0.6)")]:
            btn = QPushButton(txt)
            btn.setFixedSize(40,40)
            btn.clicked.connect(func)
            btn.setStyleSheet(f"QPushButton {{background-color: {clr}; border: none; border-radius: 20px; font-size: 24px; color: white; font-weight: bold;}} QPushButton:hover {{background-color: {clr.replace('0.5','0.7').replace('0.6','0.8')};}}")
            l.addWidget(btn)
        
        return bar
    
    def apply_theme(self):
        # ensure theme valid
        if self.theme not in THEMES:
            self.theme = "Классическая"
        t = THEMES[self.theme]
        self.grad_bg.set_gradient(t["s"], t["e"])
        
        # stop / remove effect if any
        if hasattr(self, 'effect_widget') and self.effect_widget:
            try:
                self.effect_widget.timer.stop()
            except: pass
            try:
                self.effect_widget.deleteLater()
            except: pass
            self.effect_widget = None

        # add overlay animations for special themes
        if self.theme == "Зима":
            self.effect_widget = SnowEffectWidget(self.grad_bg)
            self.effect_widget.setGeometry(0, 0, self.grad_bg.width(), self.grad_bg.height())
            # Make sure snow overlay is non-interactive and visible on top of content
            try:
                self.effect_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                self.grad_bg.installEventFilter(self)  # so we can resize overlay when bg resizes
                self.effect_widget.raise_()
                self.effect_widget.show()
            except Exception:
                # best-effort — proceed even if we can't set attributes
                try:
                    self.effect_widget.show()
                except:
                    pass
        else:
            self.effect_widget = None

        # global styles with accent, rounded cards (removed unsupported properties)
        self.setStyleSheet(f"""
            QLabel {{ color: white; font-size: 13px; }}
            QGroupBox {{ color: white; border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; margin-top: 10px; padding-top: 12px; }}
            QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 4px 12px; background-color: rgba(255,255,255,0.03); border-radius: 8px; margin-left: 8px; }}
            QLineEdit, QTextEdit, QComboBox, QDateEdit {{ background-color: {t['i']}; color: white; border: 2px solid {t['r']}; border-radius: 16px; padding: 10px; font-size: 13px; }}
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QDateEdit:focus {{ border: 2px solid {ACCENT}; background-color: rgba(255,255,255,0.06); }}
            QPushButton {{ background-color: {ACCENT}; color: white; border: none; border-radius: 16px; padding: 12px 18px; font-size: 14px; font-weight: bold; }}
            QPushButton[disabled="true"] {{ background-color: rgba(255,255,255,0.07); color: rgba(255,255,255,0.4); }}
            QPushButton.secondary {{ background-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.9); border: 1px solid rgba(255,255,255,0.06); }}
            QLineEdit[readOnly="true"] {{ background-color: rgba(255,255,255,0.03); }}
        """)
        
        if hasattr(self, 'left_frame'):
            # apply same card background to known frames including the right-side info panels
            for f in [self.left_frame, self.right_frame, self.upload_frame, getattr(self, 'upload_info_frame', None), self.settings_frame, self.ahk_frame, getattr(self, 'ahk_info_frame', None)]:
                try:
                    if f is None:
                        continue

                    # Distinguish upload panels and give them a larger radius so it is visually clear
                    is_upload_panel = (f is getattr(self, 'upload_frame', None)) or (f is getattr(self, 'upload_info_frame', None))
                    radius = 20 if is_upload_panel else 16
                    f.setStyleSheet(f"QFrame {{ background-color: {t['b']}; border: none; border-radius: {radius}px; padding: 10px; }}")

                    # Add subtle shadow for upload panels to enhance the rounded appearance
                    if is_upload_panel:
                        try:
                            # reuse existing effect if present
                            eff = getattr(f, '_theme_shadow', None)
                            if eff is None:
                                eff = QGraphicsDropShadowEffect(f)
                                eff.setBlurRadius(18)
                                eff.setOffset(0, 6)
                                eff.setColor(QColor(0, 0, 0, 140))
                                f.setGraphicsEffect(eff)
                                f._theme_shadow = eff
                            else:
                                eff.setColor(QColor(0, 0, 0, 140))
                                eff.setBlurRadius(18)
                                eff.setOffset(0, 6)
                        except Exception:
                            pass
                except Exception:
                    # avoid crashing theme application if frame not present yet
                    pass

        # Ensure the pages and all children repaint so theme changes are visible immediately
        try:
            if hasattr(self, 'pages') and isinstance(self.pages, QStackedWidget):
                for idx in range(self.pages.count()):
                    w = self.pages.widget(idx)
                    if w:
                        w.update()
                        for child in w.findChildren(QWidget):
                            child.update()
        except Exception:
            pass

    def eventFilter(self, obj, event):
        """Блокируем прокрутку колесиком для правой статичных панелей (upload_info_frame, ahk_info_frame).
        Также перехватываем Resize у grad_bg чтобы корректно изменять позицию/размер overlay эффекта (snow)."""
        try:
            # resize overlay when gradient background changes size
            try:
                if obj is getattr(self, 'grad_bg', None) and event.type() == QEvent.Type.Resize:
                    try:
                        if hasattr(self, 'effect_widget') and self.effect_widget:
                            self.effect_widget.setGeometry(0, 0, self.grad_bg.width(), self.grad_bg.height())
                            self.effect_widget.raise_()
                            self.effect_widget.update()
                    except Exception:
                        pass
            except Exception:
                pass
            if event.type() == QEvent.Type.Wheel:
                # если событие эмитится внутри правой панели — игнорируем прокрутку
                if isinstance(obj, QWidget):
                    if hasattr(self, 'upload_info_frame') and self.upload_info_frame is not None:
                        try:
                            if self.upload_info_frame.isAncestorOf(obj) or obj is self.upload_info_frame:
                                return True
                        except Exception:
                            pass
                            # Allow normal scrolling inside the AHK preview/info frame — users expect to scroll
                            # so we only block wheel events for upload_info_frame (legacy behaviour).
                            if hasattr(self, 'ahk_info_frame') and self.ahk_info_frame is not None:
                                try:
                                    # do NOT block wheel events for AHK preview
                                    pass
                                except Exception:
                                    pass
        except Exception:
            pass
        return super().eventFilter(obj, event)
    
    def mk_upload_new(self):
        p = QWidget()
        p.setStyleSheet("background: transparent;")
        l = QHBoxLayout(p)  # Горизонтальный layout для двух колонок
        l.setContentsMargins(0,0,0,0)
        l.setSpacing(15)  # Отступ между колонками как на главной
        
        # ======= ЛЕВАЯ КОЛОНКА =======
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("background: transparent; border: none;")
        
        left_container = QWidget()
        left_container.setStyleSheet("background: transparent;")
        left_scroll.setWidget(left_container)
        
        lcl = QVBoxLayout(left_container)
        lcl.setContentsMargins(0,0,0,0)
        
        self.upload_frame = QFrame()
        self.upload_frame.setObjectName('upload_frame')
        ul = QVBoxLayout(self.upload_frame)
        ul.setContentsMargins(25,20,25,20)
        ul.setSpacing(12)
        
        t = QLabel("Загрузка видео")
        t.setStyleSheet("font-size: 22px; font-weight: bold;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ul.addWidget(t)
        ul.addSpacing(8)
        
        vg = QGroupBox("Выбор видео файла")
        vl = QVBoxLayout(vg)
        vl.setSpacing(8)
        vl.setContentsMargins(12,15,12,12)
        vr = QHBoxLayout()
        self.vid_label = QLabel("Файл не выбран")
        self.vid_label.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 13px;")
        vr.addWidget(self.vid_label)
        vb = QPushButton("📁 Обзор")
        vb.clicked.connect(self.select_video)
        vb.setFixedWidth(120)
        vb.setProperty("class", "secondary")
        vr.addWidget(vb)
        vl.addLayout(vr)
        ul.addWidget(vg)
        
        self.name_input = self.mk_grp(ul, "Имя_Фамилия", "Введите имя и фамилию через _")
        self.link_input = self.mk_grp(ul, "Ссылка на жалобу", "Вставьте полную ссылку (http:// или https://)")
        
        dg = QGroupBox("Доп. описание")
        dl = QVBoxLayout(dg)
        dl.setContentsMargins(12,15,12,12)
        self.desc_input = QTextEdit()
        self.desc_input.setPlaceholderText("Можете добавить доп. информацию...")
        self.desc_input.setFixedHeight(70)
        dl.addWidget(self.desc_input)
        ul.addWidget(dg)
        
        ul.addSpacing(10)
        
        self.upload_btn = QPushButton("⬆ ЗАГРУЗИТЬ НА YOUTUBE")
        self.upload_btn.clicked.connect(self.start_upload)
        self.upload_btn.setEnabled(False)
        self.upload_btn.setStyleSheet("QPushButton {padding: 16px; font-size: 15px; border-radius: 12px;}")
        # Политика доступа для текущей загрузки (по умолчанию берем глобальную настройку)
        try:
            self.upload_privacy_combo = QComboBox()
            self.upload_privacy_combo.addItem('Только я', 'private')
            self.upload_privacy_combo.addItem('По ссылке', 'unlisted')
            self.upload_privacy_combo.addItem('Публичный', 'public')
            # default value from settings
            try:
                desired = getattr(self, 'default_privacy', 'private')
                for i in range(self.upload_privacy_combo.count()):
                    if self.upload_privacy_combo.itemData(i) == desired:
                        self.upload_privacy_combo.setCurrentIndex(i)
                        break
            except Exception:
                pass
            self.upload_privacy_combo.setFixedWidth(140)
            self.upload_privacy_combo.setStyleSheet('QComboBox { border-radius: 8px; background: rgba(255,255,255,0.04); padding: 6px; }')
            ul.addWidget(self.upload_privacy_combo)
        except Exception:
            pass
        ul.addWidget(self.upload_btn)
        
        ul.addStretch()
        lcl.addWidget(self.upload_frame)
        
        # ======= ПРАВАЯ КОЛОНКА =======
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet("background: transparent; border: none;")
        # allow normal scrollbars for this right panel (behave like in a browser)
        try:
            # show scrollbars when content needs it (as in a typical browser)
            right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            # do NOT install the global event filter here so wheel events work normally
        except Exception:
            pass
        
        right_container = QWidget()
        right_container.setStyleSheet("background: transparent;")
        right_scroll.setWidget(right_container)
        
        rcl = QVBoxLayout(right_container)
        rcl.setContentsMargins(0,0,0,0)
        
        self.upload_info_frame = QFrame()
        self.upload_info_frame.setObjectName('upload_info_frame')
        # временный стиль до применения темы (серый фон справа)
        try:
            self.upload_info_frame.setStyleSheet("QFrame { background-color: rgba(30,30,45,0.85); border-radius: 16px; padding: 10px; }")
        except Exception:
            pass
        il = QVBoxLayout(self.upload_info_frame)
        il.setContentsMargins(25,20,25,20)
        il.setSpacing(12)
        
        it = QLabel("Статус загрузки")
        it.setStyleSheet("font-size: 22px; font-weight: bold;")
        it.setAlignment(Qt.AlignmentFlag.AlignCenter)
        il.addWidget(it)
        il.addSpacing(8)
        
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 13px; padding: 8px;")
        il.addWidget(self.status_label)

        # Индикатор состояния FFmpeg / редактора (видно пользователю)
        self.editor_indicator_label = QLabel("")
        self.editor_indicator_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.editor_indicator_label.setWordWrap(True)
        self.editor_indicator_label.setStyleSheet("font-size:12px; color: rgba(255,255,255,0.8); padding:4px;")
        il.addWidget(self.editor_indicator_label)
        
        self.link_container = QWidget()
        lcl2 = QVBoxLayout(self.link_container)  # Переименовал lcl в lcl2
        lcl2.setSpacing(6)
        lcl2.setContentsMargins(0,0,0,0)
        lt = QLabel("Ссылка на видео:")
        lt.setStyleSheet("font-size: 14px; font-weight: bold;")
        lcl2.addWidget(lt)
        lr = QHBoxLayout()
        self.vid_link = QLineEdit()
        self.vid_link.setReadOnly(True)
        self.vid_link.setStyleSheet("background-color: rgba(255,255,255,0.03); font-size: 12px; border-radius: 8px;")
        lr.addWidget(self.vid_link)
        cb = QPushButton("📋")
        cb.clicked.connect(self.copy_link)
        cb.setFixedWidth(55)
        cb.setProperty("class", "secondary")
        lr.addWidget(cb)
        lcl2.addLayout(lr)
        self.link_container.setVisible(False)
        il.addWidget(self.link_container)
        
        inst = QLabel("📋 Как загрузить видео:\ВАЖНО\nВ настройках Вы можете выбрать тип приватности\n(Только я, достоп по ссылке\n\n1. Выберите видео файл\n2. Введите имя и фамилию\n3. Вставьте ссылку на жалобу\n4. При желании добавьте описание\n5. Нажмите кнопку загрузки\n\n⚡ После загрузки вы получите\nссылку на видео")
        inst.setWordWrap(True)
        inst.setStyleSheet("font-size: 13px;")
        il.addWidget(inst)

        # История загруженных видео
        try:
            hist_box = QGroupBox("История загрузок")
            hist_l = QVBoxLayout(hist_box)
            hist_l.setContentsMargins(8,8,8,8)
            hist_l.setSpacing(6)

            from PyQt6.QtWidgets import QListWidget, QListWidgetItem
            self.upload_history_list = QListWidget()
            self.upload_history_list.setFixedHeight(180)
            self.upload_history_list.itemDoubleClicked.connect(lambda it: webbrowser.open(it.data(Qt.ItemDataRole.UserRole)))
            hist_l.addWidget(self.upload_history_list)

            btns = QWidget()
            bl = QHBoxLayout(btns)
            bl.setContentsMargins(0,0,0,0)
            bl.setSpacing(6)
            open_btn = QPushButton('Открыть')
            open_btn.clicked.connect(lambda: self._open_selected_history())
            copy_btn = QPushButton('Копировать')
            copy_btn.clicked.connect(lambda: self._copy_selected_history())
            clear_btn = QPushButton('Очистить')
            clear_btn.clicked.connect(lambda: self._clear_history())
            for b in (open_btn, copy_btn, clear_btn):
                b.setFixedHeight(28)
                b.setStyleSheet('QPushButton{ padding:6px; border-radius:6px; }')
                bl.addWidget(b)

            hist_l.addWidget(btns)
            il.addWidget(hist_box)
            try:
                self._refresh_history_ui()
            except Exception:
                pass
        except Exception:
            pass

        # Добавляем кнопку редактирования видео
        edit_btn_container = QWidget()
        edit_layout = QVBoxLayout(edit_btn_container)
        edit_layout.setContentsMargins(0, 10, 0, 10)
        
        self.edit_btn = QPushButton("✂️ Редактировать видео")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self.open_video_editor)
        self.edit_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(33,150,243,0.3);
                border: 2px solid rgba(33,150,243,0.5);
                border-radius: 8px;
                padding: 12px 15px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(33,150,243,0.4);
            }
            QPushButton[enabled="false"] {
                background-color: rgba(255,255,255,0.1);
                border-color: rgba(255,255,255,0.2);
            }
        """)
        edit_layout.addWidget(self.edit_btn)
        il.addWidget(edit_btn_container)
        
        il.addStretch()
        rcl.addWidget(self.upload_info_frame)
        
        # Добавляем обе колонки в основной layout
        l.addWidget(left_scroll)
        l.addWidget(right_scroll)
        
        return p

    def mk_ahk(self):
        p = QWidget()
        p.setStyleSheet("background: transparent;")
        l = QHBoxLayout(p)  # Горизонтальный layout для двух колонок
        l.setContentsMargins(0,0,0,0)
        l.setSpacing(15)  # Отступ между колонками как на главной
        
        # ======= ЛЕВАЯ КОЛОНКА =======
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("background: transparent; border: none;")
        
        left_container = QWidget()
        left_container.setStyleSheet("background: transparent;")
        left_scroll.setWidget(left_container)
        
        lcl = QVBoxLayout(left_container)
        lcl.setContentsMargins(0,0,0,0)
        
        self.ahk_frame = QFrame()
        al = QVBoxLayout(self.ahk_frame)
        al.setContentsMargins(25,20,25,20)
        al.setSpacing(12)
        
        t = QLabel("Быстрые AHK")
        t.setStyleSheet("font-size: 22px; font-weight: bold;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        al.addWidget(t)
        al.addSpacing(8)
        
        self.ahk_nick = self.mk_fld(al, "Никнейм", "Формата Имя_Фамилия")
        self.ahk_level = self.mk_fld(al, "Уровень администрирования", "Ваш уровень администрирования (например, '1/2/3/4/5')")
        
        sg = QGroupBox("Неактив/отпуск")
        sgl = QVBoxLayout(sg)
        sgl.setContentsMargins(12,15,12,12)
        self.ahk_status = QComboBox()
        self.ahk_status.addItems(["Неактив", "Отпуск"])
        self.ahk_status.currentTextChanged.connect(self.save_ahk)
        sgl.addWidget(self.ahk_status)
        al.addWidget(sg)
        
        rg = QGroupBox("Причина отсутствия")
        rgl = QVBoxLayout(rg)
        rgl.setContentsMargins(12,15,12,12)
        self.ahk_reason = QTextEdit()
        self.ahk_reason.setPlaceholderText("Опишите причину...")
        self.ahk_reason.setFixedHeight(70)
        self.ahk_reason.textChanged.connect(self.save_ahk)
        rgl.addWidget(self.ahk_reason)
        al.addWidget(rg)
        
        dg = QGroupBox("Период отсутствия")
        dgl = QHBoxLayout(dg)
        dgl.setSpacing(12)
        dgl.setContentsMargins(12,15,12,12)
        
        for lbl_txt, attr in [("С:", "date_from"), ("До:", "date_to")]:
            w = QWidget()
            wl = QVBoxLayout(w)
            wl.setSpacing(6)
            wl.setContentsMargins(0,0,0,0)
            lbl = QLabel(lbl_txt)
            lbl.setStyleSheet("font-size: 13px;")
            wl.addWidget(lbl)
            de = QDateEdit()
            de.setCalendarPopup(True)
            de.setDate(QDate.currentDate())
            de.setDisplayFormat("dd.MM.yyyy")
            de.dateChanged.connect(self.save_ahk)
            wl.addWidget(de)
            setattr(self, f"ahk_{attr}", de)
            dgl.addWidget(w)
        
        al.addWidget(dg)
        al.addSpacing(10)
        
        cpb = QPushButton("📋 СКОПИРОВАТЬ ТЕКСТ")
        cpb.clicked.connect(self.copy_ahk)
        cpb.setStyleSheet("QPushButton {padding: 16px; font-size: 15px; background-color: rgba(76,175,80,0.4); border: 2px solid rgba(76,175,80,0.6); border-radius: 12px;} QPushButton:hover {background-color: rgba(76,175,80,0.5);}")
        al.addWidget(cpb)
        
        al.addStretch()
        lcl.addWidget(self.ahk_frame)
        
        # ======= ПРАВАЯ КОЛОНКА =======
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet("background: transparent; border: none;")
        # allow scrollbars for the right panel so long text can be scrolled and viewed
        try:
            right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        except Exception:
            pass
        
        right_container = QWidget()
        right_container.setStyleSheet("background: transparent;")
        right_scroll.setWidget(right_container)
        
        rcl = QVBoxLayout(right_container)
        rcl.setContentsMargins(0,0,0,0)
        
        self.ahk_info_frame = QFrame()
        # временный стиль до применения темы (серый фон справа)
        try:
            self.ahk_info_frame.setStyleSheet("QFrame { background-color: rgba(30,30,45,0.85); border-radius: 16px; padding: 10px; }")
        except Exception:
            pass
        il = QVBoxLayout(self.ahk_info_frame)
        il.setContentsMargins(25,20,25,20)
        il.setSpacing(12)
        
        it = QLabel("Предпросмотр")
        it.setStyleSheet("font-size: 22px; font-weight: bold;")
        it.setAlignment(Qt.AlignmentFlag.AlignCenter)
        il.addWidget(it)
        il.addSpacing(8)
        
        fb = QPushButton("🔗 Открыть форум")
        fb.clicked.connect(lambda: webbrowser.open("https://forum.amazing-online.com/threads/neaktivy-administracii-yellow-servera.1040009/"))
        fb.setStyleSheet("QPushButton {padding: 15px; font-size: 14px; background-color: rgba(33,150,243,0.4); border: 2px solid rgba(33,150,243,0.6); border-radius: 12px;} QPushButton:hover {background-color: rgba(33,150,243,0.5);}")
        il.addWidget(fb)
        il.addSpacing(10)
        
        pg = QGroupBox("Текст для форума")
        pgl = QVBoxLayout(pg)
        pgl.setContentsMargins(12,15,12,12)
        self.ahk_preview = QTextEdit()
        self.ahk_preview.setReadOnly(True)
        # make preview taller so initial text is visible and the textedit provides its own scrollbar
        self.ahk_preview.setFixedHeight(180)
        self.ahk_preview.setStyleSheet("background-color: rgba(255,255,255,0.05); font-size: 12px; border-radius: 8px;")
        pgl.addWidget(self.ahk_preview)
        il.addWidget(pg)
        
        inst = QLabel("📋 Как использовать:\n\n1. Заполните все поля слева\n2. Проверьте текст в предпросмотре\n3. Нажмите кнопку копирования\n4. Вставьте на форуме\n\n⚡ Форма сохраняет данные\nавтоматически")
        inst.setWordWrap(True)
        inst.setStyleSheet("font-size: 13px;")
        il.addWidget(inst)
        
        il.addStretch()
        rcl.addWidget(self.ahk_info_frame)
        
        # Добавляем обе колонки в основной layout
        l.addWidget(left_scroll)
        l.addWidget(right_scroll)
        
        return p

    def mk_main(self):
        p = QWidget()
        p.setStyleSheet("background: transparent;")
        l = QHBoxLayout(p)
        l.setContentsMargins(0,0,0,0)
        l.setSpacing(15)
        
        self.left_frame = QFrame()
        ll = QVBoxLayout(self.left_frame)
        ll.setContentsMargins(25,20,25,20)
        ll.setSpacing(10)
        
        t = QLabel("Информация")
        t.setStyleSheet("font-size: 22px; font-weight: bold;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ll.addWidget(t)
        ll.addSpacing(8)
        
        # ---- Строка: Статус авторизации — метка + маленькая `pill`-метка (как в примере)
        auth_row = QWidget()
        # make the whole row a light pill-like background so label+value sit inside a single light field
        auth_row.setStyleSheet("background-color: rgba(255,255,255,0.06); border-radius: 10px; padding: 6px; border: 1px solid rgba(255,255,255,0.12);")
        auth_row_layout = QHBoxLayout(auth_row)
        auth_row_layout.setContentsMargins(10, 6, 10, 6)
        auth_row_layout.setSpacing(12)

        auth_label = QLabel("Статус авторизации:")
        auth_label.setStyleSheet("font-size: 13px; background: transparent; border: none; padding: 0px; margin: 0px;")
        auth_label.setAutoFillBackground(False)
        try:
            auth_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        except Exception:
            pass
        auth_row_layout.addWidget(auth_label)

        self.auth_status = QLabel("не авторизован")
        self.auth_status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.set_auth_pill("не авторизован", "#FF6B6B")
        try:
            self.auth_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        auth_row_layout.addWidget(self.auth_status, 1)

        auth_row_layout.addStretch()
        
        # ---- Единая закруглённая строка: YouTube аккаунт ----
        yt_row = QWidget()
        # light background for the whole youtube row so the value appears inside the same field
        yt_row.setStyleSheet("background-color: rgba(255,255,255,0.06); border-radius: 10px; padding: 6px; border: 1px solid rgba(255,255,255,0.12);")
        yt_row_layout = QHBoxLayout(yt_row)
        yt_row_layout.setContentsMargins(10, 6, 10, 6)
        yt_row_layout.setSpacing(12)

        yt_label = QLabel("YouTube аккаунт:")
        yt_label.setStyleSheet("font-size: 13px; background: transparent; border: none; padding: 0px; margin: 0px;")
        yt_label.setAutoFillBackground(False)
        try:
            yt_label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        except Exception:
            pass
        yt_row_layout.addWidget(yt_label)

        self.nick_label = QLabel(self.channel)
        self.nick_label.setWordWrap(True)
        self.nick_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.set_nick_pill(self.channel, "#FFFFFF")
        yt_row_layout.addWidget(self.nick_label, 1)

        yt_row_layout.addStretch()

        # Добавляем итоговые строки (auth и yt) — компактные строки с pill-метками
        ll.addWidget(auth_row)
        ll.addSpacing(10)
        ll.addWidget(yt_row)
        ll.addSpacing(14)
        ll.addSpacing(10)
        
        self.auth_btn = QPushButton("🔐 Авторизоваться")
        self.auth_btn.clicked.connect(self.auth)
        self.auth_btn.setStyleSheet("QPushButton {background-color: rgba(76,175,80,0.3); border: 2px solid rgba(76,175,80,0.5); padding: 14px; font-size: 14px; border-radius: 12px;} QPushButton:hover {background-color: rgba(76,175,80,0.4);}")
        ll.addWidget(self.auth_btn)
        ll.addStretch()
        
        self.right_frame = QFrame()
        rl = QVBoxLayout(self.right_frame)
        rl.setContentsMargins(25,20,25,20)
        rl.setSpacing(10)
        
        it = QLabel("Инструкция")
        it.setStyleSheet("font-size: 22px; font-weight: bold;")
        it.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rl.addWidget(it)
        rl.addSpacing(8)
        
        inst = QLabel("📋 Как использовать:\n1. Авторизуйтесь в YouTube\n2. Перейдите в 'Загрузка'\n3. Выберите видео файл\n4. Заполните данные\n5. Загрузите видео\n\nИспользуйте 'Быстрые AHK'\nвзятия быстрого неактив/отпуска")
        inst.setWordWrap(True)
        inst.setStyleSheet("font-size: 13px;")
        rl.addWidget(inst)
        rl.addStretch()
        
        l.addWidget(self.left_frame)
        l.addWidget(self.right_frame)
        return p
    
    def mk_upload_old(self):
        # This method is deprecated and replaced by mk_upload_new
        pass
    
    def mk_settings(self):
        p = QWidget()
        p.setStyleSheet("background: transparent;")
        l = QVBoxLayout(p)
        l.setContentsMargins(0,0,0,0)
        
        self.settings_frame = QFrame()
        sl = QVBoxLayout(self.settings_frame)
        sl.setContentsMargins(35,25,35,25)
        sl.setSpacing(15)
        
        t = QLabel("Настройки приложения")
        t.setStyleSheet("font-size: 24px; font-weight: bold;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(t)
        sl.addSpacing(10)
        
        tg = QGroupBox("Тема оформления")
        tl = QVBoxLayout(tg)
        tl.setSpacing(10)
        tl.setContentsMargins(10,12,10,10)
        
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(THEMES.keys())
        self.theme_combo.setCurrentText(self.theme)
        self.theme_combo.currentTextChanged.connect(self.change_theme)
        tl.addWidget(self.theme_combo)
        # Настройка: разрешить загрузку без FFmpeg (пропуск редактора/проверки)
        try:
            # создаём строку: switch (QCheckBox без текста) слева и подпись справа
            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(8)

            self.allow_upload_checkbox = QCheckBox()
            # стиль-таблица: индикатор — svg-картинки (трек + круглая ручка), чтобы визуально было похоже на toggle
            self.allow_upload_checkbox.setStyleSheet(
                "QCheckBox::indicator { width: 40px; height: 20px; }"
                "QCheckBox::indicator:unchecked { image: url('data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"40\" height=\"20\"><rect rx=\"10\" ry=\"10\" width=\"40\" height=\"20\" fill=\"%23bdbdbd\"/><circle cx=\"10\" cy=\"10\" r=\"7\" fill=\"%23ffffff\"/></svg>'); }"
                "QCheckBox::indicator:checked { image: url('data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"40\" height=\"20\"><rect rx=\"10\" ry=\"10\" width=\"40\" height=\"20\" fill=\"%2351CF66\"/><circle cx=\"30\" cy=\"10\" r=\"7\" fill=\"%23ffffff\"/></svg>'); }"
            )
            self.allow_upload_checkbox.setChecked(self.allow_upload_without_ffmpeg)
            self.allow_upload_checkbox.toggled.connect(self.on_toggle_allow_upload_without_ffmpeg)

            lbl = QLabel("Разрешить загрузку без FFmpeg")
            # белый текст и скруглённая обводка
            lbl.setStyleSheet("color: white; font-size: 13px; padding: 6px; border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;")
            # кликабельная метка — переключаем чекбокс при клике
            lbl.mousePressEvent = lambda e, cb=self.allow_upload_checkbox: cb.toggle()

            row_l.addWidget(self.allow_upload_checkbox, 0)
            row_l.addWidget(lbl, 1)
            info_lbl = QLabel("ℹ️")
            info_lbl.setToolTip("Если включено, загрузка будет разрешена даже если FFmpeg недоступен — редактор и проверки будут пропущены.")
            info_lbl.setStyleSheet("color: rgba(255,255,255,0.9); font-size: 12px; padding: 4px; border-radius: 6px; background: rgba(255,255,255,0.02);")
            row_l.addWidget(info_lbl, 0)
            tl.addWidget(row)
            # Кнопка для ручной проверки обновлений (демо) — показывает сплэш с текущей темой
            try:
                check_update_btn = QPushButton("Проверить обновления (сплэш)")
                check_update_btn.setStyleSheet("QPushButton { padding: 10px; border-radius: 10px; background: rgba(255,255,255,0.06); }")
                check_update_btn.clicked.connect(lambda: getattr(self, 'show_update_check', lambda: None)())
                tl.addWidget(check_update_btn)
                # Опция: авто-проверка при старте
                try:
                    auto_row = QWidget()
                    ar_l = QHBoxLayout(auto_row)
                    ar_l.setContentsMargins(0,0,0,0)
                    ar_l.setSpacing(8)

                    self.auto_check_checkbox = QCheckBox()
                    self.auto_check_checkbox.setChecked(getattr(self, 'auto_check_on_start', True))
                    self.auto_check_checkbox.toggled.connect(lambda v: self.on_toggle_auto_check(v))

                    lbl_auto = QLabel('Авто-проверять обновления при старте')
                    lbl_auto.setStyleSheet('color: white; font-size: 13px; padding: 6px; border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;')
                    lbl_auto.mousePressEvent = lambda e, cb=self.auto_check_checkbox: cb.toggle()

                    ar_l.addWidget(self.auto_check_checkbox, 0)
                    ar_l.addWidget(lbl_auto, 1)
                    tl.addWidget(auto_row)
                except Exception:
                    pass
                # (Оставляем единственную кнопку проверки) — ввод репозитория убран (используется GITHUB_REPO или config.json)
                # (default upload privacy selection removed from Settings UI; per-upload selection remains on Upload page)
            except Exception:
                pass
        except Exception:
            # защита если виджеты ещё не готовы
            pass
        # Настройка: полностью отключить редактор (удаляет возможность открытия редактора)
        try:
            row2 = QWidget()
            row2_l = QHBoxLayout(row2)
            row2_l.setContentsMargins(0,0,0,0)
            row2_l.setSpacing(8)

            self.disable_editor_checkbox = QCheckBox()
            self.disable_editor_checkbox.setStyleSheet(
                "QCheckBox::indicator { width: 40px; height: 20px; }"
                "QCheckBox::indicator:unchecked { image: url('data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"40\" height=\"20\"><rect rx=\"10\" ry=\"10\" width=\"40\" height=\"20\" fill=\"%23bdbdbd\"/><circle cx=\"10\" cy=\"10\" r=\"7\" fill=\"%23ffffff\"/></svg>'); }"
                "QCheckBox::indicator:checked { image: url('data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"40\" height=\"20\"><rect rx=\"10\" ry=\"10\" width=\"40\" height=\"20\" fill=\"%23FF6B6B\"/><circle cx=\"30\" cy=\"10\" r=\"7\" fill=\"%23ffffff\"/></svg>'); }"
            )
            self.disable_editor_checkbox.setChecked(getattr(self, 'disable_editor_completely', False))
            self.disable_editor_checkbox.toggled.connect(self.on_toggle_disable_editor)

            lbl2 = QLabel("Отключить редактор полностью")
            lbl2.setStyleSheet("color: white; font-size: 13px; padding: 6px; border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;")
            lbl2.mousePressEvent = lambda e, cb=self.disable_editor_checkbox: cb.toggle()

            row2_l.addWidget(self.disable_editor_checkbox, 0)
            row2_l.addWidget(lbl2, 1)
            info2 = QLabel("ℹ️")
            info2.setToolTip("Если включено — кнопка редактирования видео будет скрыта и редактор нельзя открыть.")
            info2.setStyleSheet("color: rgba(255,255,255,0.9); font-size: 12px; padding: 4px; border-radius: 6px; background: rgba(255,255,255,0.02);")
            row2_l.addWidget(info2, 0)
            tl.addWidget(row2)
        except Exception:
            pass
        # Сделаем две визуальные карточки (без скролла): слева — настройки, справа — краткая помощь
        cards = QWidget()
        cards_l = QHBoxLayout(cards)
        cards_l.setContentsMargins(0,0,0,0)
        cards_l.setSpacing(18)

        # Левая карточка — основной блок настроек (скрываем лишние отступы внутри)
        left_card = QFrame()
        left_card.setObjectName('settings_left_card')
        left_card.setStyleSheet('QFrame{ border-radius:18px; padding:18px; }')
        left_v = QVBoxLayout(left_card)
        left_v.setContentsMargins(6,6,6,6)
        left_v.setSpacing(10)

        # Переносим туда группу темы и опции
        left_v.addWidget(tg)

        # Добавим основные флаги кратко
        try:
            chk_row = QWidget()
            chk_l = QVBoxLayout(chk_row)
            chk_l.setContentsMargins(0,0,0,0)
            chk_l.setSpacing(8)
            # reuse the existing check rows (they were added into tg via tl earlier)
            left_v.addStretch()
        except Exception:
            pass

        # Правая карточка — короткая справка и дополнительные опции (короче текст, без прокрутки)
        right_card = QFrame()
        right_card.setObjectName('settings_right_card')
        right_card.setStyleSheet('QFrame{ border-radius:18px; padding:18px; }')
        right_v = QVBoxLayout(right_card)
        right_v.setContentsMargins(6,6,6,6)
        right_v.setSpacing(10)


        adv = QGroupBox('Темы')
        advl = QVBoxLayout(adv)
        advl.setContentsMargins(8,8,8,8)
        advl.addWidget(QLabel('Доступные темы: соси хуй твар ебаная маму ебал твою \n\n• Классическая - фиолетово-синий\n• Темная - серые тона (Dark Mode)\n• Океан - морские оттенки\n• Закат - розово-красный\n• Лес - зеленые тона\n• Фиолетовый сон - фиолетовый\n• Зима - снежинки и морозная анимация'))
        right_v.addWidget(adv)
        right_v.addStretch()

        cards_l.addWidget(left_card, 2)
        cards_l.addWidget(right_card, 1)

        sl.addWidget(cards)
        
        l.addWidget(self.settings_frame)
        return p
    
    def mk_upload(self):
        p = QWidget()
        p.setStyleSheet("background: transparent;")
        l = QHBoxLayout(p)  # Горизонтальный layout для двух колонок
        l.setContentsMargins(0,0,0,0)
        l.setSpacing(15)  # Отступ между колонками
        
        # Левая колонка
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("background: transparent; border: none;")
        
        c = QWidget()
        c.setStyleSheet("background: transparent;")
        left_scroll.setWidget(c)
        
        cl = QVBoxLayout(c)
        cl.setContentsMargins(0,0,0,0)
        
        self.ahk_frame = QFrame()
        al = QVBoxLayout(self.ahk_frame)
        al.setContentsMargins(35,25,35,25)
        al.setSpacing(12)
        
        t = QLabel("Быстрые AHK формы")
        t.setStyleSheet("font-size: 26px; font-weight: bold;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        al.addWidget(t)
        al.addSpacing(10)
        
        fb = QPushButton("🔗 Открыть форум")
        fb.clicked.connect(lambda: webbrowser.open("https://forum.amazing-online.com/threads/neaktivy-administracii-yellow-servera.1040009/"))
        fb.setStyleSheet("QPushButton {padding: 15px; font-size: 14px; background-color: rgba(33,150,243,0.4); border: 2px solid rgba(33,150,243,0.6); border-radius: 12px;} QPushButton:hover {background-color: rgba(33,150,243,0.5);}")
        al.addWidget(fb)
        al.addSpacing(10)
        
        self.ahk_nick = self.mk_fld(al, "Никнейм", "Введите игровой никнейм")
        self.ahk_level = self.mk_fld(al, "Уровень администрирования", "Helper, Moderator, Admin")
        
        sg = QGroupBox("Неактив/отпуск")
        sgl = QVBoxLayout(sg)
        sgl.setContentsMargins(12,15,12,12)
        self.ahk_status = QComboBox()
        self.ahk_status.addItems(["Неактив", "Отпуск"])
        self.ahk_status.currentTextChanged.connect(self.save_ahk)
        sgl.addWidget(self.ahk_status)
        al.addWidget(sg)
        
        rg = QGroupBox("Причина отсутствия")
        rgl = QVBoxLayout(rg)
        rgl.setContentsMargins(12,15,12,12)
        self.ahk_reason = QTextEdit()
        self.ahk_reason.setPlaceholderText("Опишите причину...")
        self.ahk_reason.setFixedHeight(70)
        self.ahk_reason.textChanged.connect(self.save_ahk)
        rgl.addWidget(self.ahk_reason)
        al.addWidget(rg)
        
        dg = QGroupBox("Период отсутствия")
        dgl = QHBoxLayout(dg)
        dgl.setSpacing(12)
        dgl.setContentsMargins(12,15,12,12)
        
        for lbl_txt, attr in [("С:", "date_from"), ("До:", "date_to")]:
            w = QWidget()
            wl = QVBoxLayout(w)
            wl.setSpacing(6)
            wl.setContentsMargins(0,0,0,0)
            lbl = QLabel(lbl_txt)
            lbl.setStyleSheet("font-size: 13px;")
            wl.addWidget(lbl)
            de = QDateEdit()
            de.setCalendarPopup(True)
            de.setDate(QDate.currentDate())
            de.setDisplayFormat("dd.MM.yyyy")
            de.dateChanged.connect(self.save_ahk)
            wl.addWidget(de)
            setattr(self, f"ahk_{attr}", de)
            dgl.addWidget(w)
        
        al.addWidget(dg)
        al.addSpacing(10)
        
        cpb = QPushButton("📋 СКОПИРОВАТЬ ТЕКСТ")
        cpb.clicked.connect(self.copy_ahk)
        cpb.setStyleSheet("QPushButton {padding: 16px; font-size: 15px; background-color: rgba(76,175,80,0.4); border: 2px solid rgba(76,175,80,0.6); border-radius: 12px;} QPushButton:hover {background-color: rgba(76,175,80,0.5);}")
        al.addWidget(cpb)
        
        pg = QGroupBox("Предпросмотр")
        pgl = QVBoxLayout(pg)
        pgl.setContentsMargins(12,15,12,12)
        self.ahk_preview = QTextEdit()
        self.ahk_preview.setReadOnly(True)
        self.ahk_preview.setFixedHeight(100)
        self.ahk_preview.setStyleSheet("background-color: rgba(255,255,255,0.05); font-size: 12px; border-radius: 8px;")
        pgl.addWidget(self.ahk_preview)
        al.addWidget(pg)
        
        al.addStretch()
        cl.addWidget(self.ahk_frame)
        l.addWidget(left_scroll)
        return p
    
    def mk_grp(self, layout, title, placeholder):
        g = QGroupBox(title)
        gl = QVBoxLayout(g)
        gl.setContentsMargins(12,15,12,12)
        f = QLineEdit()
        f.setPlaceholderText(placeholder)
        f.setStyleSheet("border-radius: 12px;")
        gl.addWidget(f)
        layout.addWidget(g)
        return f
    
    def mk_fld(self, layout, title, placeholder):
        g = QGroupBox(title)
        gl = QVBoxLayout(g)
        gl.setContentsMargins(12,15,12,12)
        f = QLineEdit()
        f.setPlaceholderText(placeholder)
        f.textChanged.connect(self.save_ahk)
        f.setStyleSheet("border-radius: 12px;")
        gl.addWidget(f)
        layout.addWidget(g)
        return f
    
    def mk_nav(self):
        n = QFrame()
        n.setFixedHeight(80)
        n.setStyleSheet("background-color: rgba(20,20,30,0.85); border-top-left-radius: 12px; border-top-right-radius: 12px;")
        
        l = QHBoxLayout(n)
        l.setContentsMargins(15,10,15,10)
        l.setSpacing(10)
        
        self.nav_btns = []
        icons = ["🏠","⬆️","⚙️","⚡"]
        labels = ["Главная","Загрузка","Настройки","Быстрые AHK"]
        for i, (ic, txt) in enumerate(zip(icons, labels)):
            b = QPushButton(f"{ic} {txt}")
            b.clicked.connect(lambda checked, i=i: self.switch_page(i))
            b.setProperty("tabIndex", i)
            b.setStyleSheet("QPushButton { padding: 14px 12px; font-size: 13px; background-color: rgba(255,255,255,0.06); border: none; border-radius: 12px; color: rgba(255,255,255,0.9); } QPushButton:hover { background-color: rgba(255,255,255,0.09); }")
            l.addWidget(b, 1)
            self.nav_btns.append(b)
        
        self.set_active_nav(0)
        return n

    def switch_page(self, idx):
        self.pages.setCurrentIndex(idx)
        self.set_active_nav(idx)
    
    def set_active_nav(self, idx):
        for i, b in enumerate(self.nav_btns):
            if i == idx:
                b.setStyleSheet(f"QPushButton {{ padding: 14px 12px; font-size: 13px; background-color: {ACCENT}; color: white; border-radius: 12px; border: 1px solid rgba(255,255,255,0.08); }} QPushButton:hover {{ background-color: {ACCENT}; }}")
            else:
                b.setStyleSheet("QPushButton { padding: 14px 12px; font-size: 13px; background-color: rgba(255,255,255,0.03); color: rgba(255,255,255,0.9); border-radius: 12px; } QPushButton:hover { background-color: rgba(255,255,255,0.06); }")

    def load_all(self):
        """Centralized loader called during startup."""
        try:
            # загружаем конфигурацию приложения (если есть)
            try:
                self.load_config()
            except Exception:
                pass

            self.load_creds()
        except Exception:
            pass
        try:
            self.load_ahk()
        except Exception:
            pass
        # load upload history
        try:
            self._load_upload_history()
        except Exception:
            pass

    # ---- helper для pill-стилей статусов ----
    def _pill_style(self, fg="#FFFFFF"):
        # тёмный фон + тонкая обводка — похож на стиль в скрине №2
        return (
            f"background-color: rgba(20,20,30,0.85); color: {fg}; "
            "padding: 6px 10px; border-radius: 10px; font-weight: bold; font-size: 14px; "
            "border: 1px solid rgba(255,255,255,0.06);"
        )

    def set_auth_pill(self, text, fg="#FF6B6B"):
        try:
            self.auth_status.setText(text)
            self.auth_status.setStyleSheet(self._pill_style(fg))
        except Exception:
            # graceful fallback
            self.auth_status.setText(text)

    def set_nick_pill(self, text, fg="#FFFFFF"):
        try:
            self.nick_label.setText(text)
            self.nick_label.setStyleSheet(self._pill_style(fg))
        except Exception:
            self.nick_label.setText(text)
        try:
            self.load_theme()
        except Exception:
            pass
    
    def load_creds(self):
        if os.path.exists('token.pickle'):
            try:
                with open('token.pickle', 'rb') as f:
                    self.creds = pickle.load(f)
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    self.creds.refresh(Request())
                if self.creds and self.creds.valid:
                    self.set_auth_pill("✓ авторизован", "#51CF66")
                    self.auth_btn.setText("🔄 Повторить")
                    self.upload_btn.setEnabled(True)
                    self.get_channel()
            except Exception as e:
                logging.exception("Ошибка загрузки учетных данных")
                self.set_auth_pill("❌ ошибка токена", "#FF6B6B")
    
    def get_channel(self):
        try:
            yt = build('youtube', 'v3', credentials=self.creds)
            r = yt.channels().list(part='snippet', mine=True).execute()
            if 'items' in r and r['items']:
                self.channel = r['items'][0]['snippet']['title']
                self.set_nick_pill(self.channel, "#51CF66")
                logging.info(f"Успешно получен канал: {self.channel}")
        except Exception as e:
            self.channel = "ошибка"
            self.set_nick_pill(self.channel, "#FF6B6B")
            logging.exception("Ошибка получения данных канала")
    
    def auth(self):
        try:
            if not os.path.exists('client_secrets.json'):
                self.set_auth_pill("❌ файл не найден", "#FF6B6B")
                return
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            self.creds = flow.run_local_server(port=8080)
            with open('token.pickle', 'wb') as f:
                pickle.dump(self.creds, f)
            self.set_auth_pill("✓ авторизован", "#51CF66")
            self.auth_btn.setText("🔄 Повторить")
            self.upload_btn.setEnabled(True)
            self.get_channel()
        except Exception as e:
            self.set_auth_pill("❌ ошибка", "#FF6B6B")
            print(f"Auth error: {e}")

    def load_config(self):
        """Загружаем конфигурацию приложения (config.json)"""
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                self.allow_upload_without_ffmpeg = bool(cfg.get('allow_upload_without_ffmpeg', False))
                # Новый флаг: отключить редактор полностью
                self.disable_editor_completely = bool(cfg.get('disable_editor_completely', False))
                # Настройка: политика приватности по умолчанию для загрузок
                self.default_privacy = str(cfg.get('default_privacy', getattr(self, 'default_privacy', 'private')))
                # GitHub repo for updates (owner/repo)
                self.github_repo = str(cfg.get('github_repo', getattr(self, 'github_repo', GITHUB_REPO)))
                # auto-check flag
                self.auto_check_on_start = bool(cfg.get('auto_check_on_start', getattr(self, 'auto_check_on_start', True)))
                # если UI уже создан — применяем состояние чекбокса
                try:
                    # (репозиторий для авто-проверки хранится в config.json или в GITHUB_REPO; UI поле удалено)
                    try:
                        if hasattr(self, 'auto_check_checkbox'):
                            self.auto_check_checkbox.setChecked(bool(self.auto_check_on_start))
                    except Exception:
                        pass
                    if hasattr(self, 'allow_upload_checkbox'):
                        self.allow_upload_checkbox.setChecked(self.allow_upload_without_ffmpeg)
                    if hasattr(self, 'disable_editor_checkbox'):
                        self.disable_editor_checkbox.setChecked(self.disable_editor_completely)
                    # если существует комбобокс приватности — применяем значение
                    try:
                        if hasattr(self, 'privacy_combo') and self.privacy_combo is not None:
                            if self.default_privacy in ['private','unlisted','public']:
                                self.privacy_combo.setCurrentText(self.default_privacy)
                    except Exception:
                        pass
                    # обновляем индикатор состояния редактора
                    try:
                        if hasattr(self, 'update_editor_indicator'):
                            self.update_editor_indicator()
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception as e:
            logging.exception('Ошибка загрузки config.json')
    
    def select_video(self):
        p, _ = QFileDialog.getOpenFileName(self, "Выберите видео", "", "Video (*.mp4 *.avi *.mov *.mkv *.flv *.wmv);;All (*)")
        if p:
            self.video_path = p
            self.vid_label.setText(os.path.basename(p))
            self.vid_label.setStyleSheet("color: #51CF66; font-size: 12px; font-weight: bold;")
            # Обновляем индикатор/состояние редактора
            try:
                if hasattr(self, 'update_editor_indicator'):
                    self.update_editor_indicator()
            except Exception:
                pass

    def open_video_editor(self):
        if not self.video_path:
            QMessageBox.warning(self, 'Ошибка', 'Сначала выберите видео файл.')
            return
        # Если редактор отключён полностью — не открываем
        try:
            if getattr(self, 'disable_editor_completely', False):
                QMessageBox.information(self, 'Редактор отключён', 'Редактор видео отключён в настройках.')
                return
        except Exception:
            pass

        # Если FFmpeg недоступен, но пользователь включил опцию разрешить загрузку без FFmpeg —
        # не пытаемся открывать редактор и просто информируем пользователя.
        try:
            if shutil.which('ffmpeg') is None and getattr(self, 'allow_upload_without_ffmpeg', False):
                QMessageBox.information(
                    self,
                    'Редактор недоступен',
                    'FFmpeg не найден — редактор отключён по настройке. Вы можете загрузить видео без редактирования.'
                )
                return
        except Exception:
            # если что-то пошло не так при проверке — продолжаем попытку открыть диалог
            pass
        dlg = VideoTrimDialog(self, self.video_path)
        # exec will accept() when trimming finished; VideoTrimDialog.result_path contains path
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if getattr(dlg, 'result_path', None):
                self.video_path = dlg.result_path
                # mark label to indicate trimmed file is used
                try:
                    self.vid_label.setText(os.path.basename(self.video_path) + ' (обрез.)')
                    self.vid_label.setStyleSheet("color: #FFD93D; font-size: 12px; font-weight: bold;")
                except Exception:
                    pass
                # remember to cleanup after upload
                try:
                    self._temp_trim_files.append(self.video_path)
                except Exception:
                    pass
    
    def start_upload(self):
        if not self.video_path:
            self.status_label.setText("❌ Выберите видео")
            self.status_label.setStyleSheet("color: #FF6B6B; font-size: 12px; padding: 8px;")
            return
        n = self.name_input.text().strip()
        lk = self.link_input.text().strip()
        if not n or not lk:
            self.status_label.setText("❌ Заполните все поля")
            self.status_label.setStyleSheet("color: #FF6B6B; font-size: 12px; padding: 8px;")
            return
            
        # Проверка URL
        try:
            result = urlparse(lk)
            if not all([result.scheme, result.netloc]):
                self.status_label.setText("❌ Неверный формат ссылки")
                self.status_label.setStyleSheet("color: #FF6B6B; font-size: 12px; padding: 8px;")
                return
            if not result.scheme in ['http', 'https']:
                self.status_label.setText("❌ Ссылка должна начинаться с http:// или https://")
                self.status_label.setStyleSheet("color: #FF6B6B; font-size: 12px; padding: 8px;")
                return
        except Exception as e:
            logging.exception("Ошибка проверки URL")
            self.status_label.setText("❌ Неверный формат ссылки")
            self.status_label.setStyleSheet("color: #FF6B6B; font-size: 12px; padding: 8px;")
            return
        if not self.creds or not self.creds.valid:
            self.status_label.setText("❌ Авторизуйтесь")
            self.status_label.setStyleSheet("color: #FF6B6B; font-size: 12px; padding: 8px;")
            return
        d = f"Ссылка на жалобу: {lk}"
        ex = self.desc_input.toPlainText().strip()
        if ex: d += f"\n\n{ex}"
        self.upload_btn.setEnabled(False)
        self.status_label.setText("⏳ Подготовка...")
        self.status_label.setStyleSheet("color: #FFD93D; font-size: 12px; padding: 8px;")
        self.link_container.setVisible(False)
        if self.upload_thread and self.upload_thread.isRunning():
            self.upload_thread.quit()
            self.upload_thread.wait()
        # determine privacy for this upload (per-upload override or default)
        try:
            if getattr(self, 'upload_privacy_combo', None):
                privacy = self.upload_privacy_combo.currentData() or getattr(self, 'default_privacy', 'private')
            else:
                privacy = getattr(self, 'default_privacy', 'private')
        except Exception:
            privacy = getattr(self, 'default_privacy', 'private')

        # remember title/privacy for history
        try:
            self._last_upload_title = n
            self._last_upload_privacy = privacy
        except Exception:
            self._last_upload_title = n
            self._last_upload_privacy = privacy

        self.upload_thread = UploadThread(self.creds, self.video_path, n, d, allow_missing_ffmpeg=getattr(self, 'allow_upload_without_ffmpeg', False), privacy_status=privacy)
        self.upload_thread.progress.connect(self.update_progress)
        self.upload_thread.finished.connect(self.upload_done)
        self.upload_thread.start()
    
    def update_progress(self, m):
        self.status_label.setText(f"⏳ {m}")
        self.status_label.setStyleSheet("color: #FFD93D; font-size: 12px; padding: 8px;")
    
    def upload_done(self, s, r):
        self.upload_btn.setEnabled(True)
        if s:
            self.video_url = r
            self.status_label.setText("✓ Загружено!")
            self.status_label.setStyleSheet("color: #51CF66; font-size: 12px; padding: 8px;")
            self.vid_link.setText(r)
            self.link_container.setVisible(True)
            self.name_input.clear()
            self.link_input.clear()
            self.desc_input.clear()
            # cleanup temporary trimmed files if any
            try:
                for tf in list(getattr(self, '_temp_trim_files', [])):
                    try:
                        if os.path.exists(tf):
                            os.unlink(tf)
                    except Exception:
                        pass
                self._temp_trim_files = []
            except Exception:
                pass
            self.video_path = None
            self.vid_label.setText("Файл не выбран")
            self.vid_label.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 12px;")
            # Добавляем запись в историю
            try:
                title = getattr(self, '_last_upload_title', '')
                privacy = getattr(self, '_last_upload_privacy', getattr(self, 'default_privacy', 'private'))
                self._add_history_entry(self.video_url, title, privacy)
            except Exception:
                pass
        else:
            self.status_label.setText(f"❌ {r}")
            self.status_label.setStyleSheet("color: #FF6B6B; font-size: 12px; padding: 8px;")
    
    def copy_link(self):
        if self.video_url:
            QApplication.clipboard().setText(self.video_url)
            self.status_label.setText("✓ Ссылка скопирована!")
            self.status_label.setStyleSheet("color: #51CF66; font-size: 12px; padding: 8px;")
    
    def copy_ahk(self):
        t = f"Никнейм: {self.ahk_nick.text()}\nВаш уровень администрирования: {self.ahk_level.text()}\n{self.ahk_status.currentText()}\nПричина отсутствия: {self.ahk_reason.toPlainText()}\nС {self.ahk_date_from.date().toString('dd.MM.yyyy')} до {self.ahk_date_to.date().toString('dd.MM.yyyy')}"
        QApplication.clipboard().setText(t)
        self.ahk_preview.setText(t)
        from PyQt6.QtCore import QTimer
        g = self.ahk_preview.parent()
        old = g.title()
        g.setTitle("✓ Скопировано!")
        g.setStyleSheet("QGroupBox {color: #51CF66;}")
        QTimer.singleShot(2000, lambda: g.setTitle(old))
        QTimer.singleShot(2000, lambda: g.setStyleSheet(""))

    # ---------------- upload history -----------------
    def _load_upload_history(self):
        try:
            self.upload_history = []
            if os.path.exists(self.HISTORY_FILE):
                with open(self.HISTORY_FILE, 'r', encoding='utf-8') as f:
                    self.upload_history = json.load(f) or []
        except Exception:
            self.upload_history = []
        # refresh UI if possible
        try:
            self._refresh_history_ui()
        except Exception:
            pass

    def _save_upload_history(self):
        try:
            with file_lock:
                with open(self.HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self.upload_history, f, ensure_ascii=False, indent=2)
        except Exception:
            logging.exception('Не удалось сохранить историю загрузок')

    def _add_history_entry(self, url, title, privacy):
        try:
            entry = {
                'url': url,
                'title': title,
                'privacy': privacy,
                'time': datetime.now().isoformat()
            }
            # prepend
            self.upload_history.insert(0, entry)
            # keep only last 100 entries
            self.upload_history = self.upload_history[:100]
            self._save_upload_history()
            self._refresh_history_ui()
        except Exception:
            logging.exception('Ошибка добавления истории')

    def _refresh_history_ui(self):
        try:
            if hasattr(self, 'upload_history_list') and self.upload_history_list is not None:
                self.upload_history_list.clear()
                for e in self.upload_history:
                    dt = e.get('time', '')
                    t = e.get('title', '')
                    p = e.get('privacy', '')
                    url = e.get('url', '')
                    label = f"{t} — {p} — {dt.split('T')[0]}"
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, url)
                    self.upload_history_list.addItem(item)
        except Exception:
            pass

    def _open_selected_history(self):
        try:
            it = self.upload_history_list.currentItem()
            if not it:
                return
            url = it.data(Qt.ItemDataRole.UserRole)
            if url:
                webbrowser.open(url)
        except Exception:
            pass

    def _copy_selected_history(self):
        try:
            it = self.upload_history_list.currentItem()
            if not it:
                return
            url = it.data(Qt.ItemDataRole.UserRole)
            if url:
                QApplication.clipboard().setText(url)
                self.status_label.setText('✓ Ссылка скопирована!')
                self.status_label.setStyleSheet("color: #51CF66; font-size: 12px; padding: 8px;")
        except Exception:
            pass

    def _clear_history(self):
        try:
            self.upload_history = []
            self._save_upload_history()
            self._refresh_history_ui()
        except Exception:
            pass
    
    def _do_save_ahk(self):
        d = {
            'nick': self.ahk_nick.text(),
            'level': self.ahk_level.text(),
            'status': self.ahk_status.currentText(),
            'reason': self.ahk_reason.toPlainText(),
            'from': self.ahk_date_from.date().toString("dd.MM.yyyy"),
            'to': self.ahk_date_to.date().toString("dd.MM.yyyy")
        }
        try:
            # keep in-memory state in sync so later _save_settings() won't overwrite it with stale data
            self.ahk_data = dict(d)
            # write atomically using file lock to avoid races
            with file_lock:
                with open(self.AHK_DATA_FILE, 'w', encoding='utf-8') as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
            logging.info("AHK данные сохранены")
        except Exception as e:
            logging.exception("Ошибка сохранения AHK данных")
    
    def save_ahk(self):
        # Отменяем предыдущий таймер, если он существует
        if hasattr(self, '_save_timer'):
            self._save_timer.stop()
        else:
            self._save_timer = QTimer()
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(self._do_save_ahk)
        
        # Запускаем новый таймер (сохранение через 1 секунду после последнего изменения)
        self._save_timer.start(1000)
    
    def load_ahk(self):
        if os.path.exists(self.AHK_DATA_FILE):
            try:
                with open(self.AHK_DATA_FILE, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                # keep in-memory copy in sync
                try:
                    self.ahk_data = dict(d)
                except Exception:
                    self.ahk_data = {}
                self.ahk_nick.setText(d.get('nick', ''))
                self.ahk_level.setText(d.get('level', ''))
                self.ahk_status.setCurrentText(d.get('status', 'Неактив'))
                self.ahk_reason.setText(d.get('reason', ''))
                if 'from' in d:
                    df = QDate.fromString(d['from'], "dd.MM.yyyy")
                    if df.isValid(): self.ahk_date_from.setDate(df)
                if 'to' in d:
                    dt = QDate.fromString(d['to'], "dd.MM.yyyy")
                    if dt.isValid(): self.ahk_date_to.setDate(dt)
            except: pass
    
    def change_theme(self, n):
        self.theme = n
        self.apply_theme()
        try:
            with open('theme.txt', 'w') as f: f.write(n)
        except: pass

    def _style_dialog(self, dlg):
        """Apply current app theme to a dialog (QMessageBox/QProgressDialog).

        This attempts to use the same card/background color as the app theme so
        dialogs are readable (white text on dark background) instead of the
        default white-on-white in some Windows themes.
        """
        try:
            t = THEMES.get(getattr(self, 'theme', 'Классическая'), THEMES['Классическая'])
            # t['b'] stored as rgba string in themes — use it for dialog background
            bg = t.get('b', 'rgba(30,30,45,0.85)')
            accent = ACCENT
            dlg.setStyleSheet(f"""
                QWidget {{ background-color: {bg}; color: white; border-radius: 12px; }}
                QLabel {{ color: white; }}
                QPushButton {{ background-color: rgba(255,255,255,0.06); color: white; border-radius: 8px; padding: 6px 12px; }}
                QProgressBar {{ background-color: rgba(255,255,255,0.06); color: white; border-radius: 8px; height: 14px; }}
                QProgressDialog QLabel {{ color: white; }}
            """)
        except Exception:
            # Best-effort only; do not raise
            pass

    def show_update_check(self):
        """Проверить наличие обновления на GitHub Releases и показать результат.

        Если `GITHUB_REPO` не настроен (placeholder), покажем подсказку.
        """
        try:
            # используем instance repo, а если он некорректен — пробуем глобальную константу
            repo = getattr(self, 'github_repo', None)
            if not repo or repo.startswith('yourusername') or repo.startswith('your-') or '/' not in repo:
                # fallback to global repo constant if present
                fallback = GITHUB_REPO if GITHUB_REPO and '/' in GITHUB_REPO and not GITHUB_REPO.startswith('yourusername') and not GITHUB_REPO.startswith('your-') else None
                if fallback:
                    repo = fallback
                else:
                    msg = QMessageBox(self)
                    msg.setWindowTitle('Проверка обновлений')
                    msg.setText('GitHub репозиторий для авто-проверки не настроен')
                    msg.setInformativeText('Установите константу GITHUB_REPO в начале файла (формат owner/repo).')
                    msg.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                    try: self._style_dialog(msg)
                    except: pass
                    msg.exec()
                    # всё ещё покажем демонстрационный сплэш
                    try:
                        splash = PreloadSplash(parent=self, theme_name=self.theme)
                        splash.exec()
                    except Exception:
                        logging.exception('Ошибка отображения демонстрационного сплэша')
                    return

            # Показываем прогресс-диалог и запускаем фоновую проверку
            progress = QProgressDialog('Проверка обновлений на GitHub...', 'Отмена', 0, 0, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setWindowTitle('Проверка обновлений')
            progress.setCancelButtonText('Отмена')
            progress.show()

            try:
                self._style_dialog(progress)
            except Exception:
                pass

            def on_done(res: dict):
                try:
                    if progress:
                        progress.close()
                except Exception:
                    pass

                if not res.get('ok') or not res.get('release'):
                    msg = QMessageBox(self)
                    msg.setWindowTitle('Проверка обновлений')
                    msg.setText('Не удалось получить информацию о релизах')
                    msg.setInformativeText('Проверьте интернет или конфигурацию.')
                    msg.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                    try: self._style_dialog(msg)
                    except: pass
                    msg.exec()
                    return
                else:
                    rel = res['release']
                    tag = rel.get('tag_name')
                    url = rel.get('html_url')
                    readable = rel.get('name') or tag or 'Новый релиз'

                    cmp = compare_versions(APP_VERSION, _normalize_tag(tag))
                    if cmp < 0:
                        txt = f'Найдена новая версия: {tag}\n\nТекущая: {APP_VERSION}\nРелиз: {readable}\n\nОткрыть страницу релиза?'
                        m = QMessageBox(self)
                        m.setWindowTitle('Доступно обновление')
                        m.setText(txt)
                        open_btn = m.addButton('Открыть', QMessageBox.ButtonRole.ActionRole)
                        cancel_btn = m.addButton('Отмена', QMessageBox.ButtonRole.RejectRole)
                        try: self._style_dialog(m)
                        except: pass
                        m.exec()
                        if m.clickedButton() is open_btn and url:
                            webbrowser.open(url)
                    elif cmp == 0:
                        m = QMessageBox(self)
                        m.setWindowTitle('Проверка обновлений')
                        m.setText(f'У вас последняя версия ({APP_VERSION}).')
                        m.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                        try: self._style_dialog(m)
                        except: pass
                        m.exec()
                    else:
                        m = QMessageBox(self)
                        m.setWindowTitle('Проверка обновлений')
                        m.setText(f'Установлена бета/необычная версия ({APP_VERSION}). GitHub latest: {tag}')
                        m.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                        try: self._style_dialog(m)
                        except: pass
                        m.exec()

                # После проверки релиза — всегда проверяем содержимое файла youtube_uploader.py в репозитории
                try:
                    file_path = 'youtube_uploader.py'

                    def on_file_done(fr: dict):
                        try:
                            if not fr.get('ok') or not fr.get('file'):
                                # если не удалось получить file — молча возвращаемся
                                return
                            remote = fr['file']
                            remote_content = remote.get('content', '')
                            # Read local file (this module's file)
                            try:
                                local_fn = os.path.abspath(__file__)
                                with open(local_fn, 'r', encoding='utf-8') as f:
                                    local_content = f.read()
                            except Exception:
                                # fallback — try working dir
                                try:
                                    with open(file_path, 'r', encoding='utf-8') as f:
                                        local_content = f.read()
                                except Exception:
                                    return

                            # Normalize newlines
                            rc = remote_content.replace('\r\n', '\n')
                            lc = local_content.replace('\r\n', '\n')

                            if rc != lc:
                                # файл отличается — предлагаем открыть страницу на GitHub, заменить локальную копию или отменить
                                try:
                                    txt = 'Файл youtube_uploader.py на GitHub отличается от локальной версии.'
                                    msg = QMessageBox(self)
                                    msg.setWindowTitle('Файл обновлён')
                                    msg.setText(txt)
                                    msg.setInformativeText('Открыть страницу, скачать и заменить локальную копию или отменить?')
                                    open_btn = msg.addButton('Открыть', QMessageBox.ButtonRole.ActionRole)
                                    replace_btn = msg.addButton('Заменить локально', QMessageBox.ButtonRole.AcceptRole)
                                    cancel_btn = msg.addButton('Отмена', QMessageBox.ButtonRole.RejectRole)
                                    try: self._style_dialog(msg)
                                    except: pass
                                    msg.exec()

                                    clicked = msg.clickedButton()
                                    if clicked is open_btn:
                                        url = remote.get('download_url') or f'https://github.com/{repo}/blob/main/{file_path}'
                                        webbrowser.open(url)
                                    elif clicked is replace_btn:
                                        # запуск замены в фоне
                                        try:
                                            local_fn = os.path.abspath(__file__)
                                        except Exception:
                                            local_fn = file_path

                                        # Progress dialog
                                        progress2 = QProgressDialog('Скачивание и замена файла...', 'Отмена', 0, 0, self)
                                        try: self._style_dialog(progress2)
                                        except: pass
                                        progress2.setWindowModality(Qt.WindowModality.WindowModal)
                                        progress2.setAutoClose(False)
                                        progress2.setCancelButtonText('Отмена')
                                        progress2.show()

                                        def on_done_replace(dr: dict):
                                            try:
                                                try:
                                                    progress2.close()
                                                except Exception:
                                                    pass
                                                if not dr.get('ok'):
                                                    em = QMessageBox(self)
                                                    em.setWindowTitle('Ошибка')
                                                    em.setText(f"Не удалось заменить файл: {dr.get('error')}")
                                                    em.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                                                    try: self._style_dialog(em)
                                                    except: pass
                                                    em.exec()
                                                else:
                                                    backup = dr.get('backup')
                                                    msg_text = 'Файл успешно заменён.'
                                                    if backup:
                                                        msg_text += f' Бэкап сохранён: {backup}'
                                                    msg_text += '\n\nПерезапустите приложение, чтобы изменения вступили в силу.'
                                                    im = QMessageBox(self)
                                                    im.setWindowTitle('Готово')
                                                    im.setText(msg_text)
                                                    im.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                                                    try: self._style_dialog(im)
                                                    except: pass
                                                    im.exec()
                                            except Exception:
                                                logging.exception('Ошибка обработки результата замены')

                                        try:
                                            drt = DownloadReplaceThread(repo, file_path, local_fn, parent=self)
                                            drt.done.connect(on_done_replace)
                                            drt.start()
                                        except Exception:
                                            progress2.close()
                                            logging.exception('Не удалось запустить замену файла')

                                except Exception:
                                    logging.exception('Ошибка показа диалога об отличии файла')

                        except Exception:
                            logging.exception('Ошибка обработки результата проверки файла')

                    ft = FileCheckThread(repo, 'youtube_uploader.py', parent=self)
                    ft.done.connect(on_file_done)
                    ft.start()
                except Exception:
                    logging.exception('Не удалось запустить проверку файла на GitHub')

            # запускаем в QThread
            try:
                t = ReleaseCheckThread(repo, parent=self)
                t.done.connect(on_done)
                t.start()
            except Exception as e:
                logging.exception('Не удалось запустить проверку релиза')
                progress.close()

        except Exception:
            logging.exception('Ошибка отображения окна проверки обновлений')

    def _start_auto_update_check(self):
        """Тихая авто-проверка при старте: показывает уведомление только если найдена новая версия."""
        try:
            repo = getattr(self, 'github_repo', GITHUB_REPO)
            if not repo or repo.startswith('yourusername') or repo.startswith('your-') or '/' not in repo:
                # репозиторий не задан — молча ничего не делаем
                return

            def on_done(res: dict):
                try:
                    if not res.get('ok') or not res.get('release'):
                        return
                    rel = res['release']
                    tag = rel.get('tag_name')
                    url = rel.get('html_url')
                    cmp = compare_versions(APP_VERSION, _normalize_tag(tag))
                    if cmp < 0:
                        # уведомляем пользователя — только если найдена новая версия
                        try:
                            # малое окно: спрашиваем — открыть релиз в браузере?
                            txt = f'Доступна новая версия {tag} (текущая {APP_VERSION}). Открыть страницу релиза?'
                            m = QMessageBox(self)
                            m.setWindowTitle('Обновление доступно')
                            m.setText(txt)
                            ob = m.addButton('Открыть', QMessageBox.ButtonRole.ActionRole)
                            cb = m.addButton('Отмена', QMessageBox.ButtonRole.RejectRole)
                            try: self._style_dialog(m)
                            except: pass
                            m.exec()
                            if m.clickedButton() is ob and url:
                                webbrowser.open(url)
                        except Exception:
                            logging.exception('Ошибка показа уведомления об обновлении')

                except Exception:
                    logging.exception('Ошибка обработки результата авто-проверки')

            # запускаем проверку релиза в фоновом потоке
            t = ReleaseCheckThread(repo, parent=self)
            t.done.connect(on_done)
            t.start()

            # также запускаем проверку содержимого файла youtube_uploader.py и сравниваем с локальной версией
            try:
                file_path = 'youtube_uploader.py'

                def on_file_done(fr: dict):
                    try:
                        if not fr.get('ok') or not fr.get('file'):
                            return
                        remote = fr['file']
                        remote_content = remote.get('content', '')
                        # Read local file (this module's file)
                        try:
                            local_fn = os.path.abspath(__file__)
                            with open(local_fn, 'r', encoding='utf-8') as f:
                                local_content = f.read()
                        except Exception:
                            # fallback — try working dir
                            try:
                                with open(file_path, 'r', encoding='utf-8') as f:
                                    local_content = f.read()
                            except Exception:
                                return

                        # Normalize newlines
                        rc = remote_content.replace('\r\n', '\n')
                        lc = local_content.replace('\r\n', '\n')

                        if rc != lc:
                            # файл отличается — предлагаем открыть страницу на GitHub, заменить или отменить
                            try:
                                txt = 'Файл youtube_uploader.py на GitHub отличается от локальной версии.'
                                msg = QMessageBox(self)
                                msg.setWindowTitle('Файл обновлён')
                                msg.setText(txt)
                                msg.setInformativeText('Открыть страницу, скачать и заменить локальную копию или отменить?')
                                open_btn = msg.addButton('Открыть', QMessageBox.ButtonRole.ActionRole)
                                replace_btn = msg.addButton('Заменить локально', QMessageBox.ButtonRole.AcceptRole)
                                cancel_btn = msg.addButton('Отмена', QMessageBox.ButtonRole.RejectRole)
                                try: self._style_dialog(msg)
                                except: pass
                                msg.exec()

                                clicked = msg.clickedButton()
                                if clicked is open_btn:
                                    url = remote.get('download_url') or f'https://github.com/{repo}/blob/main/{file_path}'
                                    webbrowser.open(url)
                                elif clicked is replace_btn:
                                    # запуск замены в фоне
                                    try:
                                        local_fn = os.path.abspath(__file__)
                                    except Exception:
                                        local_fn = file_path

                                    # Progress dialog
                                    progress = QProgressDialog('Скачивание и замена файла...', 'Отмена', 0, 0, self)
                                    try: self._style_dialog(progress)
                                    except: pass
                                    progress.setWindowModality(Qt.WindowModality.WindowModal)
                                    progress.setAutoClose(False)
                                    progress.setCancelButtonText('Отмена')
                                    progress.show()

                                    def on_done_replace(dr: dict):
                                        try:
                                            try:
                                                progress.close()
                                            except Exception:
                                                pass
                                                if not dr.get('ok'):
                                                    em = QMessageBox(self)
                                                    em.setWindowTitle('Ошибка')
                                                    em.setText(f"Не удалось заменить файл: {dr.get('error')}")
                                                    em.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                                                    try: self._style_dialog(em)
                                                    except: pass
                                                    em.exec()
                                                else:
                                                    backup = dr.get('backup')
                                                    msg_text = 'Файл успешно заменён.'
                                                    if backup:
                                                        msg_text += f' Бэкап сохранён: {backup}'
                                                    msg_text += '\n\nПерезапустите приложение, чтобы изменения вступили в силу.'
                                                    im = QMessageBox(self)
                                                    im.setWindowTitle('Готово')
                                                    im.setText(msg_text)
                                                    im.addButton('OK', QMessageBox.ButtonRole.AcceptRole)
                                                    try: self._style_dialog(im)
                                                    except: pass
                                                    im.exec()
                                        except Exception:
                                            logging.exception('Ошибка обработки результата замены')

                                    try:
                                        drt = DownloadReplaceThread(repo, file_path, local_fn, parent=self)
                                        drt.done.connect(on_done_replace)
                                        drt.start()
                                    except Exception:
                                        progress.close()
                                        logging.exception('Не удалось запустить замену файла')

                            except Exception:
                                logging.exception('Ошибка показа диалога об отличии файла')

                    except Exception:
                        logging.exception('Ошибка обработки результата проверки файла')

                ft = FileCheckThread(repo, 'youtube_uploader.py', parent=self)
                ft.done.connect(on_file_done)
                ft.start()
            except Exception:
                logging.exception('Не удалось запустить проверку файла на GitHub')

        except Exception:
            logging.exception('Не удалось выполнить авто-проверку обновлений')

    def on_toggle_allow_upload_without_ffmpeg(self, enabled: bool):
        """Обработчик переключения флага разрешения загрузки без FFmpeg."""
        try:
            self.allow_upload_without_ffmpeg = bool(enabled)
            # Сохраняем конфигурацию
            try:
                self._save_settings()
            except Exception:
                logging.exception('Не удалось сохранить настройки')

            # если ffmpeg отсутствует — редактор всё равно недоступен
            try:
                if shutil.which('ffmpeg') is None:
                    # гарантированно отключаем кнопку редактора
                    if hasattr(self, 'edit_btn'):
                        self.edit_btn.setEnabled(False)
                else:
                    # ffmpeg доступен — активируем кнопку, только если есть файл
                    if hasattr(self, 'edit_btn'):
                        self.edit_btn.setEnabled(bool(self.video_path))
            except Exception:
                pass
            # обновляем визуальный индикатор
            try:
                if hasattr(self, 'update_editor_indicator'):
                    self.update_editor_indicator()
            except Exception:
                pass
        except Exception:
            logging.exception('Ошибка обновления allow_upload_without_ffmpeg')

    def on_toggle_auto_check(self, enabled: bool):
        """Handler for auto-check checkbox."""
        try:
            self.auto_check_on_start = bool(enabled)
            try:
                self._save_settings()
            except Exception:
                logging.exception('Не удалось сохранить настройку auto_check_on_start')
        except Exception:
            logging.exception('Ошибка обработки auto_check_on_start')

    
    def load_theme(self):
        if os.path.exists('theme.txt'):
            try:
                with open('theme.txt', 'r') as f:
                    t = f.read().strip()
                    if t in THEMES:
                        self.theme = t
                    else:
                        self.theme = "Классическая"
                    # ensure combo exists (settings may be created later)
                    try: self.theme_combo.setCurrentText(self.theme)
                    except: pass

        # ! Перемещено: обработчик и индикатор находятся на уровне класса, а не внутри load_theme
            except: pass

    def on_toggle_disable_editor(self, enabled: bool):
        """Обработчик переключения флага полного отключения редактора."""
        try:
            self.disable_editor_completely = bool(enabled)
            try:
                self._save_settings()
            except Exception:
                logging.exception('Не удалось сохранить настройки')

            # обновляем интерфейс и индикатор
            try:
                if hasattr(self, 'update_editor_indicator'):
                    self.update_editor_indicator()
            except Exception:
                pass
        except Exception:
            logging.exception('Ошибка обновления disable_editor_completely')

    def update_editor_indicator(self):
        """Обновляет текст-индикатор состояния FFmpeg и редактора и управляет кнопкой редактирования."""
        try:
            ff_found = shutil.which('ffmpeg') is not None
            disabled = bool(getattr(self, 'disable_editor_completely', False))
            allow_missing = bool(getattr(self, 'allow_upload_without_ffmpeg', False))

            parts = []
            if ff_found:
                parts.append('FFmpeg: найден ✅')
            else:
                parts.append('FFmpeg: не найден ⚠️')

            if disabled:
                parts.append('Редактор: полностью отключён (по настройке)')
            else:
                if ff_found:
                    parts.append('Редактор: доступен ✂️')
                else:
                    if allow_missing:
                        parts.append('Редактор: недоступен (FFmpeg отсутствует), но загрузка разрешена')
                    else:
                        parts.append('Редактор: недоступен (требуется FFmpeg)')

            txt = "\n".join(parts)
            try:
                self.editor_indicator_label.setText(txt)
            except Exception:
                pass

            # Управляем видимостью и доступностью кнопки редактора
            try:
                if disabled:
                    try:
                        self.edit_btn.hide()
                    except Exception:
                        self.edit_btn.setEnabled(False)
                else:
                    try:
                        self.edit_btn.show()
                    except Exception:
                        pass

                    # включать только если ffmpeg доступен и выбран файл
                    enable_btn = ff_found and bool(self.video_path)
                    # если ffmpeg отсутствует, но разрешено (allow_missing) — редактор не доступен
                    if not ff_found:
                        enable_btn = False
                    try:
                        self.edit_btn.setEnabled(bool(enable_btn))
                    except Exception:
                        pass
            except Exception:
                pass

        except Exception:
            logging.exception('Ошибка обновления индикатора редактора')


class CenteredProgressBar(QProgressBar):
    """QProgressBar с текстом процента, нарисованным поверх бара и выровненным по центру."""
    def __init__(self, parent=None):
        super().__init__(parent)
        # подстраиваем выравнивание, стандартный текст отключаем в caller
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def paintEvent(self, event):
        # Рисуем стандартный прогресс, затем поверх текст по центру
        super().paintEvent(event)
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            pen = QPen(QColor(255, 255, 255))
            p.setPen(pen)
            text = f"{int(self.value())}%"
            rect = self.rect()
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            p.end()
        except Exception:
            pass

class PreloadSplash(QDialog):
    """Небольшой предзагрузочный экран со следующей логикой:
    - читает имя темы из файла (theme_file) и применяет градиент
    - имитирует проверку обновлений
    - если обновление найдено — предлагает кнопку 'Обновить' (эмуляция)
    - если обновлений нет — показывает фейковую загрузку и закрывается
    """

    def __init__(self, parent=None, theme_file='theme.txt', theme_name: str | None = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setFixedSize(640, 340)

        # Определяем тему: если явно передали theme_name — используем её, иначе смотрим файл
        if theme_name is None:
            theme_name = 'Классическая'
            try:
                if theme_file and os.path.exists(theme_file):
                    with open(theme_file, 'r', encoding='utf-8') as f:
                        tname = f.read().strip()
                        if tname:
                            theme_name = tname
            except Exception:
                pass

        t = THEMES.get(theme_name, THEMES['Классическая'])
        self.gs = t.get('s', SOFT_GRAD_START)
        self.ge = t.get('e', SOFT_GRAD_END)
        self._accent = ACCENT

        # Сохраняем имя темы для возможности смены динамически
        self._theme_name = theme_name

        # Если тема "Зима" — добавляем снежные эффекты поверх сплэша
        self.effect_widget = None
        if self._theme_name == "Зима":
            try:
                self.effect_widget = SnowEffectWidget(self)
                self.effect_widget.setGeometry(0, 0, self.width(), self.height())
                # не мешаем мыши, и поднимаем наверх
                self.effect_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                self.effect_widget.raise_()
                self.effect_widget.show()
            except Exception:
                self.effect_widget = None

        # UI
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 28, 28, 18)
        v.setSpacing(12)

        self.title = QLabel('Helper by Krotinov\n <3')
        self.title.setStyleSheet('font-size: 22px; font-weight: bold; color: white;')
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.title)

        self.sub = QLabel('Проверка обновлений...')
        self.sub.setStyleSheet('font-size: 14px; color: rgba(255,255,255,0.9);')
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.sub)

        filler = QWidget(self)
        filler.setFixedHeight(160)
        v.addWidget(filler)

        # Используем подкласс прогресс-бара, который рисует процент в центре
        self.progress = CenteredProgressBar(self)
        self.progress.setMinimum(0)
        self.progress.setMaximum(100)
        self.progress.setValue(0)
        # Внутренний текст рисуется кастомно (центрально) — отключаем стандартную надпись
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(
            f"QProgressBar{{border-radius:10px; background: rgba(255,255,255,0.06); color: white;}}"
            f" QProgressBar::chunk{{background: {self._accent}; border-radius: 10px;}}"
        )
        v.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.btn_update = QPushButton('Обновить')
        self.btn_update.setVisible(False)
        self.btn_update.clicked.connect(self._on_update_clicked)
        self.btn_skip = QPushButton('Пропустить')
        self.btn_skip.setVisible(False)
        self.btn_skip.clicked.connect(self._on_skip_clicked)
        buttons.addWidget(self.btn_update)
        buttons.addWidget(self.btn_skip)
        v.addLayout(buttons)

        # Internal
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._on_tick)
        self._progress = 0
        self._update_found = False
        self._stage = 0

        # Центрируем диалог на экране
        try:
            screen = QApplication.primaryScreen().availableGeometry()
            x = (screen.width() - self.width()) // 2
            y = (screen.height() - self.height()) // 2
            self.move(x, y)
        except Exception:
            pass

        # Запускаем имитацию проверки
        QTimer.singleShot(180, self._start)

    def _start(self):
        import random
        # вероятность найти обновление — 30%
        self._update_found = random.random() < 0.30
        self._timer.start()

    def _on_tick(self):
        # Разделим поведение по прогресс-диапазонам
        if self._progress < 35:
            self.sub.setText('Проверка обновлений...')
            self._progress += 1 + (self._progress // 15)

        elif self._progress < 70:
            # после проверки — подготовка/импорт конфигураций
            if self._progress == 35 and self._update_found:
                # сообщение о доступном обновлении — ставим паузу и показываем кнопки
                self._timer.stop()
                self.sub.setText('Доступно обновление')
                self.btn_update.setVisible(True)
                self.btn_skip.setVisible(True)
                return

            self.sub.setText('Подготовка...')
            self._progress += 1

        elif self._progress < 100:
            self.sub.setText('Загрузка ресурсов...')
            self._progress += 1

        else:
            # Готово — закрываем сплэш
            self._timer.stop()
            self.accept()
            return

        # Обычное обновление прогресса (если не остановлены для апдейта)
        self.progress.setValue(min(100, int(self._progress)))

    def _on_update_clicked(self):
        # Имитируем скачивание обновления — скрываем кнопки и продолжаем
        self.btn_update.setVisible(False)
        self.btn_skip.setVisible(False)
        self.sub.setText('Скачивание обновления...')
        self._timer.start()

    def set_theme(self, theme_name: str):
        """Применить тему динамически (может вызываться из MainWindow для соответствия текущей теме)."""
        try:
            self._theme_name = theme_name
            t = THEMES.get(theme_name, THEMES['Классическая'])
            self.gs = t.get('s', SOFT_GRAD_START)
            self.ge = t.get('e', SOFT_GRAD_END)
            self._accent = ACCENT

            # переключаем снежный эффект
            try:
                if theme_name == 'Зима':
                    if not self.effect_widget:
                        self.effect_widget = SnowEffectWidget(self)
                        self.effect_widget.setGeometry(0, 0, self.width(), self.height())
                        self.effect_widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                        self.effect_widget.raise_()
                        self.effect_widget.show()
                else:
                    if getattr(self, 'effect_widget', None):
                        try:
                            self.effect_widget.timer.stop()
                        except Exception:
                            pass
                        try:
                            self.effect_widget.hide()
                            self.effect_widget.deleteLater()
                        except Exception:
                            pass
                        self.effect_widget = None
            except Exception:
                pass

            # Обновляем прогресс-бар цвет/стиль
            try:
                self.progress.setStyleSheet(
                    f"QProgressBar{{border-radius:10px; background: rgba(255,255,255,0.06); color: white;}}"
                    f" QProgressBar::chunk{{background: {self._accent}; border-radius: 10px;}}"
                )
            except Exception:
                pass

            # Перерисовываем
            try:
                self.update()
            except Exception:
                pass

        except Exception:
            logging.exception('Не удалось применить тему к сплэшу')

    def _on_skip_clicked(self):
        # Пользователь пропускает обновление — продолжаем стартап
        self.btn_update.setVisible(False)
        self.btn_skip.setVisible(False)
        self.sub.setText('Пропущено — продолжение запуска...')
        self._timer.start()

    def resizeEvent(self, event):
        # при изменении размера пересчитываем область снежинок (если присутствует)
        try:
            if hasattr(self, 'effect_widget') and self.effect_widget:
                self.effect_widget.setGeometry(0, 0, self.width(), self.height())
                self.effect_widget.raise_()
        except Exception:
            pass
        return super().resizeEvent(event)

    def paintEvent(self, event):
        # Рисуем градиент фона согласно теме
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), 0)
        try:
            g.setColorAt(0, self.gs)
            g.setColorAt(1, self.ge)
        except Exception:
            g.setColorAt(0, SOFT_GRAD_START)
            g.setColorAt(1, SOFT_GRAD_END)
        p.fillRect(self.rect(), QBrush(g))

    # Кнопка "Обновить" — открываем внешнюю ссылку (эмуляция обновления)
    def _on_update_clicked_open(self):
        webbrowser.open('https://example.com/your-app-update')


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # Показываем предзагрузочный экран — проверка обновлений (fake) и фейковая загрузка
    try:
        splash = None
        # Конструктор сплэша сам подберёт тему из файла THEME_FILE при наличии
        splash = PreloadSplash(parent=None, theme_file=MainWindow.THEME_FILE)
        # exec() блокирует пока сплэш не завершится (подходит для простого предзагрузочного экрана)
        splash.exec()
    except Exception:
        # Если по какой-то причине сплэш не может быть показан — просто продолжаем
        logging.exception('Не удалось показать предзагрузочный экран')

    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()