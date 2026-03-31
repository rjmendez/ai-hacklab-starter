# AI HackLab Starter

![MIT License Badge](https://img.shields.io/badge/license-MIT-green)

## Overview

A starter kit for building multi-agent AI research labs. This repository provides tools,
architecture, and templates for setting up an A2A (Agent-to-Agent) system, model routing,
and spend tracking across various AI agents. Fully open-source and production-ready
for experimentation or deployment.

## Architecture
```
+----------------------+       +---------+          +------------------------------+
| Agent Alpha         |       | Redis   |          | Dispatcher                   |
| (Pipeline Manager)  | <-->  | (Tasks) | <------> | + Spend Tracker             |
+----------------------+       +---------+          | + Model Budgeting           |
                                                    +------------------------------+
+----------------------+                             +----------------------------- +
| Agent Beta          |                             | A2A JSON-RPC Protocol Server|
| (GPU Compute Nodes )->                           +                             
```