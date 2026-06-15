import os
import requests
from dotenv import load_dotenv

# טעינת המפתחות הסודיים מקובץ ה-.env לזיכרון של התוכנית
load_dotenv()

monday_key = os.getenv("MONDAY_API_KEY")

# הגדרת הכתובת של monday והרשאות הגישה
url = "https://api.monday.com/v2"
headers = {
    "Authorization": monday_key,
    "Content-Type": "application/json"
}

# שאילתה פשוטה שמבקשת מ-monday להחזיר את השם של בעל המפתח (אותך)
query = "{ me { name } }"

print("🔄 מנסה להתחבר ל-API של monday...")

try:
    response = requests.post(url, json={"query": query}, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        
        # בדיקה אם monday החזירה שגיאה פנימית (למשל מפתח לא תקין)
        if "errors" in data:
            print("❌ monday החזירה שגיאה. בדקי שהמפתח שלך תקין:")
            print(data["errors"])
        else:
            user_name = data["data"]["me"]["name"]
            print(f"🎉 החיבור הצליח ב-100%! המחשב מחובר לחשבון של: {user_name}")
            
    else:
        print(f"❌ שגיאה בתקשורת השרת. קוד שגיאה: {response.status_code}")
        print(response.text)

except Exception as e:
    print(f"❌ משהו השתבש בהרצת הסקריפט: {e}")