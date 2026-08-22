module.exports = {
  apps: [
    {
      name: "ai-perp-volume",
      cwd: "/home/0xkayz/ai-perpetual-trading",
      script: "src/main.py",
      interpreter: "/home/0xkayz/ai-perpetual-trading/.venv/bin/python",
      env: {
        PYTHONPATH: "src"
      },
      autorestart: true,
      restart_delay: 10000
    }
  ]
};
