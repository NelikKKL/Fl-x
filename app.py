#!/usr/bin/env python3
"""
WebCopy — сервис для копирования сайтов в ZIP-архив.
Запуск: python app.py
Сервис будет доступен по адресу: http://localhost:5000
"""

import os
import re
import io
import time
import uuid
import zipfile
import mimetypes
import logging
from urllib.parse import urljoin, urlparse, urlunparse
from collections import deque

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_file, render_template_from_string, send_from_directory

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_FILES       = 200          # максимум файлов за одну копию
REQUEST_TIMEOUT = 15           # секунды на каждый запрос
MAX_FILE_SIZE   = 10 * 1024 * 1024  # 10 MB на файл
SKIP_EXTS       = {".pdf", ".exe", ".zip", ".tar", ".gz", ".mp4", ".avi", ".mov"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str | None:
    """Добавляет схему если отсутствует, валидирует URL."""
    url = url.strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return urlunparse(parsed)


def safe_filename(url: str, default_ext: str = ".bin") -> str:
    """Превращает URL в безопасное имя файла."""
    parsed = urlparse(url)
    path   = parsed.path.lstrip("/") or "index.html"
    # Убираем query/fragment из имени
    path   = re.sub(r"[?#].*$", "", path)
    path   = re.sub(r'[\\:*?"<>|]', "_", path)
    if "." not in os.path.basename(path):
        path = path.rstrip("/") + "/index.html"
    return path


def guess_ext(content_type: str, url: str) -> str:
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    if not ext:
        ext = os.path.splitext(urlparse(url).path)[1]
    return ext


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, stream=True, headers=HEADERS)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning("Fetch error %s: %s", url, e)
        return None


# ─── Core copy logic ──────────────────────────────────────────────────────────

def copy_site(
    start_url:       str,
    crawl_all_pages: bool = False,
    rename_files:    bool = False,
    mobile_version:  bool = False,
) -> tuple[io.BytesIO, int]:
    """
    Скачивает страницу (или весь сайт) и упаковывает в ZIP.
    Возвращает (bytes_buffer, файлов_скачано).
    """
    session = requests.Session()
    if mobile_version:
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Mobile Safari/537.36"
            )
        })

    parsed_start = urlparse(start_url)
    base_domain  = parsed_start.netloc

    visited:    set[str]        = set()
    url_to_path: dict[str, str] = {}   # original_url -> zip path
    zip_buffer  = io.BytesIO()
    files_count = 0

    queue: deque[str] = deque([start_url])

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:

        def add_resource(res_url: str) -> str | None:
            """Скачивает ресурс, кладёт в ZIP, возвращает zip-путь."""
            nonlocal files_count
            if res_url in url_to_path:
                return url_to_path[res_url]
            if files_count >= MAX_FILES:
                return None

            r = fetch(session, res_url)
            if r is None:
                return None

            # Проверяем размер
            content = b""
            for chunk in r.iter_content(65536):
                content += chunk
                if len(content) > MAX_FILE_SIZE:
                    log.warning("File too large, skipping: %s", res_url)
                    return None

            zip_path = safe_filename(res_url)
            if rename_files:
                ext      = os.path.splitext(zip_path)[1] or ".bin"
                zip_path = f"files/{uuid.uuid4().hex}{ext}"

            url_to_path[res_url] = zip_path
            zf.writestr(zip_path, content)
            files_count += 1
            log.info("[%d] Saved: %s → %s", files_count, res_url, zip_path)
            return zip_path

        while queue:
            current_url = queue.popleft()
            if current_url in visited:
                continue
            visited.add(current_url)

            r = fetch(session, current_url)
            if r is None:
                continue

            content_type = r.headers.get("content-type", "text/html")

            # Не HTML — просто сохраняем
            if "text/html" not in content_type:
                content = r.content
                if len(content) <= MAX_FILE_SIZE:
                    zip_path = safe_filename(current_url)
                    url_to_path[current_url] = zip_path
                    zf.writestr(zip_path, content)
                    files_count += 1
                continue

            # HTML — парсим
            html_bytes = r.content
            soup       = BeautifulSoup(html_bytes, "html.parser")

            # ── CSS ──────────────────────────────────────────────────────────
            for tag in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
                href = tag.get("href")
                if not href:
                    continue
                abs_url = urljoin(current_url, href)
                zip_path = add_resource(abs_url)
                if zip_path:
                    tag["href"] = zip_path

            # ── JS ───────────────────────────────────────────────────────────
            for tag in soup.find_all("script", src=True):
                abs_url  = urljoin(current_url, tag["src"])
                zip_path = add_resource(abs_url)
                if zip_path:
                    tag["src"] = zip_path

            # ── Images ───────────────────────────────────────────────────────
            for tag in soup.find_all(["img", "source"], src=True):
                abs_url  = urljoin(current_url, tag["src"])
                zip_path = add_resource(abs_url)
                if zip_path:
                    tag["src"] = zip_path
            for tag in soup.find_all("img", srcset=True):
                tag["srcset"] = ""   # убираем srcset для упрощения

            # ── Fonts / others via <link> ─────────────────────────────────
            for tag in soup.find_all("link", href=True):
                href    = tag.get("href", "")
                abs_url = urljoin(current_url, href)
                ext     = os.path.splitext(urlparse(abs_url).path)[1].lower()
                if ext in (".woff", ".woff2", ".ttf", ".eot", ".otf"):
                    zip_path = add_resource(abs_url)
                    if zip_path:
                        tag["href"] = zip_path

            # ── Внутренние страницы (если crawl_all_pages) ────────────────
            if crawl_all_pages:
                for a in soup.find_all("a", href=True):
                    href    = a["href"]
                    abs_url = urljoin(current_url, href)
                    p       = urlparse(abs_url)
                    if p.netloc == base_domain and abs_url not in visited:
                        ext = os.path.splitext(p.path)[1].lower()
                        if ext not in SKIP_EXTS:
                            queue.append(abs_url)

            # Сохраняем HTML
            page_path = "index.html" if current_url == start_url else safe_filename(current_url)
            url_to_path[current_url] = page_path
            zf.writestr(page_path, str(soup))
            files_count += 1
            log.info("[%d] HTML saved: %s", files_count, current_url)

    zip_buffer.seek(0)
    return zip_buffer, files_count


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Отдаём frontend."""
    return send_from_directory("static", "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


@app.route("/api/copy", methods=["POST"])
def api_copy():
    """
    POST /api/copy
    Body JSON: {
        "url": "https://example.com",
        "crawl_all_pages": false,   // optional
        "rename_files": false,       // optional
        "mobile_version": false      // optional
    }
    Returns: ZIP file или JSON с ошибкой.
    """
    data = request.get_json(silent=True) or {}
    raw_url = data.get("url", "").strip()

    # Валидация
    if not raw_url:
        return jsonify({"error": "url_is_empty", "message": "Адрес сайта не может быть пустым"}), 400

    url = normalize_url(raw_url)
    if not url:
        return jsonify({"error": "url_is_not_valid", "message": "Адрес сайта некорректный"}), 400

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return jsonify({"error": "protocol_is_not_valid", "message": "Поддерживаются только http и https"}), 400
    if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return jsonify({"error": "localhost_is_not_supported", "message": "Локальные сайты не поддерживаются"}), 400

    crawl_all_pages = bool(data.get("crawl_all_pages", False))
    rename_files    = bool(data.get("rename_files",    False))
    mobile_version  = bool(data.get("mobile_version",  False))

    log.info("Copy request: url=%s crawl=%s rename=%s mobile=%s",
             url, crawl_all_pages, rename_files, mobile_version)

    try:
        t0              = time.time()
        zip_buf, count  = copy_site(url, crawl_all_pages, rename_files, mobile_version)
        elapsed         = round(time.time() - t0, 2)
        log.info("Done in %.2fs, files=%d", elapsed, count)
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "dns_not_resolved", "message": "Сайт недоступен — не удалось подключиться"}), 502
    except requests.exceptions.Timeout:
        return jsonify({"error": "timeout", "message": "Сайт не ответил вовремя"}), 504
    except Exception as e:
        log.exception("Unexpected error")
        return jsonify({"error": "something_went_wrong", "message": str(e)}), 500

    # Формируем имя файла для скачивания
    site_name = parsed.netloc.replace(".", "_")
    zip_name  = f"{site_name}.zip"

    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name,
    )


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    print("\n🌐  WebCopy запущен → http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
