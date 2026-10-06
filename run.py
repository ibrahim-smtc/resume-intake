"""Run the app locally:  python run.py   (then open http://127.0.0.1:8000).

Restart it after changing .env. On Vercel this file is not used: Vercel loads `app` from app/main.py.
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True, reload_dirs=["app"])
