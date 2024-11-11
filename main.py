import asyncio
import time
from PyQt6.QtWidgets import *
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QUrl, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QPalette, QColor, QCloseEvent
from pytubefix import YouTube
import sys
import os

import requests
import json
from datetime import timedelta
from typing import Dict, List
import uuid

from youtubesearchpython import VideosSearch
from pytube import Playlist
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime
import logging
from typing import Optional, List, Tuple
from dataclasses import dataclass
import humanize
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('youtube_downloader_debug.log'),
        logging.StreamHandler()
    ]
)

@dataclass
class VideoQueueItem:
    url: str
    title: str
    duration: str
    quality: str
    thumbnail_url: str
    playlist_index: Optional[int] = None
    playlist_title: Optional[str] = None
    priority: int = 0
    retry_count: int = 0
    status: str = 'pending'
    progress: int = 0
    download_speed: str = ''
    eta: str = ''
    download_id: Optional[str] = None


class PlaylistSelectionDialog(QDialog):
    def __init__(self, playlist_info: Dict, parent=None):
        super().__init__(parent)
        self.playlist_info = playlist_info
        self.selected_videos = []
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle(f"Playlist: {self.playlist_info['title']}")
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)

        # Info header
        info_layout = QHBoxLayout()
        total_videos = self.playlist_info['total_videos']
        total_duration = timedelta(seconds=self.playlist_info['total_duration'])

        info_label = QLabel(
            f"Total Videos: {total_videos}\n"
            f"Total Duration: {str(total_duration)}\n"
            f"Playlist: {self.playlist_info['title']}"
        )
        info_layout.addWidget(info_label)
        layout.addLayout(info_layout)

        # Video list
        self.video_list = QListWidget()
        self.video_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)

        for video in self.playlist_info['videos']:
            item = QListWidgetItem(
                f"{video['playlist_index'] + 1}. {video['title']} ({video['duration']})"
            )
            item.setData(Qt.ItemDataRole.UserRole, video)
            self.video_list.addItem(item)
            item.setSelected(True)  # Select all by default

        layout.addWidget(self.video_list)

        # Selection controls
        controls_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        deselect_all_btn = QPushButton("Deselect All")
        invert_selection_btn = QPushButton("Invert Selection")

        select_all_btn.clicked.connect(self.select_all)
        deselect_all_btn.clicked.connect(self.deselect_all)
        invert_selection_btn.clicked.connect(self.invert_selection)

        controls_layout.addWidget(select_all_btn)
        controls_layout.addWidget(deselect_all_btn)
        controls_layout.addWidget(invert_selection_btn)
        layout.addLayout(controls_layout)

        # Quality selection
        quality_layout = QHBoxLayout()
        quality_label = QLabel("Download Quality:")
        self.quality_combo = QComboBox()
        self.quality_combo.addItems([
            'High Quality Pro Plus',
            '720p',
            '480p',
            '360p',
            'Audio Only'
        ])
        quality_layout.addWidget(quality_label)
        quality_layout.addWidget(self.quality_combo)
        layout.addLayout(quality_layout)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def select_all(self):
        for i in range(self.video_list.count()):
            self.video_list.item(i).setSelected(True)

    def deselect_all(self):
        for i in range(self.video_list.count()):
            self.video_list.item(i).setSelected(False)

    def invert_selection(self):
        for i in range(self.video_list.count()):
            item = self.video_list.item(i)
            item.setSelected(not item.isSelected())

    def get_selected_videos(self) -> List[VideoQueueItem]:
        selected_videos = []
        quality = self.quality_combo.currentText()

        for item in self.video_list.selectedItems():
            video_data = item.data(Qt.ItemDataRole.UserRole)
            video_item = VideoQueueItem(
                url=video_data['url'],
                title=video_data['title'],
                duration=video_data['duration'],
                quality=quality,
                thumbnail_url=video_data['thumbnail_url'],
                playlist_index=video_data['playlist_index'],
                playlist_title=video_data['playlist_title']
            )
            selected_videos.append(video_item)

        return selected_videos


class DownloadState:
    PENDING = 'pending'
    ACTIVE = 'active'
    PAUSED = 'paused'
    COMPLETED = 'completed'
    FAILED = 'failed'
    RETRYING = 'retrying'


class SmartQueueManager:
    def __init__(self):
        print("DEBUG: Initializing SmartQueueManager")
        self.active_downloads: Dict[str, VideoQueueItem] = {}
        self.pending_downloads: List[VideoQueueItem] = []
        self.paused_downloads: List[VideoQueueItem] = []
        self.completed_downloads: List[VideoQueueItem] = []
        self.failed_downloads: List[VideoQueueItem] = []

        self.max_concurrent_downloads = 3
        self.max_retry_attempts = 3
        self.retry_delay_base = 5

        self.download_progress = {}
        self._lock = threading.Lock()
        self.event_callbacks = []

        # Initialize logging
        self.logger = logging.getLogger('SmartQueue')
        self.setup_logging()
        self.download_threads = {}


    def setup_logging(self):
        self.logger.setLevel(logging.DEBUG)
        handler = logging.FileHandler('download_queue.log')
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

    def add_download(self, video_item: VideoQueueItem):
        """Add a new download to the queue with smart prioritization"""
        print(f"DEBUG: Adding download for {video_item.title}")
        try:
            with self._lock:
                video_item.priority = self._calculate_priority(video_item)
                self.pending_downloads.append(video_item)
                self._sort_queue()
                self.logger.info(f"Added new download: {video_item.title}")

            # Process queue in a separate thread to avoid blocking
            QTimer.singleShot(0, self._process_queue)
            self._notify_listeners('queue_updated', video_item)
            print(f"DEBUG: Successfully added download for {video_item.title}")

        except Exception as e:
            print(f"DEBUG: Error adding download: {str(e)}")
            self.logger.error(f"Error adding download: {str(e)}")
            raise

    def _calculate_priority(self, video_item: VideoQueueItem) -> int:
        """Calculate download priority using multiple factors"""
        priority = 0

        # Playlist priority
        if video_item.playlist_index is not None:
            priority += 1000 - video_item.playlist_index

        # Duration priority (prefer shorter videos)
        duration_seconds = self._parse_duration(video_item.duration)
        if duration_seconds < 300:  # 5 minutes
            priority += 200
        elif duration_seconds < 900:  # 15 minutes
            priority += 100

        # Quality priority
        quality_priorities = {
            'High Quality Pro Plus': 50,
            '720p': 40,
            '480p': 30,
            '360p': 20,
            'Audio Only': 10
        }
        priority += quality_priorities.get(video_item.quality, 0)

        return priority

    def _parse_duration(self, duration_str: str) -> int:
        """Convert duration string to seconds"""
        try:
            time_parts = duration_str.split(':')
            if len(time_parts) == 2:
                return int(time_parts[0]) * 60 + int(time_parts[1])
            elif len(time_parts) == 3:
                return int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
        except:
            return 0
        return 0

    def _sort_queue(self):
        """Sort the pending downloads based on priority"""
        self.pending_downloads.sort(key=lambda x: x.priority, reverse=True)

    def _process_queue(self):
        """Process the download queue intelligently"""
        print("DEBUG: Processing queue")
        try:
            with self._lock:
                while (len(self.active_downloads) < self.max_concurrent_downloads and
                       len(self.pending_downloads) > 0):
                    next_download = self.pending_downloads.pop(0)
                    print(f"DEBUG: Starting download for {next_download.title}")
                    self._start_download(next_download)

        except Exception as e:
            print(f"DEBUG: Error processing queue: {str(e)}")
            self.logger.error(f"Error processing queue: {str(e)}")

    def _start_download(self, video_item: VideoQueueItem):
        print(f"DEBUG: Starting download process for {video_item.title}")
        try:
            main_window = MainWindow.instance()
            if not main_window:
                raise Exception("Main window instance not found")

            download_path = main_window.download_manager.settings['download_path']
            print(f"DEBUG: Download path: {download_path}")

            # Create downloader
            downloader = VideoDownloader(
                video_item.url,
                video_item.quality,
                download_path
            )

            # Store downloader reference
            video_item.downloader = downloader

            # Create new thread for the downloader
            thread = QThread()
            self.download_threads[video_item.title] = thread
            downloader.moveToThread(thread)

            # Connect thread cleanup
            thread.finished.connect(thread.deleteLater)

            # Connect signals with explicit connections
            downloader.progress.connect(
                lambda p, s: self._update_progress(video_item, p, s)
            )
            downloader.finished.connect(
                lambda f, d: self._handle_download_success(video_item, f, d)
            )
            downloader.error.connect(
                lambda e: self._handle_download_error(video_item, e)
            )

            # Connect cleanup handlers
            downloader.finished.connect(lambda: self._cleanup_download(video_item))
            downloader.error.connect(lambda: self._cleanup_download(video_item))

            # Start the download
            video_item.status = DownloadState.ACTIVE
            video_item.download_id = downloader.download_id
            self.active_downloads[video_item.download_id] = video_item

            # Start thread
            thread.started.connect(downloader.run)
            thread.start()

            print(f"DEBUG: Download thread started for {video_item.title}")
            self._notify_listeners('download_started', video_item)

        except Exception as e:
            print(f"DEBUG: Error starting download: {str(e)}")
            self.logger.error(f"Failed to start download: {str(e)}")
            self._handle_download_error(video_item, str(e))

    def _cleanup_download(self, video_item: VideoQueueItem):
        """Clean up thread and resources after download"""
        print(f"DEBUG: Cleaning up download for {video_item.title}")
        try:
            if video_item.title in self.download_threads:
                thread = self.download_threads[video_item.title]
                if thread.isRunning():
                    thread.quit()
                    thread.wait()
                del self.download_threads[video_item.title]
                print(f"DEBUG: Thread cleaned up for {video_item.title}")

            # Process next download if any
            QTimer.singleShot(0, self._process_queue)

        except Exception as e:
            print(f"DEBUG: Cleanup error: {str(e)}")

    def _update_progress(self, video_item: VideoQueueItem, progress: int, status: str):
        """Update download progress and status"""
        video_item.progress = progress
        speed, eta = self._parse_status(status)
        video_item.download_speed = speed
        video_item.eta = eta
        self._notify_listeners('progress_updated', video_item)

    def _parse_status(self, status: str) -> Tuple[str, str]:
        """Parse speed and ETA from status string"""
        try:
            speed = status.split('|')[0].strip().split(': ')[1]
            eta = status.split('|')[1].strip().split(': ')[1]
            return speed, eta
        except:
            return '', ''

    def _handle_download_success(self, video_item: VideoQueueItem, folder_path: str, download_id: str):
        """Handle successful download completion"""
        print(f"DEBUG: Handling successful download for {video_item.title}")
        try:
            with self._lock:
                if download_id in self.active_downloads:
                    del self.active_downloads[download_id]

                video_item.status = DownloadState.COMPLETED
                self.completed_downloads.append(video_item)

                self.logger.info(f"Download completed: {video_item.title}")
                self._notify_listeners('download_completed', video_item)

            print(f"DEBUG: Success handled for {video_item.title}")

        except Exception as e:
            print(f"DEBUG: Error handling success: {str(e)}")
            self.logger.error(f"Error handling download success: {str(e)}")


    def _handle_download_error(self, video_item: VideoQueueItem, error: str):
        """Handle download errors with retry logic"""
        with self._lock:
            if video_item.retry_count < self.max_retry_attempts:
                video_item.retry_count += 1
                video_item.status = DownloadState.RETRYING

                retry_delay = self.retry_delay_base * (2 ** (video_item.retry_count - 1))
                self.logger.warning(
                    f"Retrying download ({video_item.retry_count}/{self.max_retry_attempts}): {video_item.title}"
                )

                QTimer.singleShot(
                    retry_delay * 1000,
                    lambda: self._retry_download(video_item)
                )
            else:
                if video_item.download_id in self.active_downloads:
                    del self.active_downloads[video_item.download_id]

                video_item.status = DownloadState.FAILED
                self.failed_downloads.append(video_item)

                self.logger.error(f"Download failed: {video_item.title} - {error}")
                self._notify_listeners('download_failed', video_item)
                self._process_queue()

    def _retry_download(self, video_item: VideoQueueItem):
        """Retry a failed download"""
        with self._lock:
            video_item.progress = 0
            video_item.download_speed = ''
            video_item.eta = ''
            self.pending_downloads.append(video_item)
            self._sort_queue()
            self._process_queue()

    def pause_download(self, download_id: str):
        """Pause a specific download"""
        with self._lock:
            if download_id in self.active_downloads:
                video_item = self.active_downloads[download_id]
                video_item.status = DownloadState.PAUSED
                self.paused_downloads.append(video_item)
                del self.active_downloads[download_id]
                self._notify_listeners('download_paused', video_item)

    def resume_download(self, download_id: str):
        """Resume a paused download"""
        with self._lock:
            for video_item in self.paused_downloads:
                if video_item.download_id == download_id:
                    self.paused_downloads.remove(video_item)
                    self.pending_downloads.append(video_item)
                    self._sort_queue()
                    self._notify_listeners('download_resumed', video_item)
                    break

    def cancel_download(self, download_id: str):
        """Cancel a download"""
        with self._lock:
            if download_id in self.active_downloads:
                video_item = self.active_downloads[download_id]
                video_item.status = DownloadState.FAILED
                self.failed_downloads.append(video_item)
                del self.active_downloads[download_id]
                self._notify_listeners('download_cancelled', video_item)

    def add_listener(self, callback):
        """Add event listener"""
        self.event_callbacks.append(callback)

    def remove_listener(self, callback):
        """Remove event listener"""
        if callback in self.event_callbacks:
            self.event_callbacks.remove(callback)

    def _notify_listeners(self, event_type: str, data=None):
        """Notify all listeners of queue events"""
        print(f"DEBUG: Notifying listeners of {event_type}")
        try:
            for callback in self.event_callbacks:
                try:
                    callback(event_type, data)
                except Exception as e:
                    print(f"DEBUG: Listener callback error: {str(e)}")
                    self.logger.error(f"Error in listener callback: {str(e)}")
        except Exception as e:
            print(f"DEBUG: Notification error: {str(e)}")
            self.logger.error(f"Error notifying listeners: {str(e)}")


class YouTubeSearchManager:
    def __init__(self):
        self.search_history = []
        self.max_history = 100
        self.results_cache = {}

    def search_videos(self, query: str, filters: Dict) -> List[Dict]:
        """Perform YouTube search with filters"""
        cache_key = f"{query}:{json.dumps(filters)}"
        if cache_key in self.results_cache:
            return self.results_cache[cache_key]

        try:
            search = VideosSearch(query, limit=50)
            results = []
            search_results = search.result()

            for result in search_results['result']:
                # Apply filters
                if not self._passes_filters(result, filters):
                    continue

                video_info = {
                    'title': result['title'],
                    'url': result['link'],
                    'duration': result.get('duration', 'Unknown'),
                    'views': result.get('viewCount', {}).get('text', 'Unknown'),
                    'thumbnail_url': result.get('thumbnails', [{}])[0].get('url', ''),
                    'channel': result.get('channel', {}).get('name', 'Unknown'),
                    'publish_date': result.get('publishedTime', 'Unknown')
                }
                results.append(video_info)

            # Cache results
            self.results_cache[cache_key] = results
            self._add_to_history(query)
            return results

        except Exception as e:
            logging.error(f"Search error: {str(e)}")
            raise

    def _passes_filters(self, result: Dict, filters: Dict) -> bool:
        """Check if video matches specified filters"""
        if filters.get('duration'):
            duration = result.get('duration', '')
            duration_seconds = self._parse_duration(duration)

            if filters['duration'] == 'Short' and duration_seconds > 240:  # 4 minutes
                return False
            elif filters['duration'] == 'Medium' and (duration_seconds < 240 or duration_seconds > 1200):
                return False
            elif filters['duration'] == 'Long' and duration_seconds < 1200:  # 20 minutes
                return False

        if filters.get('date'):
            publish_date = result.get('publishedTime', '')
            if not self._check_date_filter(publish_date, filters['date']):
                return False

        return True

    def _parse_duration(self, duration: str) -> int:
        """Convert duration string to seconds"""
        try:
            parts = duration.split(':')
            if len(parts) == 2:  # MM:SS
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:  # HH:MM:SS
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return 0
        except:
            return 0

    def _check_date_filter(self, publish_date: str, filter_date: str) -> bool:
        """Check if publish date matches the filter"""
        try:
            if filter_date == "Any Time":
                return True

            now = datetime.now()
            if not publish_date:
                return False

            if "ago" in publish_date.lower():
                # Handle relative dates like "1 month ago"
                return self._check_relative_date(publish_date, filter_date)

            # Handle absolute dates
            pub_date = datetime.strptime(publish_date, "%Y-%m-%d")
            if filter_date == "Today":
                return pub_date.date() == now.date()
            elif filter_date == "This Week":
                week_ago = now - timedelta(days=7)
                return week_ago.date() <= pub_date.date() <= now.date()
            elif filter_date == "This Month":
                month_ago = now - timedelta(days=30)
                return month_ago.date() <= pub_date.date() <= now.date()
            elif filter_date == "This Year":
                year_start = datetime(now.year, 1, 1)
                return year_start.date() <= pub_date.date() <= now.date()

        except Exception as e:
            logging.error(f"Date filter error: {str(e)}")
            return True

        return True

    def _check_relative_date(self, publish_date: str, filter_date: str) -> bool:
        """Check relative dates (e.g., '2 months ago')"""
        try:
            parts = publish_date.lower().split()
            number = int(parts[0])
            unit = parts[1]

            if filter_date == "Today":
                return unit == "hour" or (unit == "day" and number == 0)
            elif filter_date == "This Week":
                return (
                        unit == "hour" or
                        unit == "day" and number <= 7 or
                        unit == "week" and number == 0
                )
            elif filter_date == "This Month":
                return (
                        unit == "hour" or
                        unit == "day" or
                        unit == "week" and number <= 4 or
                        unit == "month" and number == 0
                )
            elif filter_date == "This Year":
                return (
                        unit == "hour" or
                        unit == "day" or
                        unit == "week" or
                        unit == "month" and number <= 12 or
                        unit == "year" and number == 0
                )

        except Exception as e:
            logging.error(f"Relative date filter error: {str(e)}")
            return True

        return True

    def _add_to_history(self, query: str):
        """Add search query to history"""
        if query not in self.search_history:
            self.search_history.insert(0, query)
            if len(self.search_history) > self.max_history:
                self.search_history.pop()


class SearchResultWidget(QWidget):
    download_requested = pyqtSignal(dict)

    def __init__(self, video_info: Dict, default_quality: str, parent=None):
        super().__init__(parent)
        self.video_info = video_info
        self.default_quality = default_quality
        self.setup_ui()

    def setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Checkbox for selection
        self.select_checkbox = QCheckBox()
        layout.addWidget(self.select_checkbox)

        # Thumbnail
        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(120, 68)
        self.thumbnail.setScaledContents(True)
        self._load_thumbnail()
        layout.addWidget(self.thumbnail)

        # Info section
        info_layout = QVBoxLayout()
        title_label = QLabel(self.video_info['title'])
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-weight: bold;")

        details_label = QLabel(
            f"Duration: {self.video_info['duration']} | "
            f"Views: {self.video_info['views']} | "
            f"Channel: {self.video_info['channel']}"
        )

        info_layout.addWidget(title_label)
        info_layout.addWidget(details_label)
        layout.addLayout(info_layout, stretch=1)

        # Quality selection
        quality_layout = QVBoxLayout()
        self.quality_combo = QComboBox()
        self.quality_combo.addItems([
            'High Quality Pro Plus',
            '720p',
            '480p',
            '360p',
            'Audio Only'
        ])
        self.quality_combo.setCurrentText(self.default_quality)
        quality_layout.addWidget(QLabel("Quality:"))
        quality_layout.addWidget(self.quality_combo)
        layout.addLayout(quality_layout)

        # Add to queue button
        self.queue_btn = QPushButton("Add to Queue")
        self.queue_btn.clicked.connect(self._add_to_queue)
        layout.addWidget(self.queue_btn)

    def _add_to_queue(self):
        """Add video to download queue"""
        try:
            video_item = VideoQueueItem(
                url=self.video_info['url'],
                title=self.video_info['title'],
                duration=self.video_info['duration'],
                quality=self.quality_combo.currentText(),
                thumbnail_url=self.video_info['thumbnail_url']
            )
            MainWindow.instance().smart_queue.add_download(video_item)
            self.queue_btn.setEnabled(False)
            self.queue_btn.setText("Added to Queue")

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not add to queue: {str(e)}")

    def _load_thumbnail(self):
        """Load thumbnail asynchronously"""

        def set_thumbnail(data):
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            self.thumbnail.setPixmap(pixmap)

        thread = ThreadPoolExecutor().submit(
            requests.get, self.video_info['thumbnail_url']
        )
        thread.add_done_callback(
            lambda f: set_thumbnail(f.result().content)
        )


class DownloadQueueWidget(QWidget):
    def __init__(self, smart_queue: SmartQueueManager, parent=None):
        super().__init__(parent)
        self.smart_queue = smart_queue
        self.download_widgets = {}
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # Queue controls
        controls_layout = QHBoxLayout()

        self.start_all_btn = QPushButton("Start All")
        self.pause_all_btn = QPushButton("Pause All")
        self.clear_completed_btn = QPushButton("Clear Completed")

        controls_layout.addWidget(self.start_all_btn)
        controls_layout.addWidget(self.pause_all_btn)
        controls_layout.addWidget(self.clear_completed_btn)
        layout.addLayout(controls_layout)

        # Queue sections
        self.active_section = self._create_queue_section("Active Downloads")
        self.pending_section = self._create_queue_section("Pending Downloads")
        self.completed_section = self._create_queue_section("Completed Downloads")

        layout.addWidget(self.active_section)
        layout.addWidget(self.pending_section)
        layout.addWidget(self.completed_section)

        # Connect signals
        self.start_all_btn.clicked.connect(self._start_all)
        self.pause_all_btn.clicked.connect(self._pause_all)
        self.clear_completed_btn.clicked.connect(self._clear_completed)

    def _start_all(self):
        """Start all pending downloads"""
        for video_item in self.smart_queue.pending_downloads:
            self.smart_queue._start_download(video_item)

    def _pause_all(self):
        """Pause all active downloads"""
        active_downloads = list(self.smart_queue.active_downloads.values())
        for video_item in active_downloads:
            self.smart_queue.pause_download(video_item.download_id)

    def _clear_completed(self):
        """Clear all completed downloads from the list"""
        # Remove completed downloads from UI
        for download_id, widget in list(self.download_widgets.items()):
            if widget.video_item.status == DownloadState.COMPLETED:
                widget.deleteLater()
                del self.download_widgets[download_id]

        # Clear completed downloads from queue
        self.smart_queue.completed_downloads.clear()

    def _create_queue_section(self, title: str) -> QGroupBox:
        """Create a collapsible section for queue items"""
        section = QGroupBox(title)
        layout = QVBoxLayout(section)
        layout.setSpacing(2)
        return section

    def update_queue_item(self, video_item: VideoQueueItem):
        """Update or create queue item widget"""
        if video_item.download_id not in self.download_widgets:
            widget = DownloadItemWidget(video_item)
            self.download_widgets[video_item.download_id] = widget

            # Add to appropriate section
            if video_item.status == DownloadState.ACTIVE:
                self.active_section.layout().addWidget(widget)
            elif video_item.status == DownloadState.PENDING:
                self.pending_section.layout().addWidget(widget)
            elif video_item.status == DownloadState.COMPLETED:
                self.completed_section.layout().addWidget(widget)
        else:
            widget = self.download_widgets[video_item.download_id]
            widget.update_status(video_item)

            # Move widget to appropriate section if status changed
            current_parent = widget.parent()
            if video_item.status == DownloadState.ACTIVE and current_parent != self.active_section:
                widget.setParent(None)
                self.active_section.layout().addWidget(widget)
            elif video_item.status == DownloadState.PENDING and current_parent != self.pending_section:
                widget.setParent(None)
                self.pending_section.layout().addWidget(widget)
            elif video_item.status == DownloadState.COMPLETED and current_parent != self.completed_section:
                widget.setParent(None)
                self.completed_section.layout().addWidget(widget)


class DownloadItemWidget(QFrame):
    def __init__(self, video_item: VideoQueueItem, parent=None):
        super().__init__(parent)
        self.video_item = video_item
        self.setup_ui()
        self.update_status(video_item)  # Initial status update

    def setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Title and details
        info_layout = QVBoxLayout()
        self.title_label = QLabel(self.video_item.title)
        self.status_label = QLabel()
        info_layout.addWidget(self.title_label)
        info_layout.addWidget(self.status_label)
        layout.addLayout(info_layout, stretch=1)

        # Progress section
        progress_layout = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.speed_label = QLabel()
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.speed_label)
        layout.addLayout(progress_layout)

        # Control buttons
        self.control_btn = QPushButton()
        self.cancel_btn = QPushButton("Cancel")
        layout.addWidget(self.control_btn)
        layout.addWidget(self.cancel_btn)

        # Connect signals
        self.cancel_btn.clicked.connect(self.cancel_download)
        self.control_btn.clicked.connect(self.toggle_download)

    def update_status(self, video_item: VideoQueueItem):
        """Update the widget based on video item status"""
        self.video_item = video_item
        self.title_label.setText(video_item.title)
        self.progress_bar.setValue(video_item.progress)

        # Update speed label if available
        if video_item.download_speed and video_item.eta:
            self.speed_label.setText(f"{video_item.download_speed} | {video_item.eta}")
        else:
            self.speed_label.clear()

        # Update control button based on status
        if video_item.status == DownloadState.PENDING:
            self.control_btn.setText("Start")
            self.control_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self.status_label.setText("Pending")
        elif video_item.status == DownloadState.ACTIVE:
            self.control_btn.setText("Pause")
            self.control_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self.status_label.setText("Downloading...")
        elif video_item.status == DownloadState.PAUSED:
            self.control_btn.setText("Resume")
            self.control_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self.status_label.setText("Paused")
        elif video_item.status == DownloadState.COMPLETED:
            self.control_btn.setText("Complete")
            self.control_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("Completed")
        elif video_item.status == DownloadState.FAILED:
            self.control_btn.setText("Retry")
            self.control_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.status_label.setText(
                f"Failed: {video_item.error_message if hasattr(video_item, 'error_message') else 'Unknown error'}")
        elif video_item.status == DownloadState.RETRYING:
            self.control_btn.setText("Retrying")
            self.control_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            self.status_label.setText("Retrying download...")

    def toggle_download(self):
        """Handle control button clicks based on current state"""
        if self.video_item.status == DownloadState.PENDING:
            MainWindow.instance().smart_queue._start_download(self.video_item)
        elif self.video_item.status == DownloadState.ACTIVE:
            MainWindow.instance().smart_queue.pause_download(self.video_item.download_id)
        elif self.video_item.status == DownloadState.PAUSED:
            MainWindow.instance().smart_queue.resume_download(self.video_item.download_id)
        elif self.video_item.status == DownloadState.FAILED:
            MainWindow.instance().smart_queue.retry_download(self.video_item)

    def cancel_download(self):
        """Cancel the download"""
        if self.video_item.download_id:
            MainWindow.instance().smart_queue.cancel_download(self.video_item.download_id)


class SmartQueue:
    def __init__(self):
        self.active_downloads = []
        self.pending_downloads = []
        self.paused_downloads = []
        self.max_concurrent_downloads = 3
        self.retry_attempts = 3
        self.retry_delay = 5  # seconds

    def add_download(self, download_info: Dict):
        """Add a new download to the queue with smart priority handling"""
        priority = self._calculate_priority(download_info)
        download_info['priority'] = priority
        download_info['retry_count'] = 0
        download_info['status'] = 'pending'

        self.pending_downloads.append(download_info)
        self._sort_queue()
        self._process_queue()

    def _calculate_priority(self, download_info: Dict) -> int:
        """Calculate download priority based on various factors"""
        priority = 0

        # Playlist items get higher priority to maintain order
        if download_info.get('playlist_index'):
            priority += 100 - download_info['playlist_index']

        # Shorter videos get slightly higher priority
        duration = download_info.get('duration', 0)
        if duration < 300:  # 5 minutes
            priority += 20

        # User-selected quality affects priority
        if download_info.get('quality') == 'High Quality Pro Plus':
            priority += 10

        return priority

    def _sort_queue(self):
        """Sort the pending downloads based on priority"""
        self.pending_downloads.sort(key=lambda x: x['priority'], reverse=True)

    def _process_queue(self):
        """Process the download queue intelligently"""
        while (len(self.active_downloads) < self.max_concurrent_downloads and
               len(self.pending_downloads) > 0):
            next_download = self.pending_downloads.pop(0)
            self.start_download(next_download)

    def start_download(self, download_info: Dict):
        """Start a download with error handling and retry logic"""
        download_info['status'] = 'active'
        self.active_downloads.append(download_info)

        # Create and configure the downloader
        downloader = VideoDownloader(
            download_info['url'],
            download_info['quality'],
            download_info['download_path']
        )

        # Add error handling and retry logic
        downloader.error.connect(lambda e: self._handle_download_error(e, download_info))
        downloader.finished.connect(lambda f, d: self._handle_download_success(download_info))

        download_info['downloader'] = downloader
        downloader.start()

    def _handle_download_error(self, error: str, download_info: Dict):
        """Handle download errors with smart retry logic"""
        if download_info['retry_count'] < self.retry_attempts:
            download_info['retry_count'] += 1
            download_info['status'] = 'retrying'

            # Schedule retry with exponential backoff
            retry_delay = self.retry_delay * (2 ** (download_info['retry_count'] - 1))
            QTimer.singleShot(retry_delay * 1000, lambda: self.retry_download(download_info))
        else:
            download_info['status'] = 'failed'
            self.active_downloads.remove(download_info)
            self._process_queue()

    def retry_download(self, download_info: Dict):
        """Retry a failed download"""
        if download_info in self.active_downloads:
            self.active_downloads.remove(download_info)
        self.add_download(download_info)

    def _handle_download_success(self, download_info: Dict):
        """Handle successful download completion"""
        if download_info in self.active_downloads:
            self.active_downloads.remove(download_info)
        self._process_queue()




class YouTubeSearch(QThread):
    results_ready = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, query: str, filters: Dict = None):
        super().__init__()
        self.query = query
        self.filters = filters or {}

    def run(self):
        try:
            # Initialize YouTube search
            search_results = []

            # Implement YouTube search using youtube-search-python or similar library
            # This is a placeholder for the actual implementation
            results = self._perform_search()

            for result in results:
                video_info = {
                    'title': result['title'],
                    'url': result['url'],
                    'thumbnail_url': result['thumbnail'],
                    'duration': result['duration'],
                    'views': result['views'],
                    'publish_date': result['publish_date']
                }
                search_results.append(video_info)

            self.results_ready.emit(search_results)

        except Exception as e:
            self.error.emit(str(e))


class PlaylistDownloader:
    def __init__(self, url: str, download_manager):
        self.url = url
        self.download_manager = download_manager
        self.videos = []

    def fetch_playlist_info(self) -> Dict:
        """Fetch playlist metadata and video information"""
        try:
            playlist = Playlist(self.url)
            videos = []

            for index, video in enumerate(playlist.videos):
                video_info = {
                    'url': video.watch_url,
                    'title': video.title,
                    'duration': str(timedelta(seconds=video.length)),
                    'thumbnail_url': video.thumbnail_url,
                    'playlist_index': index,
                    'playlist_title': playlist.title
                }
                videos.append(video_info)

            return {
                'title': playlist.title,
                'videos': videos,
                'total_videos': len(videos),
                'total_duration': sum(video.length for video in playlist.videos)
            }

        except Exception as e:
            raise Exception(f"Failed to fetch playlist: {str(e)}")


class DownloadManager:
    def __init__(self):
        self.queue = []
        self.history = self.load_history()
        self.settings = self.load_settings()

        # Create downloads directory if it doesn't exist
        os.makedirs(self.settings['download_path'], exist_ok=True)

    def load_history(self) -> List[Dict]:
        try:
            with open('download_history.json', 'r') as f:
                return json.load(f)
        except:
            return []

    def save_history(self):
        with open('download_history.json', 'w') as f:
            json.dump(self.history, f)

    def load_settings(self) -> Dict:
        try:
            with open('settings.json', 'r') as f:
                return json.load(f)
        except:
            return {
                'default_quality': 'High Quality Pro Plus',
                'download_path': os.path.join(os.path.expanduser('~'), 'Desktop', 'SYTDL - Downloads'),
                'prefer_audio': False
            }

    def save_settings(self):
        with open('settings.json', 'w') as f:
            json.dump(self.settings, f)

    def add_to_queue(self, video_info: Dict):
        self.queue.append(video_info)

    def add_to_history(self, video_info: Dict):
        self.history.append(video_info)
        self.save_history()


class VideoDownloader(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(str, str)
    error = pyqtSignal(str)

    def __init__(self, url: str, quality: str, download_path: str):
        super().__init__()
        print(f"DEBUG: Initializing VideoDownloader for URL: {url}")
        self.url = url
        self.quality = quality
        self.download_path = download_path
        self.is_cancelled = False
        self.download_id = uuid.uuid4().hex[:6].upper()
        self._yt = None
        self.start_time = None

    def run(self):
        print(f"DEBUG: Starting download process for {self.url}")
        try:
            self.start_time = time.time()
            print("DEBUG: Creating YouTube object")

            def on_progress(stream, chunk, bytes_remaining):
                if self.is_cancelled:
                    return
                try:
                    total = stream.filesize
                    downloaded = total - bytes_remaining
                    progress = int((downloaded / total) * 100)
                    speed = downloaded / (time.time() - self.start_time)
                    eta = timedelta(seconds=int(bytes_remaining / speed))
                    self.progress.emit(
                        progress,
                        f"Speed: {speed / 1024 / 1024:.1f}MB/s | ETA: {eta}"
                    )
                except Exception as e:
                    print(f"DEBUG: Progress callback error: {str(e)}")

            self._yt = YouTube(
                self.url,
                on_progress_callback=on_progress
            )
            print("DEBUG: YouTube object created successfully")

            # Create folder and download
            safe_title = "".join(c for c in self._yt.title if c.isalnum() or c in (' ', '-', '_')).rstrip()
            folder_name = f"[{self.download_id}] {safe_title}"
            video_folder = os.path.join(self.download_path, folder_name)
            os.makedirs(video_folder, exist_ok=True)
            print(f"DEBUG: Created folder: {video_folder}")

            if not self.is_cancelled:
                self._download_video(video_folder)
                print("DEBUG: Emitting finished signal")
                self.finished.emit(video_folder, self.download_id)
                print("DEBUG: Download complete")

        except Exception as e:
            print(f"DEBUG: Download error: {str(e)}")
            if not self.is_cancelled:
                self.error.emit(str(e))
        finally:
            print("DEBUG: Download process finished")

    def _download_video(self, video_folder):
        """Handle the actual download based on quality selection"""
        print(f"DEBUG: Starting download with quality: {self.quality}")
        try:
            if self.quality == 'High Quality Pro Plus':
                self._download_high_quality(video_folder)
            elif 'audio' in self.quality.lower():
                self._download_audio_only(video_folder)
            else:
                self._download_normal_quality(video_folder)
        except Exception as e:
            print(f"DEBUG: Download error: {str(e)}")
            raise

    def _download_high_quality(self, video_folder):
        try:
            print("DEBUG: Starting high quality download")
            # Video stream
            video_stream = (self._yt.streams
                            .filter(adaptive=True, only_video=True)
                            .order_by('resolution')
                            .desc()
                            .first())

            if not video_stream:
                raise Exception("No suitable video stream found")

            print(f"DEBUG: Downloading video: {video_stream.resolution}")
            video_stream.download(
                output_path=video_folder,
                filename=f"video_{video_stream.resolution}.mp4"
            )

            if self.is_cancelled:
                return

            # Audio stream
            audio_stream = (self._yt.streams
                            .filter(only_audio=True, mime_type="audio/mp4")
                            .order_by('abr')
                            .desc()
                            .first())

            if not audio_stream:
                raise Exception("No suitable audio stream found")

            print(f"DEBUG: Downloading audio: {audio_stream.abr}")
            audio_stream.download(
                output_path=video_folder,
                filename=f"audio_{audio_stream.abr}.m4a"
            )

        except Exception as e:
            print(f"DEBUG: High quality download error: {str(e)}")
            raise

    def _download_audio_only(self, video_folder):
        try:
            print("DEBUG: Starting audio-only download")
            stream = (self._yt.streams
                      .filter(only_audio=True, mime_type="audio/mp4")
                      .order_by('abr')
                      .desc()
                      .first())

            if not stream:
                raise Exception("No suitable audio stream found")

            print(f"DEBUG: Downloading audio: {stream.abr}")
            stream.download(
                output_path=video_folder,
                filename=f"audio_{stream.abr}.m4a"
            )

        except Exception as e:
            print(f"DEBUG: Audio download error: {str(e)}")
            raise

    def _download_normal_quality(self, video_folder):
        try:
            print(f"DEBUG: Starting {self.quality} download")
            stream = (self._yt.streams
                      .filter(progressive=True, resolution=self.quality)
                      .first())

            if not stream:
                raise Exception(f"No stream found for quality: {self.quality}")

            print(f"DEBUG: Downloading video: {stream.resolution}")
            stream.download(
                output_path=video_folder,
                filename=f"video_{stream.resolution}.mp4"
            )

        except Exception as e:
            print(f"DEBUG: Normal quality download error: {str(e)}")
            raise

    def cancel(self):
        print("DEBUG: Cancelling download")
        self.is_cancelled = True


class DownloadCard(QFrame):
    def __init__(self, video_info: Dict, parent=None):
        super().__init__(parent)
        self.video_info = video_info
        self.downloader = None
        self.download_id = None
        self.download_thread = None  # Add this line
        self.setup_ui()
        self.update_info(video_info)
        self.setup_connections()

    def setup_ui(self):
        layout = QHBoxLayout(self)
        self.setFrameStyle(QFrame.Shape.Box | QFrame.Shadow.Raised)

        # Thumbnail
        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(160, 90)
        self.thumbnail.setScaledContents(True)
        layout.addWidget(self.thumbnail)

        # Info section
        info_layout = QVBoxLayout()
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.details_label = QLabel()
        self.status_label = QLabel()  # Add status label
        info_layout.addWidget(self.title_label)
        info_layout.addWidget(self.details_label)
        info_layout.addWidget(self.status_label)
        layout.addLayout(info_layout, stretch=1)

        # Controls section
        controls_layout = QVBoxLayout()
        self.quality_combo = QComboBox()
        self.quality_combo.addItems([
            'High Quality Pro Plus',
            '720p',
            '480p',
            '360p',
            'Audio Only'
        ])

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)  # Hide initially
        self.download_btn = QPushButton("Download")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.hide()

        controls_layout.addWidget(self.quality_combo)
        controls_layout.addWidget(self.progress_bar)
        controls_layout.addWidget(self.download_btn)
        controls_layout.addWidget(self.cancel_btn)
        layout.addLayout(controls_layout)

    def setup_connections(self):
        self.download_btn.clicked.connect(self.start_download)
        self.cancel_btn.clicked.connect(self.cancel_download)

    def start_download(self):
        print("DEBUG: DownloadCard start_download called")
        try:
            # Update UI
            self.download_btn.setEnabled(False)
            self.cancel_btn.show()
            self.quality_combo.setEnabled(False)
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)
            self.status_label.setText("Preparing download...")

            # Create video item
            video_item = VideoQueueItem(
                url=self.video_info['url'],
                title=self.video_info['title'],
                duration=self.video_info.get('duration', 'Unknown'),
                quality=self.quality_combo.currentText(),
                thumbnail_url=self.video_info.get('thumbnail_url', '')
            )

            # Add to queue
            print("DEBUG: Adding to smart queue")
            MainWindow.instance().smart_queue.add_download(video_item)
            print("DEBUG: Successfully added to queue")

        except Exception as e:
            print(f"DEBUG: Error starting download: {str(e)}")
            self.status_label.setText(f"Error: {str(e)}")
            self.download_btn.setEnabled(True)
            self.cancel_btn.hide()

    def cancel_download(self):
        try:
            if self.downloader:
                self.downloader.is_cancelled = True
                if self.download_thread:
                    self.download_thread.quit()
                    self.download_thread.wait()
                self.reset_ui()
                self.status_label.setText("Download cancelled")
        except Exception as e:
            logging.error(f"Cancel error: {str(e)}")
            self.status_label.setText(f"Cancel error: {str(e)}")

    def update_progress(self, progress: int, status: str):
        print(f"DEBUG: Progress update - {progress}% - {status}")
        try:
            self.progress_bar.setValue(progress)
            self.status_label.setText(status)
        except Exception as e:
            print(f"DEBUG: Error in update_progress: {str(e)}")
            logging.error(f"Progress update error: {str(e)}")

    def download_finished(self, folder_path: str, download_id: str):
        print(f"DEBUG: Download finished - ID: {download_id}")
        try:
            print("DEBUG: Cleaning up thread")
            self.download_thread.quit()
            self.download_thread.wait()
            print("DEBUG: Resetting UI")
            self.reset_ui()
            self.status_label.setText(f"Download completed! [ID: {download_id}]")

            print("DEBUG: Adding to history")
            MainWindow.instance().download_manager.add_to_history({
                **self.video_info,
                'downloaded_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'folder_path': folder_path,
                'download_id': download_id
            })

            print("DEBUG: Showing completion message")
            QMessageBox.information(
                self,
                "Download Complete",
                f"Download completed successfully!\nFolder: {folder_path}"
            )

        except Exception as e:
            print(f"DEBUG: Error in download_finished: {str(e)}")
            logging.error(f"Download finish error: {str(e)}")
            self.status_label.setText(f"Error finalizing download: {str(e)}")

    def download_error(self, error: str):
        try:
            if self.download_thread:
                self.download_thread.quit()
                self.download_thread.wait()
            self.reset_ui()
            self.status_label.setText(f"Error: {error}")

            # Show error message
            QMessageBox.warning(
                self,
                "Download Error",
                f"Download failed: {error}"
            )

        except Exception as e:
            logging.error(f"Error handling error: {str(e)}")

    def reset_ui(self):
        self.download_btn.show()
        self.cancel_btn.hide()
        self.quality_combo.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)

    def update_info(self, video_info: Dict):
        try:
            self.title_label.setText(video_info.get('title', 'Unknown Title'))
            self.details_label.setText(f"Duration: {video_info.get('duration', 'Unknown')}")
            self.status_label.setText("Ready to download")

            # Load thumbnail asynchronously
            thumbnail_url = video_info.get('thumbnail_url')
            if thumbnail_url:
                self.load_thumbnail(thumbnail_url)

        except Exception as e:
            logging.error(f"Info update error: {str(e)}")
            self.status_label.setText("Error updating info")

    def load_thumbnail(self, url: str):
        def set_thumbnail(data):
            try:
                pixmap = QPixmap()
                pixmap.loadFromData(data)
                self.thumbnail.setPixmap(pixmap)
            except Exception as e:
                logging.error(f"Thumbnail load error: {str(e)}")

        try:
            # Load thumbnail in a thread pool
            ThreadPoolExecutor().submit(
                lambda: set_thumbnail(requests.get(url).content)
            )
        except Exception as e:
            logging.error(f"Thumbnail thread error: {str(e)}")


class MainWindow(QMainWindow):
    _instance = None

    @classmethod
    def instance(cls):
        return cls._instance

    def __init__(self):
        super().__init__()
        MainWindow._instance = self  # Set instance immediately
        self.download_manager = DownloadManager()
        self.search_manager = YouTubeSearchManager()
        self.smart_queue = SmartQueueManager()
        self.setup_enhanced_ui()


    def setup_enhanced_ui(self):
        self.setWindowTitle("Simit's Youtube Download Premium")
        self.setMinimumSize(1200, 800)

        # Create main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Create tab widget
        tabs = QTabWidget()
        layout.addWidget(tabs)

        # Add tabs
        tabs.addTab(self.create_search_tab(), "Search")
        tabs.addTab(self.create_downloads_tab(), "Downloads")
        tabs.addTab(self.create_history_tab(), "History")
        tabs.addTab(self.create_settings_tab(), "Settings")

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        # Setup queue manager listeners
        self.smart_queue.add_listener(self.handle_queue_event)

    def create_search_tab(self) -> QWidget:
        """Create the enhanced search tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Search area
        search_group = QGroupBox("Search or Enter YouTube URL")
        search_layout = QVBoxLayout()

        # Search controls
        search_input_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Enter YouTube URL or search for videos...")
        self.search_btn = QPushButton("Search/Fetch")
        search_input_layout.addWidget(self.search_input)
        search_input_layout.addWidget(self.search_btn)
        search_layout.addLayout(search_input_layout)

        # Quality selection
        quality_layout = QHBoxLayout()
        quality_layout.addWidget(QLabel("Default Quality:"))
        self.default_quality_combo = QComboBox()
        self.default_quality_combo.addItems([
            'High Quality Pro Plus',
            '720p',
            '480p',
            '360p',
            'Audio Only'
        ])
        quality_layout.addWidget(self.default_quality_combo)
        quality_layout.addStretch()
        search_layout.addLayout(quality_layout)

        # Filters
        filter_layout = QGridLayout()
        self.duration_combo = QComboBox()
        self.duration_combo.addItems(["Any Duration", "Short", "Medium", "Long"])
        filter_layout.addWidget(QLabel("Duration:"), 0, 0)
        filter_layout.addWidget(self.duration_combo, 0, 1)

        self.date_combo = QComboBox()
        self.date_combo.addItems([
            "Any Time", "Today", "This Week", "This Month", "This Year"
        ])
        filter_layout.addWidget(QLabel("Upload Date:"), 0, 2)
        filter_layout.addWidget(self.date_combo, 0, 3)
        search_layout.addLayout(filter_layout)

        search_group.setLayout(search_layout)
        layout.addWidget(search_group)

        # Search results
        results_group = QGroupBox("Results")
        results_layout = QVBoxLayout(results_group)

        self.results_scroll = QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_widget = QWidget()
        self.results_layout = QVBoxLayout(self.results_widget)
        self.results_scroll.setWidget(self.results_widget)
        results_layout.addWidget(self.results_scroll)

        # Queue controls
        queue_controls = QHBoxLayout()
        self.add_selected_btn = QPushButton("Queue Downloads")
        self.clear_results_btn = QPushButton("Clear Search Results")
        queue_controls.addWidget(self.add_selected_btn)
        queue_controls.addWidget(self.clear_results_btn)
        results_layout.addLayout(queue_controls)

        layout.addWidget(results_group)

        # Connect signals
        self.search_btn.clicked.connect(self._handle_input)
        self.search_input.returnPressed.connect(self._handle_input)
        self.add_selected_btn.clicked.connect(self._add_selected_to_queue)
        self.clear_results_btn.clicked.connect(self._clear_results)

        return tab

    def _handle_input(self):
        """Handle search input or URL"""
        query = self.search_input.text().strip()
        if not query:
            QMessageBox.warning(self, "Error", "Please enter a search term or YouTube URL")
            return

        if 'youtube.com' in query or 'youtu.be' in query:
            self._handle_url(query)
        else:
            self._handle_search()

    def _handle_url(self, url):
        """Handle YouTube URL"""
        try:
            yt = YouTube(url)
            video_info = {
                'url': url,
                'title': yt.title,
                'duration': str(timedelta(seconds=yt.length)),
                'thumbnail_url': yt.thumbnail_url,
                'views': 'N/A',
                'channel': yt.author,
                'publish_date': 'N/A'
            }
            # Clear existing results and show the video
            self._clear_results()

            # Create widget with current default quality
            current_quality = self.default_quality_combo.currentText()
            widget = SearchResultWidget(
                video_info=video_info,
                default_quality=current_quality
            )
            self.results_layout.addWidget(widget)
            self.search_input.clear()

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not process URL: {str(e)}")

    def _add_result_widget(self, video_info: Dict):
        """Add a result widget with quality selection"""
        widget = SearchResultWidget(video_info, self.default_quality_combo.currentText())
        self.results_layout.addWidget(widget)

    def _clear_results(self):
        """Clear search results"""
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _handle_url_download(self):
        """Handle direct URL download"""
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Error", "Please enter a YouTube URL")
            return

        try:
            # Create video info
            yt = YouTube(url)
            video_info = {
                'url': url,
                'title': yt.title,
                'duration': str(timedelta(seconds=yt.length)),
                'thumbnail_url': yt.thumbnail_url
            }

            # Create video item
            video_item = VideoQueueItem(
                url=video_info['url'],
                title=video_info['title'],
                duration=video_info['duration'],
                quality=self.download_manager.settings['default_quality'],
                thumbnail_url=video_info['thumbnail_url']
            )

            # Add to queue
            self.smart_queue.add_download(video_item)
            self.status_bar.showMessage(f"Added to queue: {video_item.title}", 2000)
            self.url_input.clear()

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not process URL: {str(e)}")

    def setup_search_features(self):
        """Setup the advanced search interface"""
        search_widget = QWidget()
        search_layout = QVBoxLayout(search_widget)

        # Search controls
        search_input = QLineEdit()
        search_input.setPlaceholderText("Search YouTube videos...")
        search_btn = QPushButton("Search")

        # Filters
        filter_group = QGroupBox("Search Filters")
        filter_layout = QFormLayout()

        self.duration_combo = QComboBox()
        self.duration_combo.addItems(["Any", "Short", "Medium", "Long"])

        self.date_combo = QComboBox()
        self.date_combo.addItems(["Any time", "Today", "This week", "This month"])

        filter_layout.addRow("Duration:", self.duration_combo)
        filter_layout.addRow("Upload date:", self.date_combo)
        filter_group.setLayout(filter_layout)

        # Add to layout
        search_layout.addWidget(search_input)
        search_layout.addWidget(search_btn)
        search_layout.addWidget(filter_group)

        # Add to tabs
        self.tabs.insertTab(0, search_widget, "Search")

        # Connect signals
        search_btn.clicked.connect(lambda: self.perform_search(search_input.text()))

    def add_playlist(self, url: str):
        """Add a playlist for download"""
        try:
            playlist_downloader = PlaylistDownloader(url, self.download_manager)
            playlist_info = playlist_downloader.fetch_playlist_info()

            # Show playlist dialog
            dialog = PlaylistSelectionDialog(playlist_info, self)
            if dialog.exec():
                selected_videos = dialog.get_selected_videos()
                for video in selected_videos:
                    self.smart_queue.add_download(video)

        except Exception as e:
            QMessageBox.warning(self, "Playlist Error", str(e))

    def create_downloads_tab(self) -> QWidget:
        """Create the enhanced downloads tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Queue widget
        self.queue_widget = DownloadQueueWidget(self.smart_queue)
        layout.addWidget(self.queue_widget)

        return tab

    def _add_selected_to_queue(self):
        """Add all selected videos to the download queue"""
        try:
            selected_count = 0
            for i in range(self.results_layout.count()):
                widget = self.results_layout.itemAt(i).widget()
                if isinstance(widget, SearchResultWidget) and widget.select_checkbox.isChecked():
                    # Create video item
                    video_item = VideoQueueItem(
                        url=widget.video_info['url'],
                        title=widget.video_info['title'],
                        duration=widget.video_info['duration'],
                        quality=widget.quality_combo.currentText(),
                        thumbnail_url=widget.video_info['thumbnail_url']
                    )
                    # Add to queue
                    self.smart_queue.add_download(video_item)
                    # Update widget
                    widget.queue_btn.setEnabled(False)
                    widget.queue_btn.setText("Added to Queue")
                    widget.select_checkbox.setChecked(False)
                    selected_count += 1

            if selected_count > 0:
                self.status_bar.showMessage(f"Added {selected_count} videos to queue", 2000)
            else:
                QMessageBox.information(self, "No Selection", "Please select videos to add to queue")

        except Exception as e:
            logging.error(f"Error adding selected videos to queue: {str(e)}")
            QMessageBox.warning(self, "Error", f"Could not add videos to queue: {str(e)}")

    def create_history_tab(self) -> QWidget:
        """Create the enhanced history tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Search history
        search_group = QGroupBox("Search History")
        search_layout = QVBoxLayout(search_group)
        self.search_history_list = QListWidget()
        search_layout.addWidget(self.search_history_list)
        layout.addWidget(search_group)

        # Download history
        download_group = QGroupBox("Download History")
        download_layout = QVBoxLayout(download_group)
        self.download_history_list = QListWidget()
        download_layout.addWidget(self.download_history_list)
        layout.addWidget(download_group)

        return tab

    def create_settings_tab(self) -> QWidget:
        """Create the enhanced settings tab"""
        tab = QWidget()
        layout = QFormLayout(tab)

        # Download settings
        self.max_downloads_spin = QSpinBox()
        self.max_downloads_spin.setRange(1, 10)
        self.max_downloads_spin.setValue(self.smart_queue.max_concurrent_downloads)
        layout.addRow("Max Concurrent Downloads:", self.max_downloads_spin)

        # Retry settings
        self.max_retries_spin = QSpinBox()
        self.max_retries_spin.setRange(0, 5)
        self.max_retries_spin.setValue(self.smart_queue.max_retry_attempts)
        layout.addRow("Max Retry Attempts:", self.max_retries_spin)

        # Path settings
        path_layout = QHBoxLayout()
        self.download_path_input = QLineEdit(
            self.download_manager.settings['download_path']
        )
        self.browse_btn = QPushButton("Browse")
        path_layout.addWidget(self.download_path_input)
        path_layout.addWidget(self.browse_btn)
        layout.addRow("Download Path:", path_layout)

        # Save button
        self.save_settings_btn = QPushButton("Save Settings")
        layout.addRow(self.save_settings_btn)

        # Connect signals
        self.browse_btn.clicked.connect(self.browse_download_path)
        self.save_settings_btn.clicked.connect(self.save_settings)

        return tab

    def _handle_search(self):
        """Handle search button click and enter key"""
        query = self.search_input.text().strip()
        if not query:
            return

        try:
            self.search_btn.setEnabled(False)
            self.status_bar.showMessage("Searching...")

            filters = {
                'duration': self.duration_combo.currentText(),
                'date': self.date_combo.currentText()
            }

            # Create event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                # Run search
                results = self.search_manager.search_videos(query, filters)
                # Display results
                self.display_search_results(results)
                self.search_input.clear()
            finally:
                loop.close()

        except Exception as e:
            QMessageBox.warning(self, "Search Error", str(e))
        finally:
            self.search_btn.setEnabled(True)
            self.status_bar.clearMessage()

    async def perform_search(self):
        """Perform YouTube search"""
        query = self.search_input.text().strip()
        if not query:
            return

        try:
            self.search_btn.setEnabled(False)
            self.status_bar.showMessage("Searching...")

            filters = {
                'duration': self.duration_combo.currentText(),
                'date': self.date_combo.currentText()
            }

            # Create a worker thread for the search
            def do_search():
                return self.search_manager.search_videos(query, filters)

            # Run the search in a thread pool
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, do_search)

            # Update UI with results
            self.display_search_results(results)

        except Exception as e:
            QMessageBox.warning(self, "Search Error", str(e))
        finally:
            self.search_btn.setEnabled(True)
            self.status_bar.clearMessage()

    def display_search_results(self, results: List[Dict]):
        """Display search results"""
        try:
            # Clear previous results
            self._clear_results()

            # Add new results
            for result in results:
                # Get current default quality from the combo box
                current_quality = self.default_quality_combo.currentText()

                # Create widget with the current default quality
                widget = SearchResultWidget(
                    video_info=result,
                    default_quality=current_quality
                )
                self.results_layout.addWidget(widget)

            # Add stretch at the end
            self.results_layout.addStretch()

        except Exception as e:
            logging.error(f"Error displaying search results: {str(e)}")
            QMessageBox.warning(self, "Error", f"Could not display results: {str(e)}")

    def handle_queue_event(self, event_type: str, data):
        """Handle queue manager events"""
        try:
            if event_type == 'queue_updated':
                self.queue_widget.update_queue_item(data)
                self.status_bar.showMessage("Queue updated", 2000)

            elif event_type == 'download_started':
                self.status_bar.showMessage(f"Started downloading: {data.title}", 2000)
                self.queue_widget.update_queue_item(data)

            elif event_type == 'download_completed':
                self.status_bar.showMessage(f"Download completed: {data.title}", 5000)
                self.queue_widget.update_queue_item(data)
                self.update_history()

                # Show notification
                if hasattr(self, 'tray_icon'):
                    self.tray_icon.showMessage(
                        "Download Complete",
                        f"{data.title} has been downloaded successfully",
                        QSystemTrayIcon.MessageIcon.Information,
                        3000
                    )

            elif event_type == 'download_failed':
                self.status_bar.showMessage(f"Download failed: {data.title}", 5000)
                self.queue_widget.update_queue_item(data)

                # Show error notification
                if hasattr(self, 'tray_icon'):
                    self.tray_icon.showMessage(
                        "Download Failed",
                        f"Failed to download {data.title}",
                        QSystemTrayIcon.MessageIcon.Critical,
                        3000
                    )

            elif event_type == 'download_paused':
                self.status_bar.showMessage(f"Download paused: {data.title}", 2000)
                self.queue_widget.update_queue_item(data)

            elif event_type == 'download_resumed':
                self.status_bar.showMessage(f"Download resumed: {data.title}", 2000)
                self.queue_widget.update_queue_item(data)

            elif event_type == 'progress_updated':
                self.queue_widget.update_queue_item(data)

                # Update status bar with overall progress
                active_downloads = len(self.smart_queue.active_downloads)
                if active_downloads > 0:
                    total_progress = sum(
                        item.progress for item in self.smart_queue.active_downloads.values()
                    )
                    avg_progress = total_progress / active_downloads
                    self.status_bar.showMessage(
                        f"Overall progress: {avg_progress:.1f}% | Active downloads: {active_downloads}"
                    )

            elif event_type == 'download_cancelled':
                self.status_bar.showMessage(f"Download cancelled: {data.title}", 2000)
                self.queue_widget.update_queue_item(data)

        except Exception as e:
            logging.error(f"Error handling queue event: {str(e)}")
            self.status_bar.showMessage(f"Error: {str(e)}", 5000)

    def add_to_queue(self, video_info: Dict):
        """Add video to download queue"""
        try:
            video_item = VideoQueueItem(
                url=video_info['url'],
                title=video_info['title'],
                duration=video_info.get('duration', 'Unknown'),
                quality=self.download_manager.settings['default_quality'],
                thumbnail_url=video_info.get('thumbnail_url', ''),
                playlist_index=video_info.get('playlist_index'),
                playlist_title=video_info.get('playlist_title')
            )

            self.smart_queue.add_download(video_item)
            self.status_bar.showMessage(f"Added to queue: {video_item.title}", 2000)

        except Exception as e:
            logging.error(f"Error adding to queue: {str(e)}")
            QMessageBox.warning(self, "Error", f"Could not add to queue: {str(e)}")

    def cancel_all_downloads(self):
        """Cancel all active downloads"""
        try:
            active_downloads = list(self.smart_queue.active_downloads.values())
            for video_item in active_downloads:
                self.smart_queue.cancel_download(video_item.download_id)
            self.status_bar.showMessage("All downloads cancelled", 2000)

        except Exception as e:
            logging.error(f"Error cancelling downloads: {str(e)}")
            self.status_bar.showMessage(f"Error: {str(e)}", 5000)

    def setup_downloads_tab(self):
        layout = QVBoxLayout(self.downloads_tab)

        # Downloads area
        self.downloads_scroll = QScrollArea()
        self.downloads_scroll.setWidgetResizable(True)
        self.downloads_content = QWidget()
        self.downloads_layout = QVBoxLayout(self.downloads_content)
        self.downloads_scroll.setWidget(self.downloads_content)
        layout.addWidget(self.downloads_scroll)

        # Queue controls
        queue_layout = QHBoxLayout()
        self.start_all_btn = QPushButton("Start All")
        self.pause_all_btn = QPushButton("Pause All")
        self.clear_all_btn = QPushButton("Clear All")
        queue_layout.addWidget(self.start_all_btn)
        queue_layout.addWidget(self.pause_all_btn)
        queue_layout.addWidget(self.clear_all_btn)
        layout.addLayout(queue_layout)

        # Connect queue control signals
        self.start_all_btn.clicked.connect(self.start_all_downloads)
        self.pause_all_btn.clicked.connect(self.pause_all_downloads)
        self.clear_all_btn.clicked.connect(self.clear_all_downloads)

    def setup_history_tab(self):
        layout = QVBoxLayout(self.history_tab)

        # History list with better formatting
        self.history_list = QListWidget()
        self.history_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.history_list)

        # Clear history button
        self.clear_history_btn = QPushButton("Clear History")
        self.clear_history_btn.clicked.connect(self.clear_history)
        layout.addWidget(self.clear_history_btn)

    def setup_settings_tab(self):
        layout = QFormLayout(self.settings_tab)

        # Download path setting
        path_layout = QHBoxLayout()
        self.download_path_input = QLineEdit(self.download_manager.settings['download_path'])
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_download_path)
        path_layout.addWidget(self.download_path_input)
        path_layout.addWidget(browse_btn)
        layout.addRow("Download Path:", path_layout)

        # Default quality setting
        self.quality_combo = QComboBox()
        self.quality_combo.setCurrentText(self.download_manager.settings['default_quality'])
        layout.addRow("Default Quality:", self.quality_combo)

        # Prefer audio setting
        self.prefer_audio_check = QCheckBox()
        self.prefer_audio_check.setChecked(self.download_manager.settings['prefer_audio'])
        layout.addRow("Prefer Audio:", self.prefer_audio_check)

        # Save button
        save_btn = QPushButton("Save Settings")
        save_btn.clicked.connect(self.save_settings)
        layout.addRow(save_btn)

    def start_all_downloads(self):
        for i in range(self.downloads_layout.count()):
            widget = self.downloads_layout.itemAt(i).widget()
            if isinstance(widget, DownloadCard):
                widget.start_download()

    def pause_all_downloads(self):
        for i in range(self.downloads_layout.count()):
            widget = self.downloads_layout.itemAt(i).widget()
            if isinstance(widget, DownloadCard):
                widget.cancel_download()

    def clear_all_downloads(self):
        while self.downloads_layout.count():
            item = self.downloads_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def update_history_list(self):
        self.history_list.clear()
        for download in reversed(self.download_manager.history):
            download_id = download.get('download_id', 'N/A')
            title = download.get('title', 'Unknown')
            date = download.get('downloaded_at', 'Unknown date')

            # Format the history item text
            item_text = f"[{download_id}] {title} - {date}"

            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, download)

            # Optional: Add tooltip with more information
            tooltip = (f"ID: {download_id}\n"
                       f"Title: {title}\n"
                       f"Downloaded: {date}\n"
                       f"Path: {download.get('folder_path', 'Unknown')}")
            item.setToolTip(tooltip)

            self.history_list.addItem(item)

    def clear_history(self):
        reply = QMessageBox.question(
            self,
            "Clear History",
            "Are you sure you want to clear the download history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.download_manager.history.clear()
            self.download_manager.save_history()
            self.update_history_list()


    def fetch_videos(self):
        urls = self.url_input.text().strip().split('\n')
        for url in urls:
            try:
                yt = YouTube(url.strip())
                video_info = {
                    'url': url,
                    'title': yt.title,
                    'duration': str(timedelta(seconds=yt.length)),
                    'thumbnail_url': yt.thumbnail_url
                }
                download_card = DownloadCard(video_info, self)
                self.downloads_layout.addWidget(download_card)
                self.download_manager.add_to_queue(video_info)
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Could not fetch video info: {str(e)}")

        self.url_input.clear()

    def update_history(self):
        """Update history lists"""
        # Update search history
        self.search_history_list.clear()
        for query in self.search_manager.search_history:
            self.search_history_list.addItem(query)

        # Update download history
        self.download_history_list.clear()
        for download in reversed(self.download_manager.history):
            self.download_history_list.addItem(
                f"[{download['download_id']}] {download['title']}"
            )

    def browse_download_path(self):
        """Open directory browser for download path"""
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Download Directory",
            self.download_path_input.text()
        )
        if path:
            self.download_path_input.setText(path)

    def save_settings(self):
        """Save application settings"""
        self.smart_queue.max_concurrent_downloads = self.max_downloads_spin.value()
        self.smart_queue.max_retry_attempts = self.max_retries_spin.value()

        self.download_manager.settings.update({
            'download_path': self.download_path_input.text(),
            'default_quality': self.quality_combo.currentText()
        })
        self.download_manager.save_settings()

        QMessageBox.information(self, "Success", "Settings saved successfully!")

    def closeEvent(self, event: QCloseEvent):
        """Handle application closure"""
        reply = QMessageBox.question(
            self,
            "Confirm Exit",
            "Are you sure you want to exit? Active downloads will be cancelled.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.smart_queue.cancel_all_downloads()
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Create and set the dark theme palette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)

    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
