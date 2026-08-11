"""Agent-Native V2 Architecture — StateGraph Engine + Checkpoint Persistence.

Phase 3 of the agent-native refactoring. Replaces PipelineOrchestrator with
a StateGraph engine that drives execution through milestones, persists
checkpoints to DB, and supports HITL resume.
"""
