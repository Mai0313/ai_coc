# CoC AI Controller

CoC AI Controller is a Windows desktop application that combines MuMu Player control, persistent village data, configurable automation tasks, and Gemini-assisted visual decisions.

The application is packaged as a PyInstaller one-directory build. Its automation design follows an observe, decide, act, and verify cycle so that emulator actions can be checked against a fresh screenshot before a task is marked complete.

## Project documentation

- [Architecture](architecture.md) describes the application boundaries and runtime flow.
- [AI Agent](ai-agent.md) records the agent contract and safety constraints.
- [Requirements](requirements.md) defines the current product scope.
- [Database Schema](database-schema.md) documents persistent state.
- [Decisions](decisions.md) captures important technical choices.
- [Roadmap](todo.md) tracks remaining implementation work.
