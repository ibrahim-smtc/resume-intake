"""Run the app locally:  python run.py   (then open http://127.0.0.1:8000).

Restart it after changing .env. This is for development only: in production the app is started with uvicorn, see the README.
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True, reload_dirs=["app"])
