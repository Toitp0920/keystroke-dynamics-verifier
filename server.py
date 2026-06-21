from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from keystroke_verifier import BaselineNotFoundError, KeystrokeVerifier


PROJECT_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT_ROOT / "verification_results"
THRESHOLD_CONFIG_PATH = PROJECT_ROOT / "threshold_config.json"
TSV_HEADERS = [
    "PARTICIPANT_ID",
    "TEST_SECTION_ID",
    "SENTENCE",
    "USER_INPUT",
    "KEYSTROKE_ID",
    "PRESS_TIME",
    "RELEASE_TIME",
    "LETTER",
    "KEYCODE",
    "ZHUYIN_STAGE",
    "IME_STAGE",
    "COMPOSING_DATA",
    "COMPOSING_SEQ",
]

verifier = KeystrokeVerifier(project_root=PROJECT_ROOT)
verify_lock = threading.Lock()


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _escape_tsv_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _records_to_tsv(records: list[Any], article: str = "") -> str:
    rows = ["\t".join(TSV_HEADERS)]
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("keystroke_records must contain JSON objects")
        row = []
        for header in TSV_HEADERS:
            value = item.get(header, "")
            if header == "USER_INPUT" and article:
                value = article
            row.append(_escape_tsv_cell(value))
        rows.append("\t".join(row))
    return "\n".join(rows) + "\n"


def _load_threshold_config() -> Dict[str, Any]:
    if not THRESHOLD_CONFIG_PATH.is_file():
        return {}
    with THRESHOLD_CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("threshold_config.json must contain a JSON object")
    return data


def _lookup_threshold(section: Any, language: Optional[str], mode: str) -> Optional[float]:
    if not isinstance(section, dict):
        return None

    lang = (language or "ALL").upper()
    candidates = [lang, "ALL", "default"]
    for key in candidates:
        nested = section.get(key)
        if isinstance(nested, dict):
            value = nested.get(mode) or nested.get(f"{mode}_threshold")
            if value is not None:
                return float(value)

    value = section.get(mode) or section.get(f"{mode}_threshold")
    if value is not None:
        return float(value)
    return None


def _configured_threshold(user_id: str, language: Optional[str], mode: str) -> tuple[Optional[float], Optional[str]]:
    config = _load_threshold_config()
    if not config:
        return None, None

    normalized_mode = "continuous" if mode == "continuous" else "final"
    users = config.get("users", {})
    user_config = users.get(user_id) if isinstance(users, dict) else None
    value = _lookup_threshold(user_config, language, normalized_mode)
    if value is not None:
        return value, f"free_text_config:{normalized_mode}"

    value = _lookup_threshold(config.get("default"), language, normalized_mode)
    if value is not None:
        return value, f"free_text_config:{normalized_mode}:default"

    return None, None


class KeystrokeRequestHandler(SimpleHTTPRequestHandler):
    server_version = "KeystrokeVerifierHTTP/1.0"

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
                    "baseline_dir": str(verifier.baseline_dir),
                    "processed_dir": str(verifier.processed_dir),
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

        user_id = verifier.resolve_user_id(user_id, language=language)
        cache = verifier.ensure_baseline(user_id, language=language, force=force)
        self.send_json(
            {
                "ok": True,
                "user_id": user_id,
                "language": language,
                "baseline_files": [item["name"] for item in cache["source_files"]],
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

        user_id = verifier.resolve_user_id(user_id, language=language)
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
                language=language,
                threshold=threshold,
                matching_strategy=strategy,
            )

        if threshold_source_override:
            result["threshold_source"] = threshold_source_override
            result["verification_mode"] = "continuous" if mode == "continuous" else "final"

        if payload.get("save_result", True):
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            out_path = RESULTS_DIR / f"verification_{language or 'ALL'}_{user_id}_{_timestamp()}.json"
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "result": result,
                        "record_count": len(records),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                )
            result["result_path"] = str(out_path)

        self.send_json({"ok": True, "result": result})

    def handle_free_text_session(self, payload: Dict[str, Any]) -> None:
        user_id = str(payload.get("user_id", "")).strip()
        language = self.normalize_language(payload.get("language", "ZH"))
        if not user_id:
            raise ValueError("Missing user_id")

        user_id = verifier.resolve_user_id(user_id, language=language)
        final_result = payload.get("final_result")
        continuous_results = payload.get("continuous_results")
        keystroke_records = payload.get("keystroke_records", [])
        article = str(payload.get("article", ""))
        if not isinstance(final_result, dict):
            raise ValueError("Missing final_result")
        if not isinstance(continuous_results, list):
            raise ValueError("continuous_results must be a list")
        if not isinstance(keystroke_records, list):
            raise ValueError("keystroke_records must be a list")

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = _timestamp()
        out_path = RESULTS_DIR / f"free_text_session_{language or 'ALL'}_{user_id}_{timestamp}.json"
        tsv_path = RESULTS_DIR / f"free_text_keystrokes_{language or 'ALL'}_{user_id}_{timestamp}.tsv"
        if keystroke_records:
            tsv_path.write_text(_records_to_tsv(keystroke_records, article=article), encoding="utf-8")

        document = {
            "result_type": "free_text_session",
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "user_id": user_id,
            "language": language,
            "session_id": payload.get("session_id"),
            "article_character_count": payload.get("article_character_count"),
            "keystroke_count": payload.get("keystroke_count"),
            "keystroke_tsv_path": str(tsv_path) if keystroke_records else None,
            "keystroke_tsv_record_count": len(keystroke_records),
            "continuous_window_size": payload.get("continuous_window_size"),
            "continuous_results": continuous_results,
            "final_result": final_result,
        }
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(document, f, ensure_ascii=False, indent=2, default=_json_default)

        self.send_json(
            {
                "ok": True,
                "result_path": str(out_path),
                "keystroke_tsv_path": str(tsv_path) if keystroke_records else None,
            }
        )

    def handle_register(self, payload: Dict[str, Any]) -> None:
        # 註冊新帳號的 API，接收前端發送來的 TSV 打字紀錄
        user_id = str(payload.get("user_id", "")).strip()
        language = str(payload.get("language", "ZH")).strip().upper()
        tsv_data = payload.get("tsv_data", "")

        if not user_id:
            raise ValueError("缺少學號/帳號 (user_id)")
        if not tsv_data:
            raise ValueError("缺少按鍵記錄數據 (tsv_data)")
        if language not in ("ZH", "EN"):
            raise ValueError("語言類型不合法，必須為 ZH 或 EN")

        # 確保 baseline_profiles 目錄存在
        baseline_dir = PROJECT_ROOT / "baseline_profiles"
        baseline_dir.mkdir(parents=True, exist_ok=True)

        # 產生檔名：keystrokes_[LANGUAGE]_[user_id]_[timestamp].tsv
        # 為了與原本的檔案命名格式一致（如 keystrokes_ZH_110b06252_20260525T02070.tsv），這裡使用 YYYYMMDDThhmmss
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        filename = f"keystrokes_{language}_{user_id}_{timestamp}.tsv"
        filepath = baseline_dir / filename

        # 寫入檔案，使用帶 BOM 的 UTF-8（utf-8-sig）
        filepath.write_text(tsv_data, encoding="utf-8-sig")

        # 清除 processed_baselines 資料夾下該用戶的舊快取，以確保再次登入時會重新解析新的 TSV 基準檔案
        cache_file_zh = PROJECT_ROOT / "processed_baselines" / f"{user_id}_ZH.npy"
        cache_file_en = PROJECT_ROOT / "processed_baselines" / f"{user_id}_EN.npy"
        cache_file_all = PROJECT_ROOT / "processed_baselines" / f"{user_id}_ALL.npy"
        for cache_file in (cache_file_zh, cache_file_en, cache_file_all):
            if cache_file.exists():
                try:
                    cache_file.unlink()
                except Exception:
                    pass

        self.send_json(
            {
                "ok": True,
                "filename": filename,
                "filepath": str(filepath.resolve()),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Keystroke Dynamics verification web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

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
