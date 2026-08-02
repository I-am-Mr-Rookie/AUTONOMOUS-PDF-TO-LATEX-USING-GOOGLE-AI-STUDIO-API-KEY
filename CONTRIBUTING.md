# Contributing to Autonomous PDF → LaTeX Pipeline

Thank you for considering contributing to the Autonomous PDF → LaTeX Pipeline!

## Code of Conduct

Please maintain a respectful, welcoming, and collaborative environment for all contributors.

## How to Contribute

### 1. Reporting Issues
- Search existing issues before creating a new one to avoid duplicates.
- Provide clear steps to reproduce any bug, along with error logs and system environment details (OS, Python version, TeX distribution).

### 2. Pull Requests
1. Fork the repository and create your branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Ensure preflight checks pass cleanly:
   ```bash
   python main.py --preflight-only
   ```
3. Maintain existing architecture conventions:
   - **Cache-first state machine**: Preserve `cache/[Original_Name]_state.json` updates and atomic writes.
   - **Zero-API offline steps**: Ensure Phase 1 (preflight) and Phase 3 (image slicing) consume zero API requests.
   - **Filename preservation**: Propagate `[Original_Name]` base names cleanly across `input/`, `cache/`, and `output/`.
4. Submit your pull request with a descriptive title and detailed summary of changes.

## Development Guidelines
- Follow PEP 8 guidelines for Python code.
- Ensure cross-platform compatibility (Windows, macOS, Linux). Use `pathlib` for all file path operations.
- Never commit API keys or private exam PDFs. Ensure `.env` remains gitignored.
