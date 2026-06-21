def compare_participants(file1_path, file2_path):
    try:
        # 讀取第一個檔案，並去除空白與換行
        with open(file1_path, "r", encoding="utf-8") as f:
            set1 = set(line.strip() for line in f if line.strip())

        # 讀取第二個檔案，並去除空白與換行
        with open(file2_path, "r", encoding="utf-8") as f:
            set2 = set(line.strip() for line in f if line.strip())

        # 1. 找出重複的 ID（交集）
        duplicate_ids = sorted(list(set1.intersection(set2)))

        # 2. 找出只存在於第一個檔案的 ID（差集）
        only_in_1 = sorted(list(set1.difference(set2)))

        # 3. 找出只存在於第二個檔案的 ID（差集）
        only_in_2 = sorted(list(set2.difference(set1)))

        # 寫入結果檔案
        with open("both_exist.txt", "w", encoding="utf-8") as f:
            f.writelines(f"{s_id}\n" for s_id in duplicate_ids)

        with open("only_in_p1.txt", "w", encoding="utf-8") as f:
            f.writelines(f"{s_id}\n" for s_id in only_in_1)

        with open("only_in_p2.txt", "w", encoding="utf-8") as f:
            f.writelines(f"{s_id}\n" for s_id in only_in_2)

        # 在螢幕上顯示摘要報告
        print("=== 比對完成摘要 ===")
        print(f"participants1.txt 總人數: {len(set1)}")
        print(f"participants2.txt 總人數: {len(set2)}")
        print(f"----------------------")
        print(f"兩邊都重複的 ID 人數: {len(duplicate_ids)} -> 已存入 both_exist.txt")
        print(f"只在 p1 出現的人數: {len(only_in_1)} -> 已存入 only_in_p1.txt")
        print(f"只在 p2 出現的人數: {len(only_in_2)} -> 已存入 only_in_p2.txt")

    except FileNotFoundError as e:
        print(f"錯誤：找不到檔案，請確認路徑是否正確。詳細訊息: {e}")
    except Exception as e:
        print(f"執行過程中發生錯誤: {e}")

# --- 使用說明 ---
# 請確保這兩個 txt 檔案與程式碼在同一個資料夾下，或者填入完整路徑
file1 = "participants1.txt"
file2 = "participants2.txt"

compare_participants(file1, file2)