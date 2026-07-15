#!/usr/bin/env bash
uv run uvicorn terminals.main:app --reload --ws-per-message-deflate false
