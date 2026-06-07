import sqlite3

conn = sqlite3.connect('chat_history.db')
cursor = conn.cursor()

cursor.execute("SELECT * FROM chat")
rows = cursor.fetchall()

for row in rows:
    print(f"ID: {row[0]}")
    print(f"问题: {row[1]}")
    print(f"回答: {row[2]}")
    print(f"时间: {row[3]}")
    print("-" * 40)

conn.close()