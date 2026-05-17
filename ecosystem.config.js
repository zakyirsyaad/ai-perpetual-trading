module.exports = {
    apps: [
      {
        name: "ai-perp-volume",
        cwd: "/home/trader/ai-perpetual-trading",
        script: "src/main.py",
        interpreter: "/home/trader/ai-perpetual-trading/.venv/bin/python",
        env: {
          PYTHONPATH: "src"
        },
        autorestart: true,
        restart_delay: 10000,
        max_restarts: 20
      }
    ]
  };