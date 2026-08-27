import sqlite3

con = sqlite3.connect("/app/backend/data/webui.db")
cur = con.cursor()
print("before:", cur.execute("SELECT id, name, is_active FROM function").fetchall())
cur.execute("UPDATE function SET is_active = 1 WHERE id = 'npa'")
con.commit()
print("after:", cur.execute("SELECT id, name, is_active FROM function").fetchall())
print("api_keys:", cur.execute("SELECT count(*) FROM api_key").fetchone()[0])
