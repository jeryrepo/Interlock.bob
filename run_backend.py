import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "orchestrator.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=["orchestrator", "agents"],
        reload_excludes=["fixtures/*", "*.db", "interlock.db*"],
    )
