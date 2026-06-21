from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sqlite3
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

# 導入 PostgreSQL 驅動 (如果 DATABASE_URL 存在)
try:
    import psycopg2
except ImportError:
    psycopg2 = None

from keystroke_verifier import BaselineNotFoundError, KeystrokeVerifier

PROJECT_ROOT = Path(__file__).resolve().parent
THRESHOLD_CONFIG_PATH = PROJECT_ROOT / "threshold_config.json"

# 自動偵測資料庫配置
DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = DATABASE_URL is not None and DATABASE_URL.startswith("postgresql")

# SQLite 本地路徑預設
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", PROJECT_ROOT / "keystroke_dynamics.db"))
PROCESSED_DIR = Path(os.environ.get("PROCESSED_DIR", PROJECT_ROOT / "processed_baselines"))

verifier = KeystrokeVerifier(project_root=PROJECT_ROOT, processed_dir=PROCESSED_DIR)
verify_lock = threading.Lock()


def get_db_connection():
    """獲取資料庫連線 (Postgres 或 SQLite)"""
    if IS_POSTGRES:
        if psycopg2 is None:
            raise ImportError("環境中未安裝 psycopg2-binary，無法連接 PostgreSQL！")
        return psycopg2.connect(DATABASE_URL)
    else:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def q(sql: str) -> str:
    """自動調整 SQL 語法佔位符。SQLite 使用 '?'，Postgres 使用 '%s'"""
    if IS_POSTGRES:
        return sql.replace("?", "%s")
    return sql


def init_db():
    """初始化資料表結構與預設閾值"""
    if IS_POSTGRES:
        if psycopg2 is None:
            print("警告：未安裝 psycopg2，無法初始化遠端 PostgreSQL 資料表！")
            return
        
        # 雲端 Postgres 建表
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # 1. user_profiles
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50) NOT NULL,
                    language VARCHAR(10) NOT NULL,
                    keystrokes_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profiles_user_lang ON user_profiles(user_id, language)')
            
            # 2. user_thresholds
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_thresholds (
                    user_id VARCHAR(50) NOT NULL,
                    language VARCHAR(10) NOT NULL,
                    final_threshold DOUBLE PRECISION NOT NULL,
                    continuous_threshold DOUBLE PRECISION NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, language)
                )
            ''')
            
            # 3. verification_sessions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS verification_sessions (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(50) NOT NULL,
                    session_id VARCHAR(100) NOT NULL,
                    language VARCHAR(10) NOT NULL,
                    article_character_count INTEGER NOT NULL,
                    keystroke_count INTEGER NOT NULL,
                    final_score DOUBLE PRECISION NOT NULL,
                    is_genuine BOOLEAN NOT NULL,
                    keystrokes_json TEXT NOT NULL,
                    continuous_results_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 寫入預設閾值
            cursor.execute("SELECT 1 FROM user_thresholds WHERE user_id = 'default' AND language = 'ZH'")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO user_thresholds (user_id, language, final_threshold, continuous_threshold) VALUES ('default', 'ZH', 8.25, 10.9)"
                )
            cursor.execute("SELECT 1 FROM user_thresholds WHERE user_id = 'default' AND language = 'EN'")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO user_thresholds (user_id, language, final_threshold, continuous_threshold) VALUES ('default', 'EN', 8.25, 10.9)"
                )
                
            # 導入舊 JSON 設定 (如果有)
            _import_legacy_thresholds_to_db(cursor)
            conn.commit()
            print("[+] PostgreSQL 雲端資料庫初始化完成！")
        finally:
            conn.close()
    else:
        # SQLite 建表
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            
            # 1. user_profiles
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    keystrokes_json TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profiles_user_lang ON user_profiles(user_id, language)')
            
            # 2. user_thresholds
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_thresholds (
                    user_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    final_threshold REAL NOT NULL,
                    continuous_threshold REAL NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, language)
                )
            ''')
            
            # 3. verification_sessions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS verification_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    article_character_count INTEGER NOT NULL,
                    keystroke_count INTEGER NOT NULL,
                    final_score REAL NOT NULL,
                    is_genuine INTEGER NOT NULL,
                    keystrokes_json TEXT NOT NULL,
                    continuous_results_json TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 寫入預設閾值
            cursor.execute("SELECT 1 FROM user_thresholds WHERE user_id = 'default' AND language = 'ZH'")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO user_thresholds (user_id, language, final_threshold, continuous_threshold) VALUES ('default', 'ZH', 8.25, 10.9)"
                )
            cursor.execute("SELECT 1 FROM user_thresholds WHERE user_id = 'default' AND language = 'EN'")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO user_thresholds (user_id, language, final_threshold, continuous_threshold) VALUES ('default', 'EN', 8.25, 10.9)"
                )
                
            _import_legacy_thresholds_to_db(cursor)
            conn.commit()
            print(f"[+] 本地 SQLite 資料庫初始化完成：{DATABASE_PATH.name}")
        finally:
            conn.close()


def _import_legacy_thresholds_to_db(cursor):
    """將舊的 JSON 閾值設定導入至目前資料表中"""
    try:
        if THRESHOLD_CONFIG_PATH.is_file():
            with THRESHOLD_CONFIG_PATH.open("r", encoding="utf-8") as f:
                config = json.load(f)
            users = config.get("users", {})
            if isinstance(users, dict):
                for u_id, lang_cfg in users.items():
                    if isinstance(lang_cfg, dict):
                        for lang, cfg in lang_cfg.items():
                            final_val = cfg.get("final") or cfg.get("final_threshold")
                            cont_val = cfg.get("continuous") or cfg.get("continuous_threshold")
                            if final_val is not None and cont_val is not None:
                                if IS_POSTGRES:
                                    cursor.execute(
                                        """
                                        INSERT INTO user_thresholds (user_id, language, final_threshold, continuous_threshold) 
                                        VALUES (%s, %s, %s, %s)
                                        ON CONFLICT (user_id, language) 
                                        DO UPDATE SET final_threshold = EXCLUDED.final_threshold, continuous_threshold = EXCLUDED.continuous_threshold
                                        """,
                                        (u_id, lang.upper(), float(final_val), float(cont_val))
                                    )
                                else:
                                    cursor.execute(
                                        "INSERT OR REPLACE INTO user_thresholds (user_id, language, final_threshold, continuous_threshold) VALUES (?, ?, ?, ?)",
                                        (u_id, lang.upper(), float(final_val), float(cont_val))
                                    )
    except Exception as e:
        print(f"導入舊設定時發生錯誤 (可忽略)：{e}")


def resolve_user_id(user_id: str, language: Optional[str] = None) -> str:
    """在資料庫中尋找與輸入相符的用戶，忽略大小寫"""
    requested = str(user_id).strip()
    if not requested:
        return requested
    lang = (language or "ZH").upper()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        sql = q("SELECT DISTINCT user_id FROM user_profiles WHERE LOWER(user_id) = LOWER(?) AND language = ? LIMIT 1")
        cursor.execute(sql, (requested, lang))
        row = cursor.fetchone()
        if row:
            return row[0]
    finally:
        conn.close()
    return requested


def get_user_keystrokes_list(user_id: str, language: str) -> list[list[dict[str, Any]]]:
    """從資料庫查出該用戶的所有基準特徵紀錄"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        sql = q("SELECT keystrokes_json FROM user_profiles WHERE LOWER(user_id) = LOWER(?) AND language = ?")
        cursor.execute(sql, (user_id, language))
        rows = cursor.fetchall()
    finally:
        conn.close()
        
    if not rows:
        raise BaselineNotFoundError(f"資料庫中找不到用戶 {user_id!r} 的基準特徵。請先註冊。")
        
    keystrokes_list = []
    for row in rows:
        try:
            keystrokes_list.append(json.loads(row[0]))
        except Exception:
            continue
    return keystrokes_list


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _configured_threshold(user_id: str, language: Optional[str], mode: str) -> tuple[Optional[float], Optional[str]]:
    lang = (language or "ZH").upper()
    normalized_mode = "continuous" if mode == "continuous" else "final"
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # 1. 查找特定用戶及特定語言
        sql = q("SELECT final_threshold, continuous_threshold FROM user_thresholds WHERE user_id = ? AND language = ?")
        cursor.execute(sql, (user_id, lang))
        row = cursor.fetchone()
        if row:
            val = row[1] if normalized_mode == "continuous" else row[0]
            return val, f"db_config:{normalized_mode}"
            
        # 2. 查找特定用戶全域設定 (ALL)
        sql = q("SELECT final_threshold, continuous_threshold FROM user_thresholds WHERE user_id = ? AND language = 'ALL'")
        cursor.execute(sql, (user_id,))
        row = cursor.fetchone()
        if row:
            val = row[1] if normalized_mode == "continuous" else row[0]
            return val, f"db_config:{normalized_mode}:all"
            
        # 3. 查找預設設定
        sql = q("SELECT final_threshold, continuous_threshold FROM user_thresholds WHERE user_id = 'default' AND language = ?")
        cursor.execute(sql, (lang,))
        row = cursor.fetchone()
        if row:
            val = row[1] if normalized_mode == "continuous" else row[0]
            return val, f"db_config:{normalized_mode}:default"
    finally:
        conn.close()
            
    return None, None


class KeystrokeRequestHandler(SimpleHTTPRequestHandler):
    server_version = "KeystrokeVerifierHTTP/2.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {format % args}")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "model_loaded": verifier.model is not None,
                    "database_type": "PostgreSQL" if IS_POSTGRES else "SQLite",
                    "database_path": DATABASE_URL if IS_POSTGRES else str(DATABASE_PATH.resolve()),
                    "processed_dir": str(PROCESSED_DIR.resolve()),
                }
            )
            return

        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/login":
                self.handle_login(payload)
            elif parsed.path == "/api/verify":
                self.handle_verify(payload)
            elif parsed.path == "/api/free-text-session":
                self.handle_free_text_session(payload)
            elif parsed.path == "/api/register":
                self.handle_register(payload)
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
        except BaselineNotFoundError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def handle_login(self, payload: Dict[str, Any]) -> None:
        user_id = str(payload.get("user_id", "")).strip()
        language = self.normalize_language(payload.get("language", "ZH"))
        force = bool(payload.get("force", False))
        if not user_id:
            raise ValueError("Missing user_id")

        user_id = resolve_user_id(user_id, language=language)
        keystrokes_list = get_user_keystrokes_list(user_id, language)
        
        # 呼叫 Verifier 計算或讀取快取
        cache = verifier.ensure_baseline(user_id, keystrokes_list=keystrokes_list, language=language, force=force)
        self.send_json(
            {
                "ok": True,
                "user_id": user_id,
                "language": language,
                "template_count": int(cache["features"].shape[0]),
                "feature_shape": list(cache["features"].shape),
                "cache_path": cache.get("cache_path"),
            }
        )

    def handle_verify(self, payload: Dict[str, Any]) -> None:
        user_id = str(payload.get("user_id", "")).strip()
        language = self.normalize_language(payload.get("language", "ZH"))
        records = payload.get("records")
        if not user_id:
            raise ValueError("Missing user_id")
        if not isinstance(records, list) or not records:
            raise ValueError("Missing records")

        user_id = resolve_user_id(user_id, language=language)
        keystrokes_list = get_user_keystrokes_list(user_id, language)

        threshold = payload.get("threshold")
        threshold_source_override = None
        if threshold in ("", None):
            mode = str(payload.get("verification_mode", "final")).strip().lower()
            threshold, threshold_source_override = _configured_threshold(
                user_id=user_id,
                language=language,
                mode=mode,
            )
        else:
            mode = str(payload.get("verification_mode", "final")).strip().lower()
            threshold = float(threshold)

        strategy = str(payload.get("matching_strategy", verifier.matching_strategy))

        with verify_lock:
            result = verifier.verify_records(
                user_id=user_id,
                records=records,
                keystrokes_list=keystrokes_list,
                language=language,
                threshold=threshold,
                matching_strategy=strategy,
            )

        if threshold_source_override:
            result["threshold_source"] = threshold_source_override
            result["verification_mode"] = "continuous" if mode == "continuous" else "final"

        # 寫入歷史結果 (對應原有的單次驗證寫入)
        if payload.get("save_result", True):
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                sql = q("""
                    INSERT INTO verification_sessions 
                    (user_id, session_id, language, article_character_count, keystroke_count, final_score, is_genuine, keystrokes_json, continuous_results_json) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """)
                cursor.execute(
                    sql,
                    (
                        user_id,
                        f"single_{_timestamp()}",
                        language or "ZH",
                        0,
                        len(records),
                        result["score"],
                        bool(result["is_genuine"]),
                        json.dumps(records),
                        "[]"
                    )
                )
                conn.commit()
            finally:
                conn.close()

        self.send_json({"ok": True, "result": result})

    def handle_free_text_session(self, payload: Dict[str, Any]) -> None:
        user_id = str(payload.get("user_id", "")).strip()
        language = self.normalize_language(payload.get("language", "ZH"))
        if not user_id:
            raise ValueError("Missing user_id")

        user_id = resolve_user_id(user_id, language=language)
        final_result = payload.get("final_result")
        continuous_results = payload.get("continuous_results")
        keystroke_records = payload.get("keystroke_records", [])
        
        if not isinstance(final_result, dict):
            raise ValueError("Missing final_result")
        if not isinstance(continuous_results, list):
            raise ValueError("continuous_results must be a list")
        if not isinstance(keystroke_records, list):
            raise ValueError("keystroke_records must be a list")

        session_id = payload.get("session_id") or f"session_{_timestamp()}"
        
        # 寫入歷程紀錄至資料庫
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            sql = q("""
                INSERT INTO verification_sessions 
                (user_id, session_id, language, article_character_count, keystroke_count, final_score, is_genuine, keystrokes_json, continuous_results_json) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """)
            cursor.execute(
                sql,
                (
                    user_id,
                    session_id,
                    language or "ZH",
                    int(payload.get("article_character_count", 0)),
                    int(payload.get("keystroke_count", len(keystroke_records))),
                    float(final_result.get("score", 0.0)),
                    bool(final_result.get("is_genuine")),
                    json.dumps(keystroke_records),
                    json.dumps(continuous_results)
                )
            )
            conn.commit()
        finally:
            conn.close()

        self.send_json(
            {
                "ok": True,
                "session_id": session_id,
            }
        )

    def handle_register(self, payload: Dict[str, Any]) -> None:
        user_id = str(payload.get("user_id", "")).strip()
        language = str(payload.get("language", "ZH")).strip().upper()
        keystrokes = payload.get("keystrokes", [])

        if not user_id:
            raise ValueError("缺少帳號 ID (user_id)")
        if not keystrokes or not isinstance(keystrokes, list):
            raise ValueError("缺少按鍵記錄數據 (keystrokes)")
        if language not in ("ZH", "EN"):
            raise ValueError("語言類型不合法，必須為 ZH 或 EN")

        # 將擊鍵陣列轉為 JSON 字串存入資料庫
        keystrokes_json = json.dumps(keystrokes)
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            sql = q("INSERT INTO user_profiles (user_id, language, keystrokes_json) VALUES (?, ?, ?)")
            cursor.execute(sql, (user_id, language, keystrokes_json))
            conn.commit()
        finally:
            conn.close()

        # 清除 processed_baselines 資料夾下該用戶的舊快取
        cache_file_zh = PROCESSED_DIR / f"{user_id}_ZH.npy"
        cache_file_en = PROCESSED_DIR / f"{user_id}_EN.npy"
        cache_file_all = PROCESSED_DIR / f"{user_id}_ALL.npy"
        for cache_file in (cache_file_zh, cache_file_en, cache_file_all):
            if cache_file.exists():
                try:
                    cache_file.unlink()
                except Exception:
                    pass

        self.send_json(
            {
                "ok": True,
                "user_id": user_id,
                "language": language,
            }
        )

    def normalize_language(self, language: Any) -> Optional[str]:
        value = str(language or "").strip().upper()
        if value in ("", "ALL", "NONE"):
            return None
        return value

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON payload") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")
        return data

    def send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"ok": False, "error": message}, status=status)

    def serve_static(self, request_path: str) -> None:
        if request_path in ("", "/"):
            relative = "index.html"
        else:
            relative = unquote(request_path).lstrip("/")

        target = (PROJECT_ROOT / relative).resolve()
        if not str(target).startswith(str(PROJECT_ROOT.resolve())) or not target.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return

        content = target.read_bytes()
        content_type, _ = mimetypes.guess_type(str(target))
        if content_type is None:
            content_type = "application/octet-stream"
        elif content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "application/xml",
        }:
            content_type = f"{content_type}; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def check_and_download_weights():
    """檢測並修復 Git LFS 指標檔案，自動從 GitHub 下載真實的 227MB 權重"""
    weights_path = PROJECT_ROOT / "10persentData_model.weights.h5"
    if not weights_path.exists() or weights_path.stat().st_size < 1024 * 1024:
        print("[!] 偵測到模型權重檔案不存在或為 Git LFS 指標檔案，正在自動下載真實模型數據 (227MB)...")
        url = "https://media.githubusercontent.com/media/Toitp0920/keystroke-dynamics-verifier/main/10persentData_model.weights.h5"
        import urllib.request
        try:
            weights_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = weights_path.with_suffix(".tmp")
            
            # 下載至臨時檔案
            urllib.request.urlretrieve(url, temp_path)
            
            if temp_path.exists() and temp_path.stat().st_size > 1024 * 1024:
                if weights_path.exists():
                    weights_path.unlink()
                temp_path.rename(weights_path)
                print(f"[+] 成功下載並取代模型權重！大小：{weights_path.stat().st_size} bytes")
            else:
                print("[-] 下載的檔案無效或大小不足，請確認連結。")
                if temp_path.exists():
                    temp_path.unlink()
        except Exception as e:
            print(f"[-] 自動下載模型權重時發生錯誤：{e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Keystroke Dynamics verification web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # 初始化資料表 (Postgres 或 SQLite)
    init_db()
    
    # 檢查並下載模型權重 (防範 Git LFS 指針問題)
    check_and_download_weights()

    address = (args.host, args.port)
    httpd = ThreadingHTTPServer(address, KeystrokeRequestHandler)
    print(f"Keystroke verifier running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
