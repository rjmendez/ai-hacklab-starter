# AI HackLab Architecture

This document provides a deep dive into the architecture and functionality of the AI HackLab Starter kit.

## Overview
The system is built around an Agent-to-Agent (A2A) JSON-RPC protocol, Redis-based task handling, and model routing through a dispatcher.

### Core Components
- **A2A Protocol**: Ensures seamless agent communication using JSON-RPC.
- **Dispatcher**: Manages tasks among agents and tracks spending efficiently.
- **TOTP 2FA**: Adds another layer of security within the mesh.

![Architecture Diagram]

Further details: explore each module for implementation specifics.