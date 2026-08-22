# CoC Agent — VER 0.1.0

The AI Agent is the general CoC operator. It receives emulator/frame identity, current screenshot and available account context. Gemini semantic vision is primary; templates are not used in this release.

Every proposed future action must carry `emulator_id` and `frame_id` and specify a postcondition. The current UI provides semantic observation/chat and manual verified action primitives. It does not autonomously execute Gemini-proposed coordinates.

User teaching is stored as USER_CONFIRMED. AI guesses must remain CANDIDATE until independently verified. Tactical combat remains RESERVED_RL.
