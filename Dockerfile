FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 安裝基本編譯依賴與 sqlite3 指令工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# 複製依賴清單並安裝
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 複製專案代碼
COPY . .

# 建立持久化數據目錄
RUN mkdir -p /data

# 暴露服務埠口
EXPOSE 8000

# 設定環境變數指向持久化掛載區
ENV DATABASE_PATH=/data/keystroke_dynamics.db
ENV PROCESSED_DIR=/data/processed_baselines/

# 啟動伺服器，使用 0.0.0.0 監聽以利外部存取
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]
