#!/bin/bash
# Scripted fake agent: no LLM. Tests that the observers and compare.py catch known divergences.
cd /workspace && exec python3 /opt/mx/fake_agent.py "$1"
