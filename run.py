#!/usr/bin/env python3
"""Run the NAgent server.

Usage:
    python run.py                      # 127.0.0.1:8765 (solo)
    AGENT_HOST=0.0.0.0 AGENT_PORT=8080 python run.py
"""
from server.main import main

if __name__ == "__main__":
    main()
