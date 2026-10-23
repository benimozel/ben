#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import time
import os
import re
import json
import requests
import threading
from collections import deque
from datetime import datetime, timezone, timedelta

# ===================== AYARLAR =====================
RTMP_URL = "rtmp://ssh101.bozztv.com:1935/ssh101"
STREAM_KEY = os.getenv("STREAM_KEY") or "zirvesa"
RTMP_SERVER = f"{RTMP_URL}/{STREAM_KEY}"

# Canlı yayın kaynağı (M3U kaldırıldı)
LIVE_SOURCE_URL = os.getenv("LIVE_SOURCE_URL") or "https://v2.catcast.tv/content/50736/seg-27708703-v1-a1/index.m3u8"
STREAM_TITLE = os.getenv("STREAM_TITLE") or "Zirve TV Canlı"

LOGO_URL = os.getenv("LOGO_URL") or "https://i.hizliresim.com/on1bgkjg.png"
TELEGRAM_ICON_URL = os.getenv("TELEGRAM_ICON_URL") or "https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg"
TELEGRAM_TEXT = "t.me/Zirvesa"

STATE_FILE_NAME = os.getenv("STATE_FILE_NAME", "zirvesa.json")
GITHUB_STEP_SUMMARY = os.getenv("GITHUB_STEP_SUMMARY")

STREAM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
STREAM_REFERER = "https://ha.vixolity.com/"

# Logo ve yazı opaklık ayarları (0.0 - 1.0 arası)
LOGO_OPACITY = float(os.getenv("LOGO_OPACITY", "1.0"))
TEXT_OPACITY = float(os.getenv("TEXT_OPACITY", "1.0"))
BOLD_FONT_PATH = os.getenv("BOLD_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# Decoder'ı tek thread'e zorlamak için
DECODER_THREADS = os.getenv("DECODER_THREADS", "1")

# Watchdog Zaman Aşımları (Saniye)
WATCHDOG_STARTUP_SECONDS = int(os.getenv("WATCHDOG_STARTUP_SECONDS", "60"))
WATCHDOG_TIMEOUT_SECONDS = int(os.getenv("WATCHDOG_TIMEOUT_SECONDS", "45"))

# Türkiye saati için UTC+3
TURKEY_TZ = timezone(timedelta(hours=3))


def format_hms(total_seconds):
    total_seconds = max(0, int(total_seconds))
    hrs = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def get_turkey_time_str():
    """Türkiye saatini HH:MM:SS formatında döndürür."""
    return datetime.now(TURKEY_TZ).strftime("%H:%M:%S")


# ===================== KALICI RTMP YAYINCISI =====================
class RtmpPublisher:
    def __init__(self):
        self.proc = None
        self.started_at = 0.0
        self.tail = deque(maxlen=30)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure_running(self):
        if self.alive():
            return
        self.stop()
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-fflags', '+genpts',
            '-analyzeduration', '3000000',
            '-probesize', '3000000',
            '-f', 'mpegts', '-i', 'pipe:0',
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            '-flvflags', 'no_duration_filesize',
            '-f', 'flv',
            RTMP_SERVER
        ]
        print(f"📡 Kalıcı RTMP yayıncısı başlatılıyor => {RTMP_SERVER}")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        self.started_at = time.time()
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()

    def _drain(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b''):
                self.tail.append(raw.decode('utf-8', 'replace').rstrip())
        except Exception:
            pass

    def clock(self):
        return time.time() - self.started_at

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        self.proc = None


publisher = RtmpPublisher()


def download_file(url, filename):
    headers = {'User-Agent': STREAM_USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200 and len(response.content) > 0:
            with open(filename, 'wb') as f:
                f.write(response.content)
            print(f"✅ {filename} başarıyla indirildi.")
            return True
    except Exception as e:
        print(f"⚠️ {filename} indirme hatası: {e}")
    return False


def download_logo():
    download_file(LOGO_URL, 'logo.png')


def download_telegram_icon():
    download_file(TELEGRAM_ICON_URL, 'telegram.png')


def write_title_file(title):
    try:
        with open('title.txt', 'w', encoding='utf-8') as f:
            f.write(title)
    except Exception as e:
        print(f"⚠️ Başlık dosyası yazma hatası: {e}")


def write_telegram_text_file():
    try:
        with open('telegram_text.txt', 'w', encoding='utf-8') as f:
            f.write(TELEGRAM_TEXT)
    except Exception as e:
        print(f"⚠️ Telegram metin dosyası yazma hatası: {e}")


def write_time_file():
    """Türkiye saatini dosyaya yazar (drawtext reload için)."""
    try:
        with open('time.txt', 'w', encoding='utf-8') as f:
            f.write(get_turkey_time_str())
    except Exception as e:
        print(f"⚠️ Saat dosyası yazma hatası: {e}")


def time_updater(stop_event):
    """Her saniye time.txt dosyasını günceller."""
    while not stop_event.is_set():
        write_time_file()
        stop_event.wait(1)


def print_dashboard(title, status="🟢 Yayında"):
    print("┌" + "─" * 58 + "┐")
    print(f"│ 🎬 İçerik         : {title[:36]:<36} │")
    print(f"│ ⏱️  Türkiye Saati  : {get_turkey_time_str():<36} │")
    print(f"│ 📡 Durum          : {status:<36} │")
    print("└" + "─" * 58 + "┘")


def write_step_summary(title, status="🟢 Yayında"):
    if not GITHUB_STEP_SUMMARY:
        return
    try:
        content = (
            "## 📺 Canlı Yayın Durumu (Zirve TV)\n\n"
            "| Alan | Değer |\n"
            "|---|---|\n"
            f"| 🎬 Şu an oynayan içerik | {title} |\n"
            f"| 🕒 Türkiye Saati | {get_turkey_time_str()} |\n"
            f"| 📡 Durum | {status} |\n"
            f"| 📱 Telegram | {TELEGRAM_TEXT} |\n"
            f"| 🕒 Son güncelleme | {time.strftime('%Y-%m-%d %H:%M:%S')} |\n"
        )
        with open(GITHUB_STEP_SUMMARY, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"⚠️ Step summary yazma hatası: {e}")


def start_live_stream():
    print(f"🔧 Canlı Kaynak    : {LIVE_SOURCE_URL}")
    print(f"🔧 Logo            : {LOGO_URL}")
    print(f"🔧 Telegram İkon   : {TELEGRAM_ICON_URL}")
    print(f"🔧 RTMP hedefi     : {RTMP_SERVER}")

    download_logo()
    download_telegram_icon()
    write_title_file(STREAM_TITLE)
    write_telegram_text_file()
    write_time_file()

    # Saat güncelleyici thread
    stop_event = threading.Event()
    threading.Thread(target=time_updater, args=(stop_event,), daemon=True).start()

    consecutive_failures = 0
    MAX_RETRY_DELAY_SECONDS = 120

    while True:
        print("=" * 60)
        print("📺 Zirve TV Canlı Yayın Başlatılıyor")
        print(f"🎬 İçerik            : {STREAM_TITLE}")
        print(f"🕒 Türkiye Saati     : {get_turkey_time_str()}")

        headers_arg = f"User-Agent: {STREAM_USER_AGENT}\r\nReferer: {STREAM_REFERER}\r\n"

        input_options = [
            '-re',
            '-headers', headers_arg,
            '-protocol_whitelist', 'file,http,https,tcp,tls,crypto',
            '-err_detect', 'ignore_err',
            '-fflags', '+genpts+discardcorrupt',
            '-thread_queue_size', '1024',
            '-max_interleave_delta', '0',
            '-analyzeduration', '10000000',
            '-probesize', '10000000',
            '-reconnect', '1',
            '-reconnect_at_eof', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '10',
            '-rw_timeout', '10000000',
            '-threads', DECODER_THREADS,
        ]

        input_args = input_options + ['-i', LIVE_SOURCE_URL]

        print_dashboard(STREAM_TITLE, status="🟡 Başlatılıyor")

        has_logo1 = os.path.exists('logo.png') and os.path.getsize('logo.png') > 0
        has_telegram = os.path.exists('telegram.png') and os.path.getsize('telegram.png') > 0

        # Sol üst: başlık
        title_drawtext = (
            f"drawtext=textfile='title.txt':reload=1:fontfile='{BOLD_FONT_PATH}':"
            f"fontcolor=white@{TEXT_OPACITY}:fontsize=22:x=20:y=20"
        )

        # Sağ alt: Türkiye saati (dosyadan okunur, reload=1)
        time_drawtext = (
            f"drawtext=textfile='time.txt':reload=1:fontfile='{BOLD_FONT_PATH}':"
            f"fontcolor=white@{TEXT_OPACITY}:fontsize=20:"
            f"x=w-tw-20:y=h-th-20:box=1:boxcolor=black@0.5:boxborderw=6"
        )

        # Sol üst: Telegram yazısı (başlığın altına)
        telegram_text_drawtext = (
            f"drawtext=textfile='telegram_text.txt':reload=1:fontfile='{BOLD_FONT_PATH}':"
            f"fontcolor=white@{TEXT_OPACITY}:fontsize=18:x=20:y=55"
        )

        if has_logo1 and has_telegram:
            logo_inputs = ['-i', 'logo.png', '-i', 'telegram.png']
            filter_str = (
                # EKRANI TAM KAPLAMA: force_original_aspect_ratio=increase + crop
                '[0:v]scale=1920:1080:force_original_aspect_ratio=increase,'
                'crop=1920:1080,fps=25[main];'
                f'[1:v]scale=-2:85,format=rgba,colorchannelmixer=aa={LOGO_OPACITY}[logo1];'
                '[main][logo1]overlay=50:50[tmp1];'
                # Telegram ikonu sol üstte, başlığın yanında
                f'[2:v]scale=-2:40,format=rgba[telegram];'
                f'[tmp1]{title_drawtext}[tmp2];'
                f'[tmp2]{telegram_text_drawtext}[tmp3];'
                # Telegram ikonunu yazının soluna yerleştir (x=20, y=55)
                '[tmp3][telegram]overlay=20:55[tmp4];'
                f'[tmp4]{time_drawtext}[v]'
            )
        elif has_logo1:
            logo_inputs = ['-i', 'logo.png']
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=increase,'
                'crop=1920:1080,fps=25[main];'
                f'[1:v]scale=-2:85,format=rgba,colorchannelmixer=aa={LOGO_OPACITY}[logo1];'
                '[main][logo1]overlay=50:50[tmp1];'
                f'[tmp1]{title_drawtext}[tmp2];'
                f'[tmp2]{telegram_text_drawtext}[tmp3];'
                f'[tmp3]{time_drawtext}[v]'
            )
        else:
            logo_inputs = []
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=increase,'
                'crop=1920:1080,fps=25[main];'
                f'[main]{title_drawtext}[tmp2];'
                f'[tmp2]{telegram_text_drawtext}[tmp3];'
                f'[tmp3]{time_drawtext}[v]'
            )

        publisher.ensure_running()
        ts_offset = publisher.clock() + 1.0

        command = ['ffmpeg'] + input_args + logo_inputs + [
            '-filter_complex', filter_str,
            '-map', '[v]', '-map', '0:a:0?',
            '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
            '-r', '25', '-b:v', '2500k', '-maxrate', '2500k', '-bufsize', '3000k',
            '-g', '50', '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '44100',
            '-output_ts_offset', f'{ts_offset:.3f}', '-flush_packets', '1',
            '-f', 'mpegts', 'pipe:1'
        ]

        process = subprocess.Popen(
            command,
            stdout=publisher.proc.stdin,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        last_dashboard_time = time.time()
        stderr_tail = deque(maxlen=40)

        last_progress_time = [time.time()]
        has_started_progress = [False]

        def _watchdog(proc=process, progress_ref=last_progress_time, started_ref=has_started_progress):
            while proc.poll() is None:
                time.sleep(5)
                timeout = WATCHDOG_TIMEOUT_SECONDS if started_ref[0] else WATCHDOG_STARTUP_SECONDS
                if time.time() - progress_ref[0] > timeout:
                    print(f"🚨 Watchdog: {timeout} saniyedir veri akışı yok! Süreç kilitlendi, sonlandırılıyor.")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break

        threading.Thread(target=_watchdog, daemon=True).start()

        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break

            if line:
                stderr_tail.append(line.rstrip())

            if "time=" in line:
                now = time.time()
                last_progress_time[0] = now
                has_started_progress[0] = True

                if now - last_dashboard_time > 30:
                    print_dashboard(STREAM_TITLE)
                    write_step_summary(STREAM_TITLE)
                    last_dashboard_time = now

        print("⚠️ Canlı yayın bağlantısı koptu. Tekrar bağlanılıyor...")
        write_step_summary(STREAM_TITLE, status="🔴 Bağlantı koptu, tekrar denenecek")

        consecutive_failures += 1
        retry_delay = min(5 * (2 ** consecutive_failures), MAX_RETRY_DELAY_SECONDS)
        print(f"⚠️ {retry_delay} saniye sonra tekrar bağlanılıyor...")
        time.sleep(retry_delay)


if __name__ == "__main__":
    try:
        start_live_stream()
    except KeyboardInterrupt:
        print("\n🛑 Durduruluyor...")
    finally:
        publisher.stop()
