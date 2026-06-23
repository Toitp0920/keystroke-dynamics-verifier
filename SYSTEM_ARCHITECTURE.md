# 系統架構圖

本圖以目前程式碼的實際執行路徑為準，並將不在 Web 主流程內的訓練、CLI 與資料整理工具另外標示。

```mermaid
flowchart TB
    User["使用者"]

    subgraph Browser["瀏覽器端｜HTML + CSS + JavaScript"]
        UI["單頁介面<br/>登入／註冊／自由寫作／結果"]
        Capture["鍵盤與 IME 事件採集<br/>keydown／keyup／composition"]
        Window["事件切段<br/>每 100 筆持續驗證<br/>送出時重切全部事件"]
        Export["前端匯出<br/>TSV 擊鍵軌跡／JSON 報告"]
        UI --> Capture --> Window
        UI --> Export
    end

    subgraph WebServer["Python Web 服務｜server.py"]
        Static["靜態檔案服務<br/>index.html／app.js／style.css"]
        API["HTTP JSON API<br/>GET /api/health<br/>POST /api/register<br/>POST /api/login<br/>POST /api/verify<br/>POST /api/free-text-session"]
        UserService["使用者與基準資料服務<br/>帳號解析／註冊資料讀取"]
        Threshold["閾值解析<br/>使用者+語言 → 使用者+ALL<br/>→ default+語言 → 基準校準"]
        SessionService["驗證歷程寫入"]
        Lock["全域 verify_lock<br/>序列化模型推論"]
        API --> UserService
        API --> Threshold
        API --> SessionService
        API --> Lock
    end

    subgraph Engine["擊鍵驗證引擎｜keystroke_verifier.py"]
        Baseline["基準建立／載入<br/>原始擊鍵 JSON → 使用者 HistogramProfile"]
        BaseFeature["基礎特徵<br/>100 × 3<br/>Keycode／Hold Time／Flight Time"]
        Simpac["SIMPAC 合成特徵<br/>使用者歷史分布產生 synthetic HT／FT<br/>輸出 100 × 5"]
        Model["Type2Branch TensorFlow 模型<br/>鍵碼 Embedding + Temporal Attention"]
        Branch1["分支一<br/>BiGRU → Self Attention → BiGRU"]
        Branch2["分支二<br/>3× Conv1D → Channel Attention → GAP"]
        Embedding["融合 Dense<br/>256 維行為向量"]
        Match["歐氏距離比對<br/>mean_file／min_file／<br/>mean_template／min_template"]
        Decision["Score ≤ Threshold<br/>本人／特徵異常"]

        Baseline --> BaseFeature
        BaseFeature --> Simpac --> Model
        Model --> Branch1 --> Embedding
        Model --> Branch2 --> Embedding
        Embedding --> Match --> Decision
    end

    subgraph Storage["持久化與模型資產"]
        DB[("SQLite 本機<br/>或 PostgreSQL 雲端")]
        Profiles["user_profiles<br/>註冊原始擊鍵 JSON"]
        Thresholds["user_thresholds<br/>final／continuous"]
        Sessions["verification_sessions<br/>擊鍵、持續結果、最終結果"]
        Cache[("processed_baselines/*.npy<br/>100 × 5 特徵與 HistogramProfile")]
        Weights[("10persentData_model.weights.h5<br/>預訓練權重，約 227 MB")]
        LegacyConfig["threshold_config.json<br/>啟動時匯入資料庫"]
        DB --- Profiles
        DB --- Thresholds
        DB --- Sessions
        LegacyConfig -. "init_db" .-> Thresholds
    end

    subgraph Offline["離線／相容工具（不在目前 Web 主路徑）"]
        LegacyTSV[("baseline_profiles/*.tsv<br/>舊式基準來源")]
        CLI["keystroke_verifier.py CLI<br/>前處理／Embedding／TSV 驗證"]
        Training["type2branch_model/<br/>train.py／evaluate.py／model.py／conf.py"]
        Dataset[("外部訓練 datasets/*.npy<br/>及未隨專案提供的訓練模組")]
        Helpers["get_ID.py／duplicate.py<br/>受試者 ID 整理"]
        LegacyDirs["verification_results/<br/>舊式檔案輸出目錄"]
        LegacyTSV --> CLI
        Dataset --> Training --> Weights
        Helpers -.-> LegacyTSV
        CLI -.-> LegacyDirs
    end

    subgraph Deployment["啟動與部署"]
        Docker["Docker／Railway<br/>Python 3.11，Port 8000"]
        Env["環境變數<br/>DATABASE_URL／DATABASE_PATH／PROCESSED_DIR"]
        Remote["GitHub Media<br/>權重缺失或為 LFS 指標時下載"]
        Env --> Docker
        Remote -. "啟動時修復權重" .-> Weights
    end

    User --> UI
    UI -->|"GET /"| Static
    Window -->|"POST /api/verify<br/>continuous／final"| API
    UI -->|"register／login／free-text-session"| API
    API -->|"JSON 結果"| UI

    UserService <-->|"註冊與讀取"| Profiles
    Threshold <-->|"查詢"| Thresholds
    SessionService -->|"寫入"| Sessions
    UserService --> Baseline
    Baseline <-->|"讀取／重建"| Cache
    Lock --> BaseFeature
    Weights --> Model
    Threshold --> Decision
    Decision --> API
    Docker --> WebServer
```

## 關鍵資料流

1. **註冊**：使用者完成 15 段指定句子，瀏覽器記錄按壓／放開時間、鍵碼及中文 IME 狀態，透過 `/api/register` 寫入 `user_profiles`，並清除該使用者的舊 `.npy` 快取。
2. **登入**：`/api/login` 從資料庫讀取註冊擊鍵；若快取不存在，建立使用者專屬 `HistogramProfile` 與 `(N, 100, 5)` 特徵快取。
3. **持續驗證**：自由寫作每累積 100 筆有效擊鍵，前端呼叫 `/api/verify`；後端產生 256 維向量，和基準向量計算歐氏距離並套用 `continuous_threshold`。
4. **最終驗證**：送出文章時，前端把全部事件依 100 筆重切，使用 `final_threshold` 驗證，再由 `/api/free-text-session` 將整次結果寫入 `verification_sessions`。

## 實作邊界與現況

- Web 主流程以 `user_profiles`、`user_thresholds`、`verification_sessions` 三張資料表為持久化核心；`DATABASE_URL` 存在時使用 PostgreSQL，否則使用 SQLite。
- `baseline_profiles/*.tsv`、`verification_results/` 仍存在於文件與 CLI 相容流程，但目前瀏覽器註冊和驗證歷程不直接寫入這些目錄。
- `type2branch_model/train.py` 與 `evaluate.py` 是離線模型研發流程；它們依賴目前倉庫未包含的 `util`、`training_generator`、`validation_generator`、`loss` 與外部資料集，因此不屬於可直接啟動的 Web 服務。
- 現有快取檔實際為 `(1, 100, 5)`；目前一筆註冊資料庫紀錄會建立一個基準模板，即使該紀錄內含多個 `TEST_SECTION_ID`。
