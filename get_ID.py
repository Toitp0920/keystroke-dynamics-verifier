import os
import re

def extract_student_ids(folder_path, output_file="participants.txt"):
    # 用來儲存所有學號的集合（Set 可以自動避免重複）
    student_ids = set()
    
    # 正則表達式：匹配 keystrokes_ 後面的學號（由英文字母和數字組成）
    # _(\w+)_ 會抓取兩個底線之間的所有字元
    pattern = re.compile(r"keystrokes_([a-zA-Z0-9]+)_")

    try:
        # 檢查資料夾是否存在
        if not os.path.exists(folder_path):
            print(f"錯誤：找不到資料夾 '{folder_path}'")
            return

        # 遍歷資料夾內的所有檔案
        for filename in os.listdir(folder_path):
            # 確保是檔案而非子資料夾，且副檔名是 .tsv
            if os.path.isfile(os.path.join(folder_path, filename)) and filename.endswith('.tsv'):
                match = pattern.search(filename)
                if match:
                    # 提取括號中匹配到的學號
                    student_id = match.group(1)
                    student_ids.add(student_id)
        
        # 將學號排序，讓輸出的檔案更整齊
        sorted_ids = sorted(list(student_ids))
        
        # 寫入到 participants.txt
        with open(output_file, "w", encoding="utf-8") as f:
            for s_id in sorted_ids:
                f.write(s_id + "\n")
                
        print(f"成功！已提取 {len(sorted_ids)} 個不重複的學號，並寫入至 '{output_file}'。")

    except Exception as e:
        print(f"執行過程中發生錯誤: {e}")

# --- 使用說明 ---
# 請將下方的路徑替換成你實際存放資料夾的路徑
# 如果程式檔案跟資料夾在同一個目錄，可以直接寫資料夾名稱，例如 "test_data"
target_folder = r"C:\Users\ji3ru\Downloads\畢專_受試者資料" 
extract_student_ids(target_folder)