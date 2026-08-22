# CoC AI Controller

CoC AI Controller is a Windows desktop application that combines MuMu Player control, persistent village data, configurable automation tasks, and Gemini-assisted visual decisions.

The application is packaged as a PyInstaller one-directory build. Its automation design follows an observe, decide, act, and verify cycle so that emulator actions can be checked against a fresh screenshot before a task is marked complete.

## Design

- [Architecture](architecture.md) describes the application boundaries and runtime flow.
- [AI Agent](ai-agent.md) records the agent contract and safety constraints.
- [Requirements](requirements.md) defines the current product scope.
- [Database Schema](database-schema.md) documents persistent state.
- [Decisions](decisions.md) captures important technical choices.

## Project

- [Handoff](handoff.md) is the current status, known problems and next work.
- [GitHub Issues](https://github.com/Mai0313/CoC-AI-Controller/issues) track remaining implementation work.
- [Changelog](changelog.md) lists what shipped in each version.
- [Development Session Log](dev-session.md) records what each working session produced.
- [Reference Audit](reference-audit.md) holds the reference-project and local MuMu findings.
- [ChatGPT Handoff](chatgpt-handoff.md) explains what to attach when asking an external assistant for help.
